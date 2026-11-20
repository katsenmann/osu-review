"""osu!review - shows an analysis of every finished (or saved failed) osu! play.

Runs in the background, watches osu! stable and osu!lazer for new replays,
analyses hits / misses and opens a local page with the result. Optionally hands
the replay over to Rewind so you can watch it there.

Only uses the Python standard library.
"""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from core.osr import parse_osr, MODE_NAMES
from core.beatmap import parse_osu, apply_mods, apply_stacking, public_objects

ANALYSIS_VERSION = 9   # bump -> stored plays are re-analysed automatically
from core.analyze import analyze
from core.patterns import analyze_patterns, summary_of as pattern_summary
from core import library
from core import skins
from core import tablet
from core.media import StableMedia, read_header
from core.render import Renderer, PRESETS, ENCODERS
from core.vendor import RosuVendor
from core.otd_live import OTDLive
from core.shell import Shell
from core.osuapi import OsuApi
from core.tosu import TosuBridge
from core.lazercalc import LazerCalc, build_job
from core.tosu_manager import TosuManager
from core import overlay as OV
from core import secret as SECRET
from core.lazermedia import LazerMedia, sniff_type
from core.updater import Updater
from core.tray import Tray

APP_NAME = "osu!review"
PORT = 47290
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ.get("APPDATA", HERE), "osu-review") if os.name == "nt" else os.path.join(HERE, "data")
PLAYS = os.path.join(DATA, "plays")
REPLAYS = os.path.join(DATA, "replays")
MAPS = os.path.join(DATA, "maps")      # copy of every analysed .osu (survives map updates / deletions)
CONFIG_FILE = os.path.join(DATA, "config.json")
LOG_FILE = os.path.join(DATA, "log.txt")
for d in (DATA, PLAYS, REPLAYS):
    os.makedirs(d, exist_ok=True)

DEFAULTS = {
    "stable_path": "",
    "songs_path": "",
    "lazer_path": "",
    "watch_stable": True,
    "watch_lazer": True,
    "popup": "app",            # app | browser | off
    "bring_to_front": True,
    "rewind_handoff": True,
    "rewind_folder": "",       # default: <stable>/Replays
    "rewind_exe": "",
    "keep_plays": 300,
    "skin": "",              # "" = osu!review default
    "skins_path": "",        # default: <stable>/Skins
    "tosu_url": "http://127.0.0.1:24050",   # tosu (pp from osu!lazer's calculator) - used when it runs
    "osu_client_id": "",     # osu! API (OAuth app) - only to read the official pp of a score
    "osu_client_secret": "",
    "otd_path": "",          # default: %LOCALAPPDATA%/OpenTabletDriver/settings.json
    "tablet_w": 0,           # physical tablet size in mm (0 = from known-tablet list)
    "tablet_h": 0,
    "screen_w": 0,           # 0 = detect
    "screen_h": 0,
    "viewer_fps": 0,         # 0 = monitor refresh rate
    "viewer_interp": True,
    "otd_hz_always": False,
    "auto_save_fail": False,   # osu!stable: press F2 on the game over screen so failed plays get a replay
    "tosu_auto": True,         # download tosu once and run it in the background (Windows)
    "overlay": {},             # song-select overlay (see core/overlay.py for the keys)
    "tab_map": {"on": True, "op": 0.6},   # replay: draw the map on the tablet view
    "vol_music": 0.6,
    "vol_hits": 0.7,
    "vol_mute": False,
    "audio_offset": 0,       # ms, + = song later
    "video_dir": "",         # default: <user>/Videos/osu!review
    "render": {},            # last used render options
    "ui_theme": {},          # client design (colours, glass, font, density, sidebar)
    "hud": {},               # HUD layout / style
}


def _clean_setting(k, v):
    """Only accept a value of the right kind for each setting (a bad value can't break or misuse the app)."""
    d = DEFAULTS[k]
    try:
        if isinstance(d, bool):
            return True, bool(v)
        if isinstance(d, (int, float)):
            if v in ("", None):
                return True, d
            f = float(v)
            if f != f or abs(f) > 1e7:                     # NaN / absurd
                return False, None
            return True, (int(f) if isinstance(d, int) and float(int(f)) == f else f)
        if isinstance(d, dict):
            ok = isinstance(v, dict) and len(json.dumps(v)) < 200_000
            return ok, v
        if isinstance(d, str):
            if not isinstance(v, str) or len(v) > 2048 or "\x00" in v:
                return False, None
            v = v.strip()
            if k == "tosu_url":                            # tosu always runs on this PC
                if v and not re.fullmatch(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?/?", v, re.I):
                    return False, None
                return True, v.rstrip("/") or DEFAULTS["tosu_url"]
            if k == "rewind_exe" and v and os.name == "nt" and not v.lower().endswith(".exe"):
                return False, None
            if k == "popup" and v not in ("app", "browser", "off"):
                return False, None
            return True, v
    except (TypeError, ValueError, OverflowError):
        return False, None
    return False, None


def overlay_state():
    """What the song-select overlay shows: osu!'s current screen + your local plays of that map."""
    live = dict(getattr(S.tosu, "live", None) or {}) if getattr(S, "tosu", None) else {}
    cfg = OV.merged(S.cfg.get("overlay"))
    st = live.get("state")
    out = {"cfg": cfg, "tosu": bool(getattr(S.tosu, "connected", False)), "state": st,
           "screen": ("modsel" if getattr(S, "modsel", None) and S.modsel.open else "select") if st == 5 else "play" if st == 2 else "results" if st == 7 else "menu" if st is not None else "off",
           "md5": live.get("md5"), "mods": live.get("mods") or "", "res": live.get("res"),
           "map": {"title": live.get("title"), "artist": live.get("artist"), "version": live.get("version"),
                   "length": live.get("length"), "stars": live.get("stars"), "maxc": live.get("maxc")},
           "ppacc": live.get("ppacc") or {}, "plays": []}
    if live.get("md5"):
        try:
            own = S.ovc.for_map(live["md5"])
            osu = []
            try:
                osu = S.osc.for_map(S.cfg.get("stable_path"), live["md5"])
            except Exception as e:
                log(f"overlay scores.db: {e}")
            out["plays"] = OV.combine(own, osu)
        except Exception as e:
            log(f"overlay: {e}")
    out["ovv"] = _overlay_version()
    try:
        out["recent"] = S.ovc.recent(30)
    except Exception:
        out["recent"] = []
    return out


_OVV = {"t": 0, "v": 0}


def _overlay_version():
    """mtime of the overlay page; the page reloads itself when it changes. Keeps tosu's copy up to date too."""
    now = time.time()
    if now - _OVV["t"] > 5:
        _OVV["t"] = now
        try:
            v = int(os.path.getmtime(os.path.join(HERE, "web", "overlay.html")))
            if v != _OVV["v"]:
                old = _OVV["v"]
                _OVV["v"] = v
                m = getattr(S, "tosu_mgr", None)
                if old and m and OV.merged(S.cfg.get("overlay")).get("ingame"):
                    OV.install_tosu_counter(m.dir, HERE)
        except Exception:
            pass
    return _OVV["v"]


def overlay_apply():
    """Settings changed: start/stop the mini window and the counter in tosu's in-game overlay."""
    cfg = OV.merged(S.cfg.get("overlay"))
    try:
        mini_window(cfg["window"], cfg["window_top"])
    except Exception as e:
        log(f"overlay window: {e}")
    m = getattr(S, "tosu_mgr", None)
    if not m or os.name != "nt":
        return
    try:
        want = bool(cfg["ingame"])
        if want:
            OV.install_tosu_counter(m.dir, HERE)
        else:
            OV.remove_tosu_counter(m.dir)
        if m.set_env("ENABLE_INGAME_OVERLAY", "true" if want else "false"):
            log(f"overlay: tosu in-game overlay {'on' if want else 'off'} – restarting tosu")
            if m.state == "running":
                m.stop()
                m.start_async()
    except Exception as e:
        log(f"overlay in-game: {e}")


def mini_window(on, top=False):
    p = getattr(S, "mini_proc", None)
    running = p is not None and p.poll() is None
    if on and not running:
        py = S.shell._python() if getattr(S, "shell", None) else None
        if not py:
            log("overlay window: no window runtime available")
            return
        args = [py, os.path.join(HERE, "window.py"), "--mini"] + (["--top"] if top else [])
        S.mini_proc = subprocess.Popen(args, cwd=HERE, creationflags=0x08000000 if os.name == "nt" else 0)
        log("overlay window opened")
    elif not on and running:
        try:
            p.terminate()
        except Exception:
            pass
        S.mini_proc = None
        log("overlay window closed")
    elif on and running and getattr(S, "mini_top", None) != top:
        mini_window(False)
        mini_window(True, top)
        return
    S.mini_top = top


def on_failed_play(info):
    """tosu saw the health run out: on osu!stable press F2 on the game over screen -> the replay is exported."""
    if not S.cfg.get("auto_save_fail"):
        return
    if (info.get("client") or "stable") != "stable":
        log("failed play – auto-save only works in osu!stable (lazer has its own save button)")
        return
    from core import keys
    folder = os.path.join(S.cfg.get("stable_path") or "", "Replays")

    def newest():
        try:
            return max((e.stat().st_mtime for e in os.scandir(folder) if e.name.lower().endswith(".osr")), default=0)
        except OSError:
            return 0
    before = newest()
    time.sleep(1.2)                       # let the game over menu appear
    for attempt in range(3):              # the menu can take a moment - try again if no replay showed up
        ok, msg = keys.press_f2_in_osu()
        if not ok:
            log(f"failed play – couldn't press F2: {msg}")
            return
        for _ in range(10):
            time.sleep(0.25)
            if newest() > before:
                log(f"failed play – replay saved (F2, attempt {attempt + 1})")
                return
    log("failed play – pressed F2 but no replay appeared in the Replays folder")


def log(*a):
    msg = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    try:
        print(msg)
    except Exception:
        pass
    for fp in (LOG_FILE, os.path.join(HERE, "diagnostics", "log.txt")):
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass


class State:
    def __init__(self):
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.load(open(CONFIG_FILE, encoding="utf-8")))
        except Exception:
            pass
        enc = self.cfg.get("osu_client_secret") or ""
        self.cfg["osu_client_secret"] = SECRET.unprotect(enc)
        self.autodetect()
        if enc and not str(enc).startswith(SECRET.PREFIX):
            self.save_cfg()                      # old plain-text secret -> encrypted from now on
        self.index = library.BeatmapIndex(os.path.join(DATA, "beatmap_index.json"))
        self.index.on_done.append(lambda: threading.Thread(target=retry_missing_maps, daemon=True).start())
        self.lock = threading.Lock()
        self.event_id = 0
        self.events = []          # (id, play_id)
        self.last_ui_ping = 0
        self.pending = {}         # path -> (first_seen, source, tries)
        self.seen_keys = set()
        self.ignore_paths = set() # files we copied ourselves
        for fn in os.listdir(PLAYS):
            if fn.endswith(".json"):
                self.seen_keys.add(fn[:-5])
        self.watchers_built = False
        self.paths_status = {}

    def autodetect(self):
        c = self.cfg
        if not c["stable_path"]:
            c["stable_path"] = library.default_stable_path()
        if not c["songs_path"]:
            c["songs_path"] = library.stable_songs_dir(c["stable_path"])
        if not c["lazer_path"]:
            c["lazer_path"] = library.default_lazer_path()
        if not c["rewind_folder"] and c["stable_path"]:
            c["rewind_folder"] = os.path.join(c["stable_path"], "Replays")
        if not c["skins_path"] and c["stable_path"]:
            c["skins_path"] = os.path.join(c["stable_path"], "Skins")
        if not c["rewind_exe"] or not os.path.isfile(c["rewind_exe"]):
            c["rewind_exe"] = library.default_rewind_exe()
        if not c["otd_path"]:
            c["otd_path"] = tablet.default_settings_path()

    def save_cfg(self):
        out = dict(self.cfg)
        out["osu_client_secret"] = SECRET.protect(out.get("osu_client_secret") or "")
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp, CONFIG_FILE)

    def public_cfg(self):
        """Settings for the UI - without the real API secret."""
        out = dict(self.cfg)
        if out.get("osu_client_secret"):
            out["osu_client_secret"] = SECRET.MASK
        return out


