"""Song-select overlay: your local plays of the map you have selected in osu!.

The selected map comes from tosu (state 5 = song select). The numbers come from the analysed
plays in <data>/plays - a small cache keeps one line per play so the overlay can ask twice a
second without reading every play file again.

Shown three ways (all optional, set up in Settings -> Overlay):
    - OBS browser source:  http://127.0.0.1:47290/overlay
    - mini window:         a small extra osu!review window (window.py --mini), e.g. on a 2nd monitor
    - in-game:             as a counter in tosu's in-game overlay (copied into tosu's static folder)
"""
import json
import os
import shutil
import struct
import threading

DEFAULT = {
    "preset": "A", "sty": "modern", "ac": "#ff66aa", "bg": "#14101e", "tx": "#ece8f6", "op": 0.95, "ud": "g",
    "rad": 10, "bw": 1, "sz": 1.0, "wd": 250, "ff": "sans", "tt": "osu!review", "hdr": True,
    "np": 8, "cs": "bars", "lay": "col", "glass": False,
    "blocks": [{"k": "best", "on": True}, {"k": "trend", "on": False}, {"k": "cmp", "on": False},
               {"k": "fc", "on": True}, {"k": "form", "on": True}, {"k": "spot", "on": False}, {"k": "table", "on": False},
               {"k": "strip", "on": False}],
    "cmp": "mods",          # mods | any | set  (which plays count as "the same")
    "pos": "auto",          # auto | gap | top | custom
    "px": 50, "py": 20,     # custom position in % of the screen
    "anim": "fade", "only_select": True, "hide_unplayed": False,
    "obs": True, "window": False, "window_top": False, "ingame": False,
}


def merged(cfg):
    o = dict(DEFAULT)
    o.update({k: v for k, v in (cfg or {}).items() if k in DEFAULT})
    # blocks added in later versions show up in older saved settings too
    have = {b.get("k") for b in o.get("blocks") or [] if isinstance(b, dict)}
    o["blocks"] = list(o.get("blocks") or []) + [{"k": b["k"], "on": b["on"] and o.get("preset") != "custom"}
                                                 for b in DEFAULT["blocks"] if b["k"] not in have]
    return o


MODBITS = [(1, "NF"), (2, "EZ"), (8, "HD"), (16, "HR"), (32, "SD"), (64, "DT"), (128, "RX"), (256, "HT"),
           (512, "NC"), (1024, "FL"), (4096, "SO"), (8192, "AP"), (16384, "PF")]


class ScoresDb:
    """osu! stable's own local scores (scores.db) – so plays from before osu!review count too."""

    def __init__(self):
        self.path = None
        self.mtime = None
        self.by_md5 = {}
        self.lock = threading.Lock()

    @staticmethod
    def _parse(data):
        pos = 0

        def rd(fmt):
            nonlocal pos
            v = struct.unpack_from(fmt, data, pos)
            pos += struct.calcsize(fmt)
            return v[0]

        def rs():
            nonlocal pos
            b = data[pos]
            pos += 1
            if b == 0:
                return ""
            n = shift = 0
            while True:
                c = data[pos]
                pos += 1
                n |= (c & 0x7F) << shift
                shift += 7
                if not c & 0x80:
                    break
            v = data[pos:pos + n].decode("utf-8", "replace")
            pos += n
            return v

        out = {}
        rd("<i")
        for _ in range(rd("<i")):
            md5 = rs().lower()
            lst = []
            for _ in range(rd("<i")):
                mode = rd("<b")
                rd("<i")
                rs()
                player = rs()
                rs()
                n300, n100, n50, _geki, _katu, miss = (rd("<h") for _ in range(6))
                score = rd("<i")
                combo = rd("<h")
                rd("<?")
                mods = rd("<i")
                rs()
                ticks = rd("<q")
                rd("<i")
                rd("<q")
                if mods & (1 << 23):
                    rd("<d")
                if mode != 0:
                    continue
                tot = n300 + n100 + n50 + miss
                acc = (300 * n300 + 100 * n100 + 50 * n50) / (3 * tot) if tot else 0
                names = [n for bit, n in MODBITS if mods & bit]
                if "NC" in names and "DT" in names:
                    names.remove("DT")
                if "PF" in names and "SD" in names:
                    names.remove("SD")
                lst.append({"src": "osu", "md5": md5, "player": player, "added": (ticks - 621355968000000000) / 1e7,
                            "mods": "".join(names), "acc": round(acc, 2), "miss": miss, "combo": combo,
                            "score": score, "passed": True, "pp": None, "ur": None, "spots": []})
            if lst:
                out[md5] = lst
        return out

    def for_map(self, stable_path, md5):
        path = os.path.join(stable_path, "scores.db") if stable_path else None
        if not path or not os.path.isfile(path):
            return []
        with self.lock:
            mt = os.path.getmtime(path)
            if path != self.path or mt != self.mtime:
                with open(path, "rb") as f:
                    data = f.read()
                try:
                    self.by_md5 = self._parse(data)
                except Exception:
                    self.by_md5 = {}
                self.path, self.mtime = path, mt
            return list(self.by_md5.get((md5 or "").lower(), []))