S = State()
MEDIA = StableMedia()


def resolve_media(bm_path, meta=None, audio=None, bg=None):
    """Song + background folder: next to the .osu, otherwise the same map in the stable Songs folder."""
    own = os.path.dirname(bm_path)
    if meta is None:
        meta = read_header(bm_path)
        audio = audio or meta.get("AudioFilename")
        bg = bg or meta.get("bg")
    has_a = bool(audio) and os.path.isfile(os.path.join(own, audio))
    has_b = bool(bg) and os.path.isfile(os.path.join(own, bg))
    if has_a:
        return {"dir": own, "audio": audio, "bg": bg if has_b else None, "twin": False}
    twin = MEDIA.find(list(S.index.stable.keys()), meta, audio, bg)
    if twin:
        twin["twin"] = True
        if has_b and not twin.get("bg"):
            twin["bg"] = None
        return twin
    lz = getattr(S, "lzm", None) and S.lzm.lookup(library.md5_file(bm_path))
    if lz:                                     # osu!lazer: files learned from tosu
        return {"dir": lz["dir"], "audio": lz["audio"], "bg": lz["bg"], "twin": False, "lazer": True}
    return {"dir": own, "audio": None, "bg": bg if has_b else None, "twin": False}


_media_cache = {}


def play_media(p):
    bm = p.get("beatmap") or {}
    if bm.get("media_dir") is not None:
        m = {"dir": bm["media_dir"], "audio": bm.get("audio") if bm.get("has_audio") else None,
             "bg": bm.get("bg") if bm.get("has_bg") else None}
        if not m["audio"]:                     # osu!lazer map: song/background learned from tosu later
            lz = getattr(S, "lzm", None) and S.lzm.lookup((p.get("replay") or {}).get("beatmap_md5"))
            if lz:
                return {"dir": lz["dir"], "audio": lz["audio"], "bg": lz["bg"]}
        return m
    pid = p["id"]
    if pid not in _media_cache:
        path = bm.get("path")
        _media_cache[pid] = resolve_media(path) if path and os.path.isfile(path) else {"dir": "", "audio": None, "bg": None}
    return _media_cache[pid]


# ---------------------------------------------------------------- processing

def reanalyze(pid, notify=True):
    """Runs the analysis again for a stored replay (e.g. beatmap was missing before)."""
    osr = os.path.join(REPLAYS, os.path.basename(pid) + ".osr")
    if not os.path.isfile(osr):
        return False
    old = None
    try:
        old = load_play(pid)
    except Exception:
        pass
    with S.lock:
        S.seen_keys.discard(pid)
    try:
        os.remove(os.path.join(PLAYS, pid + ".json"))
    except OSError:
        pass
    key = process_replay(None, (old or {}).get("source", "import"), data=open(osr, "rb").read(),
                         added=(old or {}).get("added"), quiet=not notify, rerun=True)
    if key and old:
        # only the analysis is new - keep what belongs to the moment of the play: the tablet area that was
        # active back then, tosu's recorded pp, the stored pp values and which lists (analyzer / render) it is in
        with _save_lock:
            try:
                p = load_play(key)
                for k, v in old.items():
                    if k in ("analysis", "beatmap", "error", "av", "replay", "id", "pp_lazer"):
                        continue
                    p[k] = v
                save_play(p)
            except Exception:
                log(traceback.format_exc())
    return key is not None


def retry_missing_maps():
    """After a beatmap scan: re-analyse every stored play whose map wasn't found."""
    for fn in os.listdir(PLAYS):
        if not fn.endswith(".json"):
            continue
        try:
            p = load_play(fn[:-5])
        except Exception:
            continue
        err = p.get("error")
        outdated = p.get("analysis") and p.get("av", 1) < ANALYSIS_VERSION
        if outdated or (isinstance(err, dict) and err.get("code") in ("nomap", "indexing", "failed")):
            try:
                reanalyze(p["id"], notify=False)
                log("re-analysed:", p["id"])
                with S.lock:
                    S.event_id += 1
                    S.events.append((S.event_id, "refresh:" + p["id"]))
            except Exception:
                log(traceback.format_exc())


def keep_map(md5, src):
    """Keeps a copy of the .osu next to the plays, so a play stays analysable later."""
    try:
        os.makedirs(MAPS, exist_ok=True)
        dst = os.path.join(MAPS, os.path.basename(md5) + ".osu")
        if not os.path.isfile(dst) and src and os.path.isfile(src):
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
    except OSError:
        pass


def keep_all_maps():
    for fn in os.listdir(PLAYS):
        if fn.endswith(".json"):
            try:
                p = load_play(fn[:-5])
                keep_map(p["replay"]["beatmap_md5"], (p.get("beatmap") or {}).get("path"))
            except Exception:
                pass


def map_file(p):
    """Path of the .osu for a stored play: the original, else our kept copy."""
    bp = (p.get("beatmap") or {}).get("path")
    if bp and os.path.isfile(bp):
        return bp
    cp = os.path.join(MAPS, os.path.basename(p["replay"]["beatmap_md5"]) + ".osu")
    return cp if os.path.isfile(cp) else None


_lazer_busy = {}


def lazer_pp(pid):
    """pp from osu!lazer's own calculator (the build tosu uses) - cached in the play once done."""
    try:
        p = load_play(pid)
    except Exception:
        return {"status": "error", "error": "play not found"}
    if p["replay"].get("mode") != 0 or not p.get("analysis"):
        return {"status": "unsupported"}
    ver = S.lazer.version()
    c = p.get("pp_lazer")
    if c and c.get("version") == ver and ver:
        return dict(c, status="ok")
    if not S.lazer.installed():
        S.lazer.ensure_async()
        return {"status": S.lazer.state, "error": S.lazer.error}
    bp = map_file(p)
    if not bp:
        return {"status": "error", "error": "map not found"}
    ev = _lazer_busy.get(pid)
    if ev:                                   # already running for this play: wait for it
        ev.wait(240)
        try:
            c = load_play(pid).get("pp_lazer")
            return dict(c, status="ok") if c else {"status": "error", "error": "calculation failed"}
        except Exception as e:
            return {"status": "error", "error": str(e)}
    ev = _lazer_busy[pid] = threading.Event()
    try:
        job = build_job(p, open(bp, encoding="utf-8-sig", errors="replace").read())
        res = S.lazer.calculate(job)
        keep = {k: res.get(k) for k in ("pp", "fc", "ss", "stars", "maxCombo", "aim", "speed", "reading", "grad", "version", "ms")}
        with _save_lock:
            p = load_play(pid)
            p["pp_lazer"] = keep
            save_play(p)
        log(f"lazer pp {pid}: {keep['pp']:.2f}pp ({keep.get('ms')} ms, calculator {ver})")
        return dict(keep, status="ok")
    except Exception as e:
        log("lazer pp failed:", pid, e)
        return {"status": "error", "error": str(e)[-300:]}
    finally:
        ev.set()
        _lazer_busy.pop(pid, None)


def lazer_backfill():
    """In the background: exact lazer pp for every stored score that doesn't have it yet (newest first)."""
    time.sleep(20)
    for _ in range(60):                       # wait for the calculator download (up to ~10 min)
        if S.lazer.installed():
            break
        time.sleep(10)
    if not S.lazer.installed():
        return
    files = sorted((f for f in os.listdir(PLAYS) if f.endswith(".json")), key=lambda f: -os.path.getmtime(os.path.join(PLAYS, f)))
    for fn in files:
        try:
            p = load_play(fn[:-5])
            c = p.get("pp_lazer")
            if c and c.get("version") == S.lazer.version():
                continue
            r = lazer_pp(p["id"])
            if r.get("status") == "ok" and not p.get("pp_tosu"):
                with _save_lock:          # list + home show this value until osu!'s own one is known
                    q = load_play(p["id"])
                    if not isinstance(q.get("pp"), dict):
                        q["pp"] = {"pp": r["pp"], "fc": r.get("fc"), "ss": r.get("ss"), "stars": r.get("stars"),
                                   "max_combo": r.get("maxCombo")}
                        save_play(q)
        except Exception as e:
            log("lazer backfill:", fn, e)
        time.sleep(0.5)


def play_key(rep):
    raw = f'{rep["beatmap_md5"]}|{rep["player"]}|{rep["timestamp"]}|{rep["score"]}|{rep["n300"]}|{rep["nmiss"]}'
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def process_replay(path, source, data=None, added=None, quiet=False, folder=None, rerun=False):
    if data is None:
        with open(path, "rb") as f:
            data = f.read()
    rep = parse_osr(data)
    key = play_key(rep)
    if source in ("stable", "lazer") and rep["timestamp"] and added is None:
        # skip old/downloaded replays (e.g. watched online replays) - only fresh plays
        import datetime
        age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(rep["timestamp"])
        if age.total_seconds() > 20 * 60:
            return None
    with S.lock:
        # deleted plays stay "seen" so the watcher never brings them back; an explicit import may re-add them
        if key in S.seen_keys and (source != "import" or os.path.exists(os.path.join(PLAYS, key + ".json"))):
            return None
        S.seen_keys.add(key)
    with open(os.path.join(REPLAYS, key + ".osr"), "wb") as f:
        f.write(data)

    result = {
        "id": key,
        "source": source,
        "added": added or time.time(),
        "av": ANALYSIS_VERSION,
        "replay": {k: v for k, v in rep.items() if k != "frames"},
        "frames": [[t, round(x, 1), round(y, 1), k] for (t, x, y, k) in rep["frames"]],
        "beatmap": None,
        "analysis": None,
        "error": None,
        "in_render": folder == "render",
        "in_analysis": folder != "render",
    }
    bm_path = S.index.find(rep["beatmap_md5"])
    if not bm_path and not S.index.busy:
        S.index.scan(S.cfg["songs_path"] if S.cfg["watch_stable"] else "",
                     S.cfg["lazer_path"] if S.cfg["watch_lazer"] else "", notify=False)
        bm_path = S.index.find(rep["beatmap_md5"])
    if not bm_path:
        kept = os.path.join(MAPS, os.path.basename(rep["beatmap_md5"]) + ".osu")
        if os.path.isfile(kept):
            bm_path = kept
    if rep["mode"] != 0:
        result["error"] = {"code": "mode", "mode": MODE_NAMES.get(rep["mode"])}
    elif not bm_path:
        # index still being built -> analysis follows automatically when it's done
        result["error"] = {"code": "indexing" if S.index.busy else "nomap"}
    if bm_path:
        try:
            text = open(bm_path, encoding="utf-8-sig", errors="replace").read()
            keep_map(rep["beatmap_md5"], bm_path)
            bm = parse_osu(text)
            cs, od, ar = apply_mods(bm, rep["mods"])
            apply_stacking(bm, cs, ar)
            md = bm["metadata"]
            total = sum(1 for o in bm["objects"])
            judged = rep["n300"] + rep["n100"] + rep["n50"] + rep["nmiss"]
            result["beatmap"] = {
                "path": bm_path,
                "title": md.get("Title", "?"), "artist": md.get("Artist", "?"),
                "version": md.get("Version", "?"), "creator": md.get("Creator", "?"),
                "cs": round(cs, 2), "od": round(od, 2), "ar": round(ar, 2), "hp": bm["hp"],
                "objects": public_objects(bm), "total": total,
                "length": max((o.get("end", o["t"]) for o in bm["objects"]), default=0),
            }
            media = resolve_media(bm_path, md, bm.get("audio"), bm["background"])
            result["beatmap"].update({
                "media_dir": media["dir"], "media_twin": media.get("twin", False),
                "audio": media["audio"], "has_audio": bool(media["audio"]),
                "bg": media["bg"], "has_bg": bool(media["bg"]),
            })
            rep_passed = judged >= total
            result["replay"]["passed"] = rep_passed
            if rep["mode"] == 0:
                end_t = None
                if not rep_passed:
                    # failed / quit: where did the play end?
                    end_t = max((f[0] for f in rep["frames"]), default=0)
                    result["replay"]["fail_time"] = end_t
                result["analysis"] = analyze(rep, bm, cs, od, ar, end_time=end_t)
                try:
                    # the tablet area only says something about scores played on this PC
                    if rerun:
                        result["tablet"] = None          # re-analysis: the old snapshot is kept by reanalyze()
                    elif is_own(rep, source):
                        result["tablet"] = tablet_snapshot(rep, bm, end_t)
                    else:
                        result["tablet"] = None
                        result["foreign"] = True
                except Exception as te:
                    # tablet data is optional - never let it break the analysis
                    result["tablet"] = None
                    diag("tablet_error", {"error": repr(te), "trace": traceback.format_exc()})
        except Exception as e:
            result["error"] = {"code": "failed", "msg": str(e)}
            log(traceback.format_exc())
            diag("analysis_error", {"error": repr(e), "trace": traceback.format_exc(), "play": key})

    save_play(result)
    log(("re-analysed" if rerun else "new score") + f" [{source}] {rep['player']} – {result['beatmap']['title'] if result['beatmap'] else rep['beatmap_md5']}")
    prune()
    if getattr(S, "tosu", None) and (S.tosu.connected or S.tosu.records):
        threading.Thread(target=attach_tosu, args=(key,), daemon=True).start()
    if quiet:
        return key
    with S.lock:
        S.event_id += 1
        S.events.append((S.event_id, key))
        S.events = S.events[-50:]
    rewind_handoff(path, key, source)
    show_ui(key)
    return key


DIAG = os.path.join(HERE, "diagnostics")