def combine(own, osu):
    """osu!review's analysed plays + osu!'s local scores that osu!review doesn't have (matched by mods/acc/misses/combo)."""
    def key(p):
        return ("".join(sorted(m for m in ("DT", "HT", "HR", "EZ", "FL", "HD") if m in str(p.get("mods") or "").upper()
                               .replace("NC", "DT"))), round(p.get("acc") or 0, 1), p.get("miss"), p.get("combo"))
    seen = {key(p) for p in own}
    out = list(own) + [p for p in osu if key(p) not in seen]
    out.sort(key=lambda r: r.get("added") or 0)
    return out


class PlayCache:
    """md5 -> list of small play records, refreshed from the play files when they change."""

    def __init__(self, plays_dir):
        self.dir = plays_dir
        self.files = {}          # fn -> (mtime, record or None)
        self.lock = threading.Lock()

    @staticmethod
    def _record(p):
        r = p.get("replay") or {}
        a = p.get("analysis") or {}
        st = a.get("stats") or {}
        bm = p.get("beatmap") or {}
        pp = p.get("pp")
        pp = pp.get("pp") if isinstance(pp, dict) else pp
        miss = []
        for o in a.get("objects") or []:
            if o.get("res") == "miss" or o.get("reason") == "sbreak":
                miss.append([o.get("t"), o.get("reason"), o.get("err")])
        return {
            "id": p.get("id"), "md5": (r.get("beatmap_md5") or "").lower(), "added": p.get("added"),
            "player": r.get("player"), "mods": r.get("mods_str") or "", "acc": r.get("accuracy"),
            "miss": r.get("nmiss"), "combo": r.get("max_combo"), "passed": r.get("passed"),
            "pp": pp if isinstance(pp, (int, float)) else None, "ur": st.get("ur"), "mean": st.get("mean"),
            "len": bm.get("length"), "title": bm.get("title"), "artist": bm.get("artist"),
            "version": bm.get("version"), "spots": miss[:400],
        }

    def refresh(self):
        with self.lock:
            try:
                names = [f for f in os.listdir(self.dir) if f.endswith(".json")]
            except OSError:
                return
            seen = set()
            for fn in names:
                seen.add(fn)
                path = os.path.join(self.dir, fn)
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                old = self.files.get(fn)
                if old and old[0] == mt:
                    continue
                try:
                    with open(path, encoding="utf-8") as f:
                        rec = self._record(json.load(f))
                except Exception:
                    rec = None
                self.files[fn] = (mt, rec)
            for fn in list(self.files):
                if fn not in seen:
                    del self.files[fn]

    def recent(self, n=30):
        self.refresh()
        out = [r for _, r in self.files.values() if r and r.get("added")]
        out.sort(key=lambda r: r["added"])
        return [{"added": r["added"], "acc": r["acc"], "pp": r["pp"], "miss": r["miss"], "md5": r["md5"],
                 "passed": r["passed"]} for r in out[-n:]]

    def for_map(self, md5):
        self.refresh()
        md5 = (md5 or "").lower()
        out = [r for _, r in self.files.values() if r and r["md5"] == md5]
        out.sort(key=lambda r: r.get("added") or 0)
        return out


def install_tosu_counter(tosu_dir, here):
    """Copies the overlay page into tosu's counter folder so tosu's in-game overlay can show it."""
    static = os.path.join(tosu_dir, "static", "osu-review")
    os.makedirs(static, exist_ok=True)
    page = open(os.path.join(here, "web", "overlay.html"), encoding="utf-8").read()
    page = page.replace("<!--OSR_MODE-->", '<script>window.OSR_MODE="ingame";window.OSR_API="http://127.0.0.1:47290";</script>')
    with open(os.path.join(static, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    meta = ("Usecase: ingame, obs-overlay\nName: osu!review\nVersion: 1.0\nAuthor: Katsenmann\n"
            "CompatibleWith: tosu\nResolution: 380x420\n"
            "Notes: Your local plays of the selected map, from osu!review. Settings are made in osu!review.\n")
    with open(os.path.join(static, "metadata.txt"), "w", encoding="utf-8") as f:
        f.write(meta)
    return static


def remove_tosu_counter(tosu_dir):
    shutil.rmtree(os.path.join(tosu_dir, "static", "osu-review"), ignore_errors=True)