def diag(name, data):
    """Writes diagnostic info into the app folder (so it can be inspected when helping)."""
    try:
        os.makedirs(DIAG, exist_ok=True)
        data = dict(data)
        data["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            data["otd_raw"] = S.otd.raw if getattr(S, "otd", None) else None
            data["otd_parsed"] = get_otd()
            data["screen"] = current_screen()
        except Exception as e:
            data["diag_error"] = repr(e)
        with open(os.path.join(DIAG, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, default=str)
    except Exception:
        pass


_own_cache = [0.0, set()]


def own_names():
    """Player names that belong to this PC (osu! stable config, else whoever the auto-detected scores are from)."""
    if time.time() - _own_cache[0] < 60:
        return _own_cache[1]
    names = set()
    try:
        sp = S.cfg.get("stable_path") or ""
        for fn in os.listdir(sp) if os.path.isdir(sp) else []:
            if fn.lower().startswith("osu!.") and fn.lower().endswith(".cfg") and fn.lower() != "osu!.cfg":
                for line in open(os.path.join(sp, fn), encoding="utf-8", errors="replace"):
                    if line.strip().lower().startswith("username"):
                        v = line.split("=", 1)[-1].strip()
                        if v:
                            names.add(v.lower())
                        break
    except OSError:
        pass
    if not names:
        cnt = {}
        for fn in os.listdir(PLAYS):
            if fn.endswith(".json"):
                try:
                    p = load_play(fn[:-5])
                    if p.get("source") in ("stable", "lazer") and p["replay"].get("player"):
                        n = p["replay"]["player"].lower()
                        cnt[n] = cnt.get(n, 0) + 1
                except Exception:
                    pass
        if cnt:
            names.add(max(cnt, key=cnt.get))
    _own_cache[0], _own_cache[1] = time.time(), names
    return names


def is_own(rep, source):
    if source in ("stable", "lazer"):
        return True          # written by the local client right after playing
    player = (rep.get("player") or "").strip().lower()
    names = own_names()
    if not player or not names:
        return source != "import"
    return player in names


def tablet_snapshot(rep, bm, end_t):
    """Tablet / area data: snapshot of the OTD config at the time of the play."""
    otd = get_otd()
    if getattr(S, "otd", None):
        dur = (max((o.get("end", o["t"]) for o in bm["objects"]), default=0)) / 1000 + 20
        otd["hz"] = S.otd.hz_stats(time.time() - dur)
    scr = current_screen(otd)
    objs_ = [o for o in bm["objects"]]
    t0 = objs_[0]["t"] - 200 if objs_ else 0
    t1 = (end_t if end_t is not None else max(o.get("end", o["t"]) for o in objs_)) if objs_ else 0
    pts = tablet.cursor_sample(rep["frames"], t0, t1)
    mapper = tablet.make_mapper(otd, scr)
    edge = None
    if mapper:
        bx0, by0, bx1, by1 = mapper.screen_bounds_osu
        inside = [f for f in rep["frames"] if t0 <= f[0] <= t1]
        if inside:
            near = sum(1 for f in inside if f[1] <= bx0 + 1 or f[1] >= bx1 - 1 or f[2] <= by0 + 1 or f[2] >= by1 - 1)
            edge = round(100 * near / len(inside), 2)
    return {"otd": otd, "screen": scr, "cursor": pts, "edge_pct": edge,
            "mm_per_osu": mapper.mm_per_osu if mapper else None, "machine": machine_id()}


def machine_id():
    """Anonymous id of this PC + Windows user - tablet data is only shown on the PC it was recorded on."""
    import socket
    raw = f'{socket.gethostname()}|{os.environ.get("USERNAME") or os.environ.get("USER") or ""}'
    return hashlib.sha1(raw.lower().encode()).hexdigest()[:12]


def other_pc(p):
    m = (p.get("tablet") or {}).get("machine")
    return bool(m) and m != machine_id()


def get_otd():
    """Area/display from the running OTD daemon (live), falling back to settings.json."""
    live = S.otd.settings() if getattr(S, "otd", None) else None
    if live:
        o = tablet.parse_otd(live, "OpenTabletDriver (live)", prefer=S.otd.connected_names())
        o["source"] = "live"
    else:
        o = tablet.read_otd(S.cfg["otd_path"] if S.cfg.get("otd_path") and os.path.isfile(S.cfg["otd_path"]) else tablet.default_settings_path())
        o["source"] = "file" if o.get("found") else None
    if getattr(S, "otd", None):
        dig = S.otd.digitizer(o.get("tablet"))
        if dig:
            o["digitizer"] = dig
            if not o.get("tablet"):
                o["tablet"] = dig.get("name")
    return o


def attach_tosu(pid, wait=8.0):
    """Takes the pp tosu showed for this score (results screen + live course), if tosu was running."""
    t_end = time.time() + wait
    while True:
        try:
            p = load_play(pid)
        except Exception:
            return None
        if p.get("pp_tosu"):
            return p["pp_tosu"]
        rec = S.tosu.find(p["replay"])
        if rec:
            with _save_lock:
                p = load_play(pid)
            p["pp_tosu"] = {"pp": rec["pp"], "fc": rec.get("fc"), "curve": rec.get("curve") or [], "client": rec.get("client")}
            save_play(p)
            log(f"pp taken from tosu: {rec['pp']:.2f}pp")
            with S.lock:
                S.event_id += 1
                S.events.append((S.event_id, "refresh:" + pid))
            return p["pp_tosu"]
        if time.time() >= t_end:
            return None
        time.sleep(0.5)


_pp_lock = threading.Lock()


def official_pp(pid, force=False):
    """pp of this score as osu! itself lists it (osu! API). Cached in the play."""
    if not S.osuapi.ready:
        return {"status": "off"}
    try:
        p = load_play(pid)
    except Exception:
        return {"status": "error", "error": "play"}
    c = p.get("pp_official")
    if c and not force:
        if c.get("status") in ("found", "unranked"):
            return c
        if time.time() - c.get("checked", 0) < 600:
            return c
    with _pp_lock:
        try:
            res = S.osuapi.score_pp(p["replay"])
        except PermissionError as e:
            return {"status": "error", "error": str(e)}
        except Exception as e:
            log(f"osu!-API: {e}")
            return {"status": "error", "error": str(e)[:200]}
    res["checked"] = time.time()
    try:
        with _save_lock:
            p = load_play(pid)
            p["pp_official"] = res
            save_play(p)
    except Exception:
        pass
    return res


def otd_view():
    """Mirror of what OpenTabletDriver on THIS computer shows - live from the daemon, else its settings file."""
    live = S.otd.settings()
    if live:
        with S.otd.lock:
            tabs = list(S.otd.status["tablets"])
            ver = S.otd.status.get("version")
        v = tablet.otd_view(live, tabs, True, None, "live")
        v["version"] = ver
        return v
    path = tablet.default_settings_path()
    if path and os.path.isfile(path):
        try:
            data = json.load(open(path, encoding="utf-8-sig"))
            v = tablet.otd_view(data, [], False, None, "file", path)
            v["error"] = S.otd.status.get("error")
            return v
        except Exception as e:
            return {"source": None, "error": f"settings.json: {e}", "monitors": tablet.monitors(), "profiles": []}
    return {"source": None, "error": S.otd.status.get("error"), "monitors": tablet.monitors(), "profiles": []}


def osu_offsets():
    """Universal offset from osu! stable (osu!.<user>.cfg) and lazer (game.ini), if set."""
    import re as _re
    out = {"stable": None, "lazer": None}
    st = S.cfg.get("stable_path")
    try:
        if st and os.path.isdir(st):
            for fn in os.listdir(st):
                if fn.lower().startswith("osu!.") and fn.lower().endswith(".cfg") and fn.lower() != "osu!.cfg":
                    for line in open(os.path.join(st, fn), encoding="utf-8", errors="replace"):
                        m = _re.match(r"\s*Offset\s*=\s*(-?\d+(\.\d+)?)", line)
                        if m:
                            out["stable"] = float(m.group(1))
                            break
    except OSError:
        pass
    lz = S.cfg.get("lazer_path")
    try:
        gi = os.path.join(lz, "game.ini") if lz else ""
        if gi and os.path.isfile(gi):
            for line in open(gi, encoding="utf-8", errors="replace"):
                m = _re.match(r"\s*AudioOffset\s*=\s*(-?\d+(\.\d+)?)", line)
                if m:
                    out["lazer"] = float(m.group(1))
                    break
    except OSError:
        pass
    return out


def video_dir():
    d = S.cfg.get("video_dir")
    if not d:
        d = os.path.join(os.path.expanduser("~"), "Videos", "osu!review")
    return d


def gpu_vendor():
    if os.name != "nt":
        return None
    if shutil.which("nvidia-smi") or os.path.isfile(r"C:\Windows\System32\nvidia-smi.exe"):
        return "nvidia"
    return None


def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip().rstrip(".")
    return re.sub(r"\s+", " ", s)[:150] or "replay"


RENDER_SONGS = os.path.join(DATA, "render-songs")


def clean_render_songs(days=30):
    """Drops the little lazer map folders for danser that weren't used for a month."""
    try:
        for n in os.listdir(RENDER_SONGS):
            d = os.path.join(RENDER_SONGS, n)
            if n.startswith("osr-") and os.path.isdir(d) and time.time() - os.path.getmtime(d) > days * 86400:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def _link_or_copy(src, dst):
    if os.path.isfile(dst):
        return
    try:
        os.link(src, dst)                       # same drive: no extra space used
    except OSError:
        shutil.copyfile(src, dst)


def lazer_render_songs(p):
    """osu!lazer has no Songs folder, danser needs one: builds a tiny one with just this map.

    <data>/render-songs/osr-<md5>/ gets the .osu plus the song and background (from lazer's file store,
    learned through tosu). Returns (songs_dir, note) or (None, None) when the map is in stable's Songs anyway."""
    md5 = (p.get("replay") or {}).get("beatmap_md5") or ""
    if not re.fullmatch(r"[0-9a-f]{32}", md5):
        return None, None
    if any(v == md5 for v in S.index.stable.values()):
        return None, None                       # osu!stable has this map -> danser finds it there
    src = map_file(p)
    if not src:
        return None, None
    d = os.path.join(RENDER_SONGS, "osr-" + md5)
    os.makedirs(d, exist_ok=True)
    os.utime(d)
    meta = read_header(src)
    name = safe_name(f'{meta.get("Artist", "map")} - {meta.get("Title", md5)} ({meta.get("Creator", "")}) [{meta.get("Version", "")}]')[:150]
    _link_or_copy(src, os.path.join(d, name + ".osu"))
    lz = S.lzm.lookup(md5) if getattr(S, "lzm", None) else None
    note = None
    for key, want in (("audio", meta.get("AudioFilename")), ("bg", meta.get("bg"))):
        want = os.path.basename(str(want or "").replace("\\", "/"))
        if not want or os.path.isfile(os.path.join(d, want)):
            continue
        if lz and lz.get(key):
            _link_or_copy(os.path.join(lz["dir"], lz[key]), os.path.join(d, want))
        elif key == "audio":
            note = "no_audio"                   # map was never selected in lazer while tosu ran
    return RENDER_SONGS, note


def start_render(pid, opts, only_cfg=False):
    p = load_play(pid)
    r, bm = p["replay"], p.get("beatmap") or {}
    preset = PRESETS.get(opts.get("preset"), None)
    cfg = dict(preset or {"w": 1920, "h": 1080, "fps": 60, "mb": 8})
    for k in ("w", "h", "fps", "mb"):
        if opts.get(k) not in (None, ""):
            cfg[k] = int(opts[k])
    name = safe_name(f'{r["player"]} - {bm.get("artist", "?")} - {bm.get("title", "?")} [{bm.get("version", "?")}] '
                     f'{r["accuracy"]:.2f}% {"".join(r["mods_str"])} {time.strftime("%Y%m%d-%H%M%S")}')
    cfg.update({
        "replay": os.path.join(REPLAYS, pid + ".osr"), "name": name, "out_dir": video_dir(),
        "songs": S.cfg["songs_path"], "skins": S.cfg["skins_path"], "nodbcheck": False,
        "skin": opts.get("skin", S.cfg.get("skin") or ""),
        "music": float(opts.get("music", S.cfg.get("vol_music", 0.6))),
        "hits": float(opts.get("hits", S.cfg.get("vol_hits", 0.7))),
        "encoder": opts.get("encoder") or ("nvenc_h264" if gpu_vendor() == "nvidia" else "x264"),
        "offset": int(opts.get("offset") or 0),
        "hud": bool(opts.get("hud", True)),
        "hud_offset": int(opts.get("hud_offset") or 0),
        "dim": float(opts["dim"]) if opts.get("dim") is not None else 0.8,
        "map_hits": bool(opts.get("map_hits")), "map_colors": bool(opts.get("map_colors")),
        "dz": opts.get("dz") if isinstance(opts.get("dz"), dict) else {},
        "danser_over": opts.get("danser_over") if isinstance(opts.get("danser_over"), dict) else {},
    })
    lz_songs, lz_note = lazer_render_songs(p)
    if lz_songs:
        # osu!lazer-only map: danser reads it from our small folder. -nodbcheck keeps danser's
        # database of your stable Songs folder intact (otherwise it would rescan everything next time).
        cfg.update({"songs": lz_songs, "nodbcheck": True, "lazer_note": lz_note})
    objs = bm.get("objects") or []
    cfg["start_ms"] = max(0, (objs[0]["t"] if objs else 0) - 1500)
    if only_cfg:
        return cfg
    S.cfg["render"] = {k: opts.get(k) for k in ("preset", "w", "h", "fps", "mb", "encoder", "skin", "hud", "hud_offset", "dim", "map_hits", "map_colors")}
    S.save_cfg()
    job = {"id": pid + "-" + str(int(time.time())), "play": pid,
           "title": f'{bm.get("title", "?")} [{bm.get("version", "?")}]', "cfg": cfg}
    if cfg.get("lazer_note") == "no_audio":
        log("render: osu!lazer map without known song file - select it once in lazer while tosu runs, then render again")
    S.renderer.submit(job)
    return job["id"]


def current_screen(otd=None):
    """[w, h, x, y] of the screen osu! runs on (x/y in OTD's desktop coordinates)."""
    try:
        w, h = int(float(S.cfg.get("screen_w") or 0)), int(float(S.cfg.get("screen_h") or 0))
    except (TypeError, ValueError):
        w = h = 0
    if otd is None:
        try:
            otd = get_otd()
        except Exception:
            otd = {}
    try:
        return tablet.osu_screen((otd or {}).get("display"), (w, h) if w > 0 and h > 0 else None)
    except Exception:
        return [w, h, 0, 0] if w > 0 and h > 0 else list(tablet.screen_size()) + [0, 0]


def tablet_report(limit=40):
    """Aggregates aim + cursor data of the latest plays that used the current area."""
    otd = get_otd()
    scr = current_screen(otd)
    tw, th, tsrc = tablet.tablet_size(otd.get("tablet"), S.cfg.get("tablet_w"), S.cfg.get("tablet_h"))
    dig = otd.get("digitizer")
    if dig and tsrc != "manual":
        tw, th, tsrc = dig["width"], dig["height"], "otd"
    rep = {"otd": otd, "screen": scr, "tablet_size": [tw, th], "tablet_size_src": tsrc,
           "known": [k for k, _w, _h in tablet.KNOWN_TABLETS]}
    plays = []
    for fn in sorted(os.listdir(PLAYS), key=lambda f: -os.path.getmtime(os.path.join(PLAYS, f))):
        if not fn.endswith(".json"):
            continue
        try:
            p = load_play(fn[:-5])
        except Exception:
            continue
        if not p.get("analysis") or not p.get("tablet") or other_pc(p):
            continue
        plays.append(p)
        if len(plays) >= limit:
            break
    same = lambda a, b: a and b and all(abs(a[k] - b[k]) < 0.05 for k in ("w", "h", "x", "y", "rot"))
    cur_area = otd.get("area")
    used, heat, overs, rots, spreads, edges, n_j, per, hzs = [], [], [], [], [], [], 0, [], []
    for p in plays:
        snap = p["tablet"]["otd"]
        if cur_area and snap.get("area") and not same(cur_area, snap["area"]):
            continue
        aim = p["analysis"].get("aim") or {}
        if "over_pct" in aim:
            w = aim["n_jumps"]
            overs.append((aim["over_pct"], w))
            rots.append((aim["rot_deg"], w))
            n_j += w
        mapper = tablet.make_mapper(snap, p["tablet"]["screen"])
        if aim.get("spread") is not None and mapper:
            spreads.append(aim["spread"] * mapper.mm_per_osu)
        _hz = ((p["tablet"].get("otd") or {}).get("hz") or {}).get("avg")
        if _hz:
            hzs.append(_hz)
        if p["tablet"].get("edge_pct") is not None:
            edges.append(p["tablet"]["edge_pct"])
        if mapper:
            heat.extend([[round(v, 2) for v in mapper(x, y)] for x, y in p["tablet"]["cursor"][:400]])
        used.append(p["id"])
        bm = p.get("beatmap") or {}
        per.append({"id": p["id"], "t": p["added"], "title": bm.get("title"), "version": bm.get("version"),
                    "acc": p["replay"].get("accuracy"), "j": aim.get("n_jumps") or 0,
                    "over": aim.get("over_pct"), "rot": aim.get("rot_deg"), "edge": p["tablet"].get("edge_pct")})
    wavg = lambda xs: (sum(v * w for v, w in xs) / sum(w for _v, w in xs)) if xs and sum(w for _v, w in xs) else None
    rep.update({
        "plays_total": len(plays), "plays_used": len(used), "jumps": n_j,
        "over_pct": wavg(overs), "rot_deg": wavg(rots),
        "spread_mm": (sum(spreads) / len(spreads)) if spreads else None,
        "edge_pct": (sum(edges) / len(edges)) if edges else None,
        "heat": heat[:12000], "per_play": per, "hz_avg": (sum(hzs) / len(hzs)) if hzs else None,
    })
    mapper = tablet.make_mapper(otd, scr)
    if mapper:
        corners = [mapper(0, 0), mapper(512, 0), mapper(512, 384), mapper(0, 384)]
        rep["playfield_mm"] = [[round(a, 2), round(b, 2)] for a, b in corners]
        rep["mm_per_osu"] = mapper.mm_per_osu
    return rep


def set_folder(pid, render=None, analysis=None):
    """Which list a replay is in: Studio (to render) and/or the miss analyzer. In neither -> removed."""
    with _save_lock:
        try:
            p = load_play(pid)
        except Exception:
            return False
        if render is not None:
            p["in_render"] = bool(render)
        if analysis is not None:
            p["in_analysis"] = bool(analysis)
        if not p.get("in_render") and not p.get("in_analysis", True):
            for f in (os.path.join(PLAYS, pid + ".json"), os.path.join(REPLAYS, pid + ".osr")):
                if os.path.exists(f):
                    os.remove(f)
            return True
        save_play(p)
    return True


def prune():
    keep = int(S.cfg.get("keep_plays", 300))
    files = sorted((os.path.getmtime(os.path.join(PLAYS, f)), f) for f in os.listdir(PLAYS) if f.endswith(".json"))
    for _mt, f in files[:-keep] if len(files) > keep else []:
        try:
            if load_play(f[:-5]).get("in_render"):
                continue  # the render folder is kept until the user removes it
        except Exception:
            pass
        for g in (os.path.join(PLAYS, f), os.path.join(REPLAYS, f[:-5] + ".osr")):
            try:
                os.remove(g)
            except OSError:
                pass


def rewind_installed():
    exe = S.cfg.get("rewind_exe")
    if not exe or not os.path.isfile(exe):
        S.cfg["rewind_exe"] = exe = library.default_rewind_exe()
    return bool(exe)


def rewind_handoff(src_path, key, source):
    # only hand replays over when Rewind is actually installed (no clutter in osu!/Replays otherwise)
    if not S.cfg["rewind_handoff"] or not rewind_installed():
        return
    folder = S.cfg["rewind_folder"]
    if not folder or not os.path.isdir(folder):
        return
    if src_path and os.path.dirname(os.path.abspath(src_path)) == os.path.abspath(folder):
        return  # already in the watched folder (e.g. F2 export in stable)
    dst = os.path.join(folder, f"osu-review_{key}.osr")
    try:
        shutil.copyfile(os.path.join(REPLAYS, key + ".osr"), dst)
        S.ignore_paths.add(os.path.abspath(dst))
    except OSError as e:
        log("Rewind hand-off failed:", e)


# ---------------------------------------------------------------- UI window

def find_browser_app():
    if os.name != "nt":
        return None
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def bring_window_to_front():
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lp):
            n = user32.GetWindowTextLengthW(hwnd)
            if n and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.startswith(APP_NAME):
                    found.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        if not found:
            return False
        h = found[0]
        user32.keybd_event(0x12, 0, 0, 0)       # ALT down - lets us take focus
        user32.ShowWindow(h, 9)                 # SW_RESTORE
        user32.SetForegroundWindow(h)
        user32.keybd_event(0x12, 0, 2, 0)       # ALT up
        return True
    except Exception as e:
        log("Bringing the window to the front failed:", e)
        return False


def open_ui(play_id=None, _no_shell=False):
    url = f"http://127.0.0.1:{PORT}/" + (f"#new={play_id}" if play_id else "")
    mode = S.cfg["popup"]
    if mode == "app" and getattr(S, "shell", None) and not _no_shell:
        # own program window (WebView2); set up once in the background, Edge/Chrome window until then
        if S.shell.open(f"#new={play_id}" if play_id else ""):
            return
        S.shell.ensure()
    exe = find_browser_app() if mode == "app" else None
    if exe:
        prof = os.path.join(DATA, "window-profile")
        subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}", "--window-size=1400,900",
                          "--no-first-run", "--no-default-browser-check"])
    else:
        webbrowser.open(url)


def show_ui(play_id):
    if S.cfg["popup"] == "off":
        return
    if getattr(S, "shell", None) and S.shell.alive():
        S.shell.pending = {"hash": f"#new={play_id}"} if S.cfg["bring_to_front"] else None
        return
    ui_open = time.time() - S.last_ui_ping < 6
    if ui_open:
        if S.cfg["bring_to_front"]:
            # page switches by itself via /api/events; just raise the window
            threading.Timer(0.8, bring_window_to_front).start()
    else:
        open_ui(play_id)


# ---------------------------------------------------------------- watcher loop

def build_watchers():
    c = S.cfg
    w = []
    if c["watch_stable"] and c["stable_path"]:
        w.append(("stable", library.FolderWatcher(os.path.join(c["stable_path"], "Data", "r"))))
        w.append(("stable-export", library.FolderWatcher(os.path.join(c["stable_path"], "Replays"))))
    lazer_files = None
    if c["watch_lazer"] and c["lazer_path"]:
        w.append(("lazer-export", library.FolderWatcher(os.path.join(c["lazer_path"], "exports"))))
        lazer_files = library.LazerFilesWatcher(c["lazer_path"])
    S.paths_status = {
        "stable": bool(c["stable_path"]) and os.path.isdir(c["stable_path"]),
        "songs": bool(c["songs_path"]) and os.path.isdir(c["songs_path"]),
        "lazer": bool(c["lazer_path"]) and os.path.isdir(c["lazer_path"]),
        "rewind_folder": bool(c["rewind_folder"]) and os.path.isdir(c["rewind_folder"]),
        "rewind_exe": bool(c["rewind_exe"]) and os.path.isfile(c["rewind_exe"]),
        "skins": bool(c["skins_path"]) and os.path.isdir(c["skins_path"]),
        "otd": (bool(c["otd_path"]) and os.path.isfile(c["otd_path"])) or bool(getattr(S, "otd", None) and S.otd.status["connected"]),
    }
    return w, lazer_files


def watch_loop():
    threading.Thread(target=lambda: S.index.scan(
        S.cfg["songs_path"] if S.cfg["watch_stable"] else "",
        S.cfg["lazer_path"] if S.cfg["watch_lazer"] else ""), daemon=True).start()
    watchers, lazer_files = build_watchers()
    S.watchers_built = True
    tick = 0
    while True:
        try:
            if S.watchers_built is False:
                watchers, lazer_files = build_watchers()
                S.watchers_built = True
            for source, w in watchers:
                for p in w.poll():
                    if os.path.abspath(p) in S.ignore_paths:
                        continue
                    S.pending.setdefault(p, [time.time(), source, 0])
            if lazer_files and tick % 2 == 0:
                reps, others = lazer_files.poll()
                for p in reps:
                    S.pending.setdefault(p, [time.time(), "lazer", 0])
                for p in others:
                    S.index.add_lazer_file(p)
            # process files once they stopped changing
            now = time.time()
            for p, (first, source, tries) in list(S.pending.items()):
                try:
                    if now - os.path.getmtime(p) < 0.8 or now - first < 0.8:
                        continue
                    process_replay(p, source)
                    del S.pending[p]
                except FileNotFoundError:
                    S.pending.pop(p, None)
                except Exception as e:
                    S.pending[p][2] += 1
                    if S.pending[p][2] > 5:
                        log("Could not read replay:", p, e)
                        S.pending.pop(p, None)
        except Exception:
            log(traceback.format_exc())
        tick += 1
        time.sleep(1.0)


# ---------------------------------------------------------------- http

def _ppd(p):
    v = p.get("pp")
    return v if isinstance(v, dict) else ({"pp": v} if isinstance(v, (int, float)) else {})


def summary(p):
    r = p["replay"]
    bm = p.get("beatmap") or {}
    a = p.get("analysis") or {}
    return {
        "id": p["id"], "source": p["source"], "added": p["added"],
        "title": bm.get("title"), "artist": bm.get("artist"), "version": bm.get("version"),
        "player": r["player"], "mods": r["mods_str"], "acc": r["accuracy"], "miss": r["nmiss"],
        # played while logged out: osu! stable leaves the name empty, lazer writes "Guest"
        "offline": p["source"] in ("stable", "lazer", "stable-export", "lazer-export")
                   and (r.get("player") or "").strip().lower() in ("", "guest"),
        "combo": r["max_combo"], "passed": r.get("passed"), "lazer": r["is_lazer"],
        "ur": (a.get("stats") or {}).get("ur"), "has_bg": bm.get("has_bg"),
        "pp": _ppd(p).get("pp"), "stars": _ppd(p).get("stars"),
        "md5": r.get("beatmap_md5"), "n100": r.get("n100"), "n50": r.get("n50"),
        "reasons": a.get("reasons") or {}, "mean": (a.get("stats") or {}).get("mean"),
        "render": bool(p.get("in_render")), "in_analysis": p.get("in_analysis", True),
        "pat": {k: v for k, v in (p.get("pat_sum") or {}).items() if k != "k"} or None,
    }


_save_lock = threading.RLock()


PAT_VERSION = 1


def _pat_key(p):
    return f'{PAT_VERSION}-{p.get("av")}-{len((p.get("analysis") or {}).get("objects") or [])}'


def attach_pattern_summary(p):
    """keeps the small stream/jump numbers of the home page ('your limits') in the play file"""
    if not p.get("analysis") or (p.get("pat_sum") or {}).get("k") == _pat_key(p):
        return
    try:
        q = dict(p, tablet=None) if other_pc(p) else p
        ps = pattern_summary(analyze_patterns(q)) or {}
        ps["k"] = _pat_key(p)
        p["pat_sum"] = ps
    except Exception as e:
        log("patterns:", p.get("id"), e)


def pattern_backfill():
    time.sleep(8)
    for fn in [f for f in os.listdir(PLAYS) if f.endswith(".json")]:
        try:
            with _save_lock:
                p = load_play(fn[:-5])
                if p.get("analysis") and (p.get("pat_sum") or {}).get("k") != _pat_key(p):
                    save_play(p)
        except Exception as e:
            log("pattern backfill:", fn, e)
        time.sleep(0.05)


def save_play(p):
    """Atomic: write a private temp file, then swap it in - readers never see a half-written play."""
    attach_pattern_summary(p)
    fn = os.path.join(PLAYS, os.path.basename(p["id"]) + ".json")
    tmp = f"{fn}.{threading.get_ident()}.tmp"
    with _save_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f)
        for _ in range(5):
            try:
                os.replace(tmp, fn)
                break
            except PermissionError:      # Windows: file briefly open by a reader
                time.sleep(0.05)


def _inside(folder, name):
    """folder/name, but only if it really stays inside that folder (a map can't point at other files)."""
    if not folder or not name:
        return ""
    base = os.path.realpath(folder)
    fp = os.path.realpath(os.path.join(base, name))
    try:
        return fp if os.path.commonpath([base, fp]) == base else ""
    except ValueError:                     # other drive
        return ""


def load_play(pid):
    fn = os.path.join(PLAYS, os.path.basename(pid) + ".json")
    for i in range(4):
        try:
            with open(fn, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, PermissionError):
            if i == 3:
                raise
            time.sleep(0.05)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", cors=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            # other websites can't embed the client in a frame (clickjacking); OBS / tosu load the overlay directly
            self.send_header("Content-Security-Policy", "frame-ancestors 'self' http://127.0.0.1:* http://localhost:*")
        if cors:     # the overlay page also runs inside tosu (other port) - only pages on this PC may read it
            o = (self.headers.get("Origin") or "").strip()
            if re.fullmatch(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", o, re.I):
                self.send_header("Access-Control-Allow-Origin", o)
                self.send_header("Vary", "Origin")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass            # the window closed or reloaded while the answer was on its way

    def _raw(self, body, ctype, headers=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    # ---- only this PC's own pages may talk to the app ------------------------------------------
    # Blocks other websites from using the local API behind your back (cross-site requests and
    # DNS rebinding): the Host must be 127.0.0.1/localhost, and requests that come from another
    # site may only read the overlay data (used by OBS and tosu's in-game overlay).
    _OK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
    _PUBLIC = ("/overlay", "/api/overlay/state", "/favicon.ico")

    def _blocked(self, path, write):
        host = (self.headers.get("Host") or "").strip().lower()
        hname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if host and hname not in self._OK_HOSTS:
            self._send(403, {"error": "forbidden host"})
            return True
        origin = (self.headers.get("Origin") or "").strip().lower()
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        own = (not origin) or any(origin == f"http://{h}:{PORT}" for h in self._OK_HOSTS)
        if own and site not in ("cross-site", "same-site"):
            return False
        if not write and (path in self._PUBLIC or path.startswith("/static/")):
            return False
        # localhost pages on other ports (tosu, OBS) count as "same-site" - they may read, never write
        self._send(403, {"error": "forbidden origin"})
        return True

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        if self._blocked(path, False):
            return
        try:
            if path in ("/", "/index.html"):
                return self._send(200, open(os.path.join(HERE, "web", "index.html"), "rb").read(), "text/html; charset=utf-8")
            if path.startswith("/static/"):
                fp = os.path.join(HERE, "web", os.path.basename(path))
                return self._send(200, open(fp, "rb").read(), mimetypes.guess_type(fp)[0] or "application/octet-stream")
            if path == "/overlay":
                return self._send(200, open(os.path.join(HERE, "web", "overlay.html"), "rb").read(), "text/html; charset=utf-8")
            if path == "/api/overlay/winpos":
                return self._send(200, S.cfg.get("overlay_win") or {})
            if path == "/api/overlay/state":
                return self._send(200, overlay_state(), cors=True)
            if path == "/api/plays":
                items = []
                for fn in os.listdir(PLAYS):
                    if fn.endswith(".json"):
                        try:
                            items.append(summary(load_play(fn[:-5])))
                        except Exception:
                            pass
                items.sort(key=lambda x: -x["added"])
                return self._send(200, items)
            if path.startswith("/api/play/"):
                p = load_play(path.split("/")[3])
                if other_pc(p):                      # tablet data recorded on another PC: never show it here
                    p = dict(p, tablet=None, tablet_other_pc=True)
                tb = p.get("tablet")
                if tb and tb.get("screen") and len(tb["screen"]) == 2:
                    try:
                        disp = ((tb.get("otd") or {}).get("display"))
                        p = dict(p, tablet=dict(tb, screen=tablet.osu_screen(disp, tuple(tb["screen"]))))
                    except Exception as e:
                        log("screen:", e)
                if p.get("analysis"):
                    try:
                        p = dict(p, patterns=analyze_patterns(p))
                    except Exception as e:
                        log("patterns:", p.get("id"), e)
                return self._send(200, p)
            if path.startswith("/api/audio/"):
                p = load_play(path.split("/")[3])
                m = play_media(p)
                if not m.get("audio"):
                    # stored info may be outdated -> resolve fresh from the .osu
                    _media_cache.pop(p["id"], None)
                    bmp = (p.get("beatmap") or {}).get("path")
                    if bmp and os.path.isfile(bmp):
                        m = _media_cache[p["id"]] = resolve_media(bmp)
                fp = _inside(m["dir"], m["audio"])
                if not (m["audio"] and os.path.isfile(fp)):
                    bmp = (p.get("beatmap") or {}).get("path") or ""
                    folder = os.path.dirname(bmp)
                    try:
                        listing = os.listdir(folder)[:60] if folder and os.path.isdir(folder) else None
                    except OSError as e:
                        listing = repr(e)
                    diag("audio_missing", {"play": p["id"], "beatmap_path": bmp, "path_exists": os.path.isfile(bmp),
                                           "media": m, "header": read_header(bmp) if os.path.isfile(bmp) else None,
                                           "folder_files": listing, "stored": {k: (p.get("beatmap") or {}).get(k) for k in
                                           ("audio", "has_audio", "media_dir", "bg", "has_bg")}})
                if m["audio"] and os.path.isfile(fp):
                    ext = os.path.splitext(fp)[1].lower()
                    ctype = {".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav"}.get(ext) or sniff_type(fp) or "application/octet-stream"
                    return self._send(200, open(fp, "rb").read(), ctype)
                return self._send(404, {"error": "no audio"})
            if path.startswith("/api/mapsnd/"):
                parts = path.split("/")
                p = load_play(parts[3])
                name = urllib.parse.unquote(parts[4]).lower()
                folders = {play_media(p)["dir"], os.path.dirname((p.get("beatmap") or {}).get("path", ""))}
                if re.fullmatch(r"[a-z0-9\-]+", name):
                    for d in folders:
                        if d and os.path.isdir(d):
                            for fn in os.listdir(d):
                                if os.path.splitext(fn)[0].lower() == name and fn.lower().endswith((".wav", ".ogg", ".mp3")):
                                    fp = os.path.join(d, fn)
                                    return self._send(200, open(fp, "rb").read(), mimetypes.guess_type(fp)[0] or "audio/wav")
                return self._send(204, b"", "audio/wav")
            if path.startswith("/api/bg/"):
                p = load_play(path.split("/")[3])
                m = play_media(p)
                fp = _inside(m["dir"], m["bg"])
                if m["bg"] and os.path.isfile(fp):
                    return self._send(200, open(fp, "rb").read(), mimetypes.guess_type(fp)[0] or sniff_type(fp) or "image/jpeg")
                return self._send(404, {"error": "no bg"})
            if path == "/api/events":
                S.last_ui_ping = time.time()
                try:
                    since = int(q.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                with S.lock:
                    ev = [{"id": i, "play": k} for i, k in S.events if i > since]
                    last = S.event_id
                in_shell = q.get("shell", ["0"])[0] == "1"
                if not in_shell and S.cfg["popup"] == "app" and S.shell.state == "ready" and not S.shell.alive():
                    # setup finished while the Edge/Chrome window was open -> switch to the program window
                    S.shell.open("")
                return self._send(200, {"events": ev, "last": last, "pending": len(S.pending),
                                        "build": current_build(), "version": read_version(),
                                        "shell": S.shell.status()})
            if path == "/api/skins":
                return self._send(200, {"skins": skins.list_skins(S.cfg["skins_path"]), "current": S.cfg["skin"]})
            if path.startswith("/api/skin/"):
                parts = [urllib.parse.unquote(x) for x in path.split("/")[3:]]
                name = parts[0]
                if len(parts) >= 2 and parts[1] == "ini":
                    return self._send(200, skins.skin_ini(S.cfg["skins_path"], name))
                if len(parts) >= 3 and parts[1] == "snd":
                    fp = skins.skin_sound(S.cfg["skins_path"], name, parts[2])
                    if not fp:
                        return self._send(204, b"", "audio/wav")
                    return self._send(200, open(fp, "rb").read(), mimetypes.guess_type(fp)[0] or "audio/wav")
                if len(parts) >= 3 and parts[1] == "el":
                    found = skins.skin_element(S.cfg["skins_path"], name, "/".join(parts[2:]))
                    if not found:
                        return self._send(204, b"", "image/png")  # element not in skin -> fallback
                    fp, scale = found
                    return self._raw(open(fp, "rb").read(), mimetypes.guess_type(fp)[0] or "image/png",
                                     {"X-Skin-Scale": str(scale), "Cache-Control": "max-age=60",
                                      "X-Skin-Frames": str(skins.skin_frames(S.cfg["skins_path"], name, "/".join(parts[2:])))})
                return self._send(404, {"error": "not found"})
            if path == "/api/otd/live":
                if q.get("watch", ["1"])[0] == "1":
                    S.otd.heartbeat()
                snap = S.otd.snapshot()
                o = get_otd()
                snap["area"], snap["display"], snap["source"] = o.get("area"), o.get("display"), o.get("source")
                snap["digitizer"] = o.get("digitizer")
                snap["tablet"] = o.get("tablet")
                return self._send(200, snap)
            if path.startswith("/api/pp-tosu/"):
                return self._send(200, attach_tosu(os.path.basename(path), wait=0) or {})
            if path.startswith("/api/pp-official/"):
                return self._send(200, official_pp(os.path.basename(path), force=q.get("force", ["0"])[0] == "1"))
            if path == "/api/danser/settings":
                fp = S.renderer.settings_file()
                if not os.path.isfile(fp):
                    return self._send(200, {"installed": bool(S.renderer.exe()), "settings": None})
                try:
                    return self._send(200, {"installed": True, "settings": json.load(open(fp, encoding="utf-8-sig"))})
                except Exception as e:
                    return self._send(200, {"installed": True, "settings": None, "error": str(e)})
            if path == "/api/otd/view":
                return self._send(200, otd_view())
            if path == "/api/otd/raw":
                return self._send(200, S.otd.raw)
            if path == "/api/tablet":
                return self._send(200, tablet_report())
            if path.startswith("/api/vendor/rosu/"):
                name = path.rsplit("/", 1)[1]
                if name not in ("rosu.js", "rosu_bg.wasm") or not S.rosu.installed():
                    S.rosu.ensure_async()
                    return self._send(404, {"error": "not installed", "state": S.rosu.state})
                ctype = "text/javascript" if name.endswith(".js") else "application/wasm"
                return self._raw(open(S.rosu.path(name), "rb").read(), ctype, {"Cache-Control": "max-age=3600"})
            if path == "/api/vendor/status":
                S.rosu.ensure_async()
                return self._send(200, {"state": S.rosu.state, "error": S.rosu.error})
            if path.startswith("/api/pp-lazer/"):
                pid = os.path.basename(path.split("/")[3])
                return self._send(200, lazer_pp(pid))
            if path == "/api/update":
                return self._send(200, S.updater.public())
            if path == "/api/lazercalc/status":
                S.lazer.ensure_async()
                return self._send(200, {"state": S.lazer.state, "error": S.lazer.error, "version": S.lazer.version()})
            if path.startswith("/api/osu/"):
                p = load_play(path.split("/")[3])
                bp = map_file(p)
                if bp:
                    return self._send(200, open(bp, "rb").read(), "text/plain; charset=utf-8")
                return self._send(404, {"error": "no map"})
            if path == "/api/render/status":
                st = S.renderer.status()
                st.update({"presets": PRESETS, "encoders": list(ENCODERS), "gpu": gpu_vendor(), "video_dir": video_dir(),
                           "last": S.cfg.get("render") or {}})
                return self._send(200, st)
            if path == "/api/status":
                if not S.paths_status.get("rewind_exe"):
                    S.paths_status["rewind_exe"] = rewind_installed()
                S.paths_status["tosu"] = S.tosu.connected
                S.paths_status["otd"] = bool(S.otd.status["connected"]) or bool(tablet.default_settings_path()) \
                    or (bool(S.cfg["otd_path"]) and os.path.isfile(S.cfg["otd_path"]))
                return self._send(200, {"cfg": S.public_cfg(), "paths": S.paths_status, "osu_offsets": osu_offsets(), "index": S.index.status, "index_secs": S.index.secs,
                                        "indexed": len(S.index.by_md5), "data_dir": DATA,
                                        "shell": S.shell.status(),
                                        "tosu_mgr": S.tosu_mgr.status() if getattr(S, "tosu_mgr", None) else None})
            return self._send(404, {"error": "not found"})
        except FileNotFoundError:
            return self._send(404, {"error": "not found"})
        except Exception as e:
            log(traceback.format_exc())
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self._blocked(path, True):
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = -1
        if n < 0 or n > 64 * 1024 * 1024:                 # replays are a few MB - refuse absurd uploads
            return self._send(413, {"error": "too large"})
        body = self.rfile.read(n) if n else b""
        try:
            if path == "/api/import":
                folder = "render" if "folder=render" in (self.path.split("?", 1) + [""])[1] else None
                key = process_replay(None, "import", data=body, quiet=folder == "render", folder=folder)
                if key is None:
                    rep = parse_osr(body, with_frames=False)
                    key = play_key(rep)
                    set_folder(key, render=True if folder == "render" else None, analysis=None if folder == "render" else True)
                return self._send(200, {"id": key})
            if path.startswith("/api/folder/"):
                pid = os.path.basename(path.split("/")[3])
                o = json.loads(body or b"{}")
                return self._send(200, {"ok": set_folder(pid, o.get("render"), o.get("analysis"))})
            if path == "/api/overlay/winpos":
                w = json.loads(body or b"{}")
                S.cfg["overlay_win"] = {k: int(w[k]) for k in ("x", "y", "w", "h") if isinstance(w.get(k), (int, float))}
                S.save_cfg()
                return self._send(200, {"ok": True})
            if path == "/api/settings":
                new = json.loads(body or b"{}")
                if new.get("osu_client_secret") == SECRET.MASK:
                    new.pop("osu_client_secret")         # unchanged - the UI only knows the mask
                for k in DEFAULTS:
                    if k in new:
                        ok, v = _clean_setting(k, new[k])
                        if ok:
                            S.cfg[k] = v
                        else:
                            log(f"settings: ignored invalid value for {k}")
                S.autodetect()
                S.save_cfg()
                S.otd.always = bool(S.cfg.get("otd_hz_always"))
                S.osuapi.configure(S.cfg.get("osu_client_id"), S.cfg.get("osu_client_secret"))
                S.tosu.set_url(S.cfg.get("tosu_url"))
                if "overlay" in new:
                    threading.Thread(target=overlay_apply, daemon=True).start()
                if "tosu_auto" in new and getattr(S, "tosu_mgr", None):
                    if new["tosu_auto"] and S.tosu_mgr.state in ("off", "error"):
                        S.tosu_mgr.start_async()
                    elif not new["tosu_auto"] and S.tosu_mgr.state == "running":
                        threading.Thread(target=S.tosu_mgr.stop, daemon=True).start()
                if set(new) - {"osu_client_id", "osu_client_secret", "tosu_url"} - {"skin", "viewer_fps", "viewer_interp", "tablet_w", "tablet_h", "otd_hz_always", "tab_map", "auto_save_fail", "tosu_auto", "overlay",
                                   "vol_music", "vol_hits", "vol_mute", "audio_offset", "render", "video_dir", "hud", "ui_theme"}:
                    S.watchers_built = False
                else:
                    return self._send(200, {"ok": True})
                threading.Thread(target=lambda: S.index.scan(S.cfg["songs_path"], S.cfg["lazer_path"]),
                                 daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/reindex":
                threading.Thread(target=lambda: S.index.scan(S.cfg["songs_path"], S.cfg["lazer_path"]),
                                 daemon=True).start()
                return self._send(200, {"ok": True})
            if path.startswith("/api/open/"):
                _, _, _, pid, target = path.split("/")
                pid = re.sub(r"[^0-9A-Za-z_-]", "", pid)
                osr = os.path.join(REPLAYS, os.path.basename(pid) + ".osr")
                if target == "osu":
                    if os.name == "nt":
                        os.startfile(osr)  # opens in osu! (file association)
                    return self._send(200, {"ok": True})
                if target == "rewind":
                    if not rewind_installed():
                        return self._send(200, {"ok": False, "installed": False})
                    folder = S.cfg["rewind_folder"]
                    if folder and os.path.isdir(folder):
                        dst = os.path.join(folder, f"osu-review_{pid}_{int(time.time())}.osr")
                        shutil.copyfile(osr, dst)
                        S.ignore_paths.add(os.path.abspath(dst))
                    exe = S.cfg["rewind_exe"]
                    if exe and os.path.isfile(exe):
                        subprocess.Popen([exe])
                    return self._send(200, {"ok": True, "installed": True})
                if target == "folder":
                    if os.name == "nt":
                        subprocess.Popen(["explorer", "/select,", osr])
                    return self._send(200, {"ok": True})
            if path.startswith("/api/pp/"):
                pid = os.path.basename(path.split("/")[3])
                with _save_lock:
                    p = load_play(pid)
                    pp = json.loads(body or b"{}")
                    if not isinstance(pp, dict) or len(body) > 20000:
                        return self._send(400, {"error": "bad pp"})
                    p["pp"] = pp
                    save_play(p)
                return self._send(200, {"ok": True})
            if path == "/api/clientlog/line":
                try:
                    msg = str(json.loads(body or b"{}").get("msg", ""))[:600]
                except Exception:
                    msg = "?"
                log("window:", msg)
                return self._send(200, {"ok": True})
            if path == "/api/clientlog":
                diag("client_" + time.strftime("%H%M%S"), {"client": json.loads(body or b"{}")})
                return self._send(200, {"ok": True})
            if path == "/api/render/hud":
                try:
                    n = S.renderer.hud_feed(body, final=self.headers.get("X-Final") == "1")
                except RuntimeError as e:
                    return self._send(409, {"ok": False, "error": str(e)})
                return self._send(200, {"ok": True, "received": n})
            if path.startswith("/api/render/preview/"):
                pid = os.path.basename(path.split("/")[4])
                opts = json.loads(body or b"{}")
                try:
                    if not S.renderer.exe():
                        j = {}
                        S.renderer.install(j)
                    out, start_ms = S.renderer.preview(start_render(pid, opts, only_cfg=True), float(opts.get("t") or 0))
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
                return self._raw(open(out, "rb").read(), "video/mp4", {"X-Start-Ms": str(start_ms)})
            if path.startswith("/api/render/start/"):
                pid = os.path.basename(path.split("/")[4])
                jid = start_render(pid, json.loads(body or b"{}"))
                return self._send(200, {"ok": True, "job": jid})
            if path == "/api/render/cancel":
                S.renderer.cancel()
                return self._send(200, {"ok": True})
            if path in ("/api/render/open", "/api/render/folder"):
                st = S.renderer.status()
                out = (st.get("job") or {}).get("output")
                if os.name == "nt":
                    if path.endswith("open") and out and os.path.isfile(out):
                        os.startfile(out)
                    elif out and os.path.isfile(out):
                        subprocess.Popen(["explorer", "/select,", out])
                    else:
                        os.makedirs(video_dir(), exist_ok=True)
                        os.startfile(video_dir())
                return self._send(200, {"ok": True})
            if path == "/api/get-rewind":
                webbrowser.open(library.REWIND_DOWNLOAD)
                return self._send(200, {"ok": True})
            if path.startswith("/api/reanalyze/"):
                pid = os.path.basename(path.split("/")[3])
                if not S.index.busy:
                    S.index.scan(S.cfg["songs_path"] if S.cfg["watch_stable"] else "",
                                 S.cfg["lazer_path"] if S.cfg["watch_lazer"] else "", notify=False)
                ok = reanalyze(pid, notify=False)
                return self._send(200, {"ok": ok, "id": pid})
            if path.startswith("/api/delete/"):
                pid = os.path.basename(path.split("/")[3])
                for f in (os.path.join(PLAYS, pid + ".json"), os.path.join(REPLAYS, pid + ".osr")):
                    if os.path.exists(f):
                        os.remove(f)
                return self._send(200, {"ok": True})
            if path.startswith("/api/danser/managed/"):
                # what osu!review itself sets in danser for these Studio settings (to show the real values)
                pid = os.path.basename(path)
                opts = json.loads(body or b"{}")
                opts["danser_over"] = {}
                try:
                    patch = S.renderer._patch(start_render(pid, opts, only_cfg=True))
                except Exception as e:
                    return self._send(200, {"error": str(e)})
                flat = {}

                def walk(o, pre):
                    for k, v in o.items():
                        kp = pre + "." + k if pre else k
                        if isinstance(v, dict):
                            walk(v, kp)
                        elif not isinstance(v, list):
                            flat[kp] = v
                walk(patch, "")
                for k in ("General.OsuSongsDir", "General.OsuSkinsDir", "Recording.OutputDir"):
                    flat.pop(k, None)
                return self._send(200, flat)
            if path == "/api/osuapi/test":
                d = json.loads(body or b"{}")
                api_ = OsuApi()
                api_.configure(d.get("id"), S.cfg.get("osu_client_secret") if d.get("secret") == SECRET.MASK else d.get("secret"))
                try:
                    api_.test()
                    return self._send(200, {"ok": True})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            if path == "/api/ui/open":
                open_ui(json.loads(body or b"{}").get("play"))
                return self._send(200, {"ok": True})
            if path == "/api/shell/poll":
                return self._send(200, S.shell.poll(json.loads(body or b"{}").get("pid")))
            if path == "/api/shell/show":
                S.shell.pending = {"hash": json.loads(body or b"{}").get("hash", "")}
                return self._send(200, {"ok": True})
            if path == "/api/shell/closed":
                S.shell.closed()
                return self._send(200, {"ok": True})
            if path == "/api/update/check":
                return self._send(200, S.updater.check())
            if path == "/api/update/apply":
                ok, msg = S.updater.apply()
                return self._send(200, {"ok": ok, "message": msg})
            if path == "/api/update/page":
                webbrowser.open(S.updater.state.get("page") or "https://github.com/katsenmann/osu-review/releases/latest")
                return self._send(200, {"ok": True})
            if path == "/api/quit":
                self._send(200, {"ok": True})
                quit_app()
                return
            return self._send(404, {"error": "not found"})
        except FileNotFoundError:
            return self._send(404, {"error": "not found"})
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad request"})
        except Exception as e:
            log(traceback.format_exc())
            return self._send(500, {"error": str(e)})


# ---------------------------------------------------------------- live updates
# Web files (web/, version.json) are picked up instantly: the page notices the new build id and
# reloads itself, keeping the open score / replay position. Changed Python files are syntax-checked
# and then the background process restarts itself; the open window reconnects on its own.

def _scan(paths):
    out = {}
    for base in paths:
        if os.path.isfile(base):
            out[base] = os.path.getmtime(base)
            continue
        for root, _d, files in os.walk(base):
            if "__pycache__" in root:
                continue
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    out[fp] = os.path.getmtime(fp)
                except OSError:
                    pass
    return out


WEB_PATHS = [os.path.join(HERE, "web"), os.path.join(HERE, "version.json")]
PY_PATHS = [os.path.join(HERE, "app.py"), os.path.join(HERE, "core")]


def current_build():
    snap = _scan(WEB_PATHS)
    return hashlib.sha1(json.dumps(sorted(snap.items())).encode()).hexdigest()[:10]


def read_version():
    try:
        return json.load(open(os.path.join(HERE, "version.json"), encoding="utf-8"))
    except Exception:
        return {"version": "?", "changes": {}}


def quit_app():
    """Ends osu!review completely (window, tray icon, background part)."""
    log("quit")
    S.shell.quit = True
    if getattr(S, "tosu_mgr", None):
        threading.Thread(target=S.tosu_mgr.stop, daemon=True).start()
    try:
        mini_window(False)
    except Exception:
        pass
    if getattr(S, "tray", None):
        S.tray.remove()
    threading.Timer(1.2, lambda: os._exit(0)).start()


def tray_check_updates():
    s = S.updater.check()
    if s.get("available"):
        S.tray.notify("Update available", f'osu!review v{s["latest"]} is ready – open osu!review to install it.')
        open_ui()
    elif s.get("error") and not s.get("latest"):
        S.tray.notify("Couldn't check for updates", "GitHub can't be reached right now.")
    else:
        S.tray.notify("osu!review is up to date", f'You have the newest version (v{s["current"]}).')


def restart_self():
    log("Python files changed – restarting …")
    if getattr(S, "tosu_mgr", None):
        S.tosu_mgr.stop()
    try:
        mini_window(False)
    except Exception:
        pass
    if getattr(S, "tray", None):
        S.tray.remove()
    args = [sys.executable, os.path.join(HERE, "app.py"), "--no-window", "--restarted"]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, cwd=HERE, creationflags=flags, close_fds=True)
    try:
        S.server.server_close()
    except Exception:
        pass
    os._exit(0)


def update_watch_loop():
    base = _scan(PY_PATHS)
    changed_at = None
    while True:
        time.sleep(1.0)
        try:
            now = _scan(PY_PATHS)
            if now != base:
                if changed_at is None:
                    changed_at = time.time()
                    base_pending = now
                    continue
                if now != base_pending:          # still being written
                    base_pending, changed_at = now, time.time()
                    continue
                if time.time() - changed_at < 1.5:
                    continue
                ok = True
                for fp in now:
                    if fp.endswith(".py"):
                        try:
                            compile(open(fp, encoding="utf-8").read(), fp, "exec")
                        except Exception as e:
                            ok = False
                            log("Update not valid yet, waiting …", fp, e)
                            break
                if ok:
                    restart_self()
                base = now
                changed_at = None
        except Exception:
            log(traceback.format_exc())


class Server(ThreadingHTTPServer):
    # Windows' SO_REUSEADDR lets a second instance bind the same port -> use exclusive bind there
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            import socket as _s
            self.socket.setsockopt(_s.SOL_SOCKET, getattr(_s, "SO_EXCLUSIVEADDRUSE", -5), 1)
        super().server_bind()


def main():
    srv = None
    for attempt in range(40 if "--restarted" in sys.argv else 1):
        try:
            srv = Server(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            time.sleep(0.4)
    if srv is None:
        # already running -> ask that instance to show its window (not after an auto-restart)
        if "--restarted" not in sys.argv:
            try:
                urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/api/ui/open", data=b"{}", method="POST"), timeout=3)
            except Exception:
                open_ui()
        return
    S.server = srv
    S.otd = OTDLive(log)
    S.shell = Shell(HERE, DATA, log)
    S.shell.fallback = lambda: open_ui(_no_shell=True)
    S.tosu = TosuBridge(DATA, log, S.cfg.get("tosu_url") or "http://127.0.0.1:24050")
    S.lzm = LazerMedia(DATA, log)
    threading.Thread(target=clean_render_songs, daemon=True).start()

    def _on_map(live):
        md5 = (live.get("md5") or "").lower()
        mine = S.ovc.for_map(md5) if getattr(S, "ovc", None) else []
        need = live.get("state") in (2, 7) or bool(mine)     # only maps you play(ed) may be fetched
        before = S.lzm.lookup(md5)
        S.lzm.note(live, S.cfg.get("lazer_path") or "", need=need, tosu_url=S.tosu.url)
        if not before and S.lzm.lookup(md5):
            for r in mine:                              # old replays of this map get music + background now
                _media_cache.pop(r.get("id"), None)
    S.tosu.on_map = _on_map
    S.tosu.on_fail = on_failed_play
    S.tosu_mgr = TosuManager(DATA, log)
    if OV.merged(S.cfg.get("overlay")).get("ingame"):
        S.tosu_mgr.env_extra["ENABLE_INGAME_OVERLAY"] = "true"
    if S.cfg.get("tosu_auto", True):
        S.tosu_mgr.start_async()
    S.ovc = OV.PlayCache(PLAYS)
    S.osc = OV.ScoresDb()
    try:
        from core.modsel import ModSelect
        S.modsel = ModSelect(lambda: getattr(getattr(S, "tosu", None), "live", None), log).start()
    except Exception as e:
        log(f"modsel: {e}")
    if any(OV.merged(S.cfg.get("overlay")).get(k) for k in ("window", "ingame")):
        threading.Timer(4.0, overlay_apply).start()
    S.osuapi = OsuApi()
    S.osuapi.configure(S.cfg.get("osu_client_id"), S.cfg.get("osu_client_secret"))
    if S.cfg["popup"] == "app":
        S.shell.ensure()          # one-time setup of the program window, in the background
    S.otd.dump_path = os.path.join(HERE, "diagnostics", "otd_raw.json")
    S.renderer = Renderer(DATA, log, diag_dir=os.path.join(HERE, "diagnostics"))
    S.rosu = RosuVendor(DATA, log)
    S.updater = Updater(HERE, DATA, log, lambda: read_version().get("version") or "0")
    S.tray = None
    if os.name == "nt" and S.cfg.get("tray", True):
        S.tray = Tray(os.path.join(HERE, "web", "icon.ico"), log, on_open=lambda: open_ui(),
                      on_update=tray_check_updates, on_quit=quit_app,
                      tip=f'osu!review {read_version().get("version", "")}'.strip())
        if not S.tray.start():
            S.tray = None
    if S.tray:
        S.updater.on_available = lambda v: S.tray.notify("Update available",
                                                         f"osu!review v{v} is ready – open osu!review and click “Update now”.")
    threading.Thread(target=S.updater.loop, daemon=True).start()
    S.lazer = LazerCalc(DATA, log)
    S.lazer.ensure_async()
    threading.Thread(target=lazer_backfill, daemon=True).start()
    threading.Thread(target=pattern_backfill, daemon=True).start()
    S.rosu.ensure_async()
    S.otd.always = bool(S.cfg.get("otd_hz_always"))
    threading.Thread(target=update_watch_loop, daemon=True).start()
    log(f"{APP_NAME} running on http://127.0.0.1:{PORT}  (data: {DATA})")
    threading.Thread(target=watch_loop, daemon=True).start()
    threading.Thread(target=keep_all_maps, daemon=True).start()
    if "--no-window" not in sys.argv and "--restarted" not in sys.argv:
        threading.Timer(0.5, open_ui).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
