"""Renders replays to video with danser-go (https://github.com/Wieku/danser-go).

danser is downloaded automatically (latest GitHub release, Windows build; it ships its own
ffmpeg). Settings are passed with -sPatch so the user's own danser settings stay untouched.
"""
import collections
import io
import json
import zlib
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile

GITHUB_API = "https://api.github.com/repos/Wieku/danser-go/releases/latest"

PRESETS = {
    "yt1080": {"w": 1920, "h": 1080, "fps": 60, "mb": 8},
    "yt1440": {"w": 2560, "h": 1440, "fps": 60, "mb": 8},
    "yt2160": {"w": 3840, "h": 2160, "fps": 60, "mb": 6},
    "hfr1080": {"w": 1920, "h": 1080, "fps": 120, "mb": 0},
    "fast720": {"w": 1280, "h": 720, "fps": 60, "mb": 0},
}

FFMPEG_ENC = {
    "nvenc_h264": ["-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr", "-cq", "19", "-b:v", "0"],
    "nvenc_hevc": ["-c:v", "hevc_nvenc", "-preset", "p7", "-rc", "vbr", "-cq", "21", "-b:v", "0"],
    "x264": ["-c:v", "libx264", "-preset", "faster", "-crf", "16"],
    "amd_h264": ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "18"],
    "intel_h264": ["-c:v", "h264_qsv", "-global_quality", "18"],
}

ENCODERS = {
    "nvenc_h264": ("h264_nvenc", {"RateControl": "cq", "CQ": 20, "Preset": "p7", "Profile": "high"}),
    "nvenc_hevc": ("hevc_nvenc", {"RateControl": "cq", "CQ": 22, "Preset": "p7"}),
    "x264": ("libx264", {"RateControl": "crf", "CRF": 16, "Preset": "faster", "Profile": "high"}),
    "amd_h264": ("h264_amf", {}),
    "intel_h264": ("h264_qsv", {}),
}


def _ua_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "osu-review", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


class Renderer:
    def __init__(self, base_dir, log=print, diag_dir=None):
        self.dir = os.path.join(base_dir, "danser")
        self.diag_dir = diag_dir or base_dir
        self.log = log
        self.lock = threading.Lock()
        self.job = None            # current/last job dict
        self.proc = None
        self.queue = collections.deque()
        self.thread = None

    # ------------------------------------------------------------ install
    def exe(self):
        if not os.path.isdir(self.dir):
            return None
        best = None
        for root, _d, files in os.walk(self.dir):
            for fn in files:
                low = fn.lower()
                if low in ("danser-cli.exe", "danser-cli"):
                    return os.path.join(root, fn)
                if low in ("danser.exe", "danser") and best is None:
                    best = os.path.join(root, fn)
        return best

    def installed_version(self):
        try:
            return open(os.path.join(self.dir, "osu-review-version.txt"), encoding="utf-8").read().strip()
        except OSError:
            return None

    def install(self, job):
        job.update(status="downloading", progress=0, detail="GitHub")
        rel = json.load(_ua_get(GITHUB_API))
        assets = rel.get("assets", [])
        pick = None
        for a in assets:
            n = a.get("name", "").lower()
            if n.endswith(".zip") and "win" in n and "launcher" not in n:
                pick = a
                break
        if not pick:
            raise RuntimeError("No Windows build of danser in the latest release")
        url, total = pick["browser_download_url"], pick.get("size") or 0
        job["detail"] = f'{pick["name"]} ({total / 1e6:.0f} MB)'
        buf = io.BytesIO()
        with _ua_get(url, timeout=60) as r:
            got = 0
            while True:
                if job.get("cancel"):
                    raise RuntimeError("abgebrochen")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                buf.write(chunk)
                got += len(chunk)
                if total:
                    job["progress"] = round(100 * got / total, 1)
        job.update(status="installing", progress=0, detail="")
        tmp = self.dir + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(buf) as z:
            z.extractall(tmp)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.replace(tmp, self.dir)
        with open(os.path.join(self.dir, "osu-review-version.txt"), "w", encoding="utf-8") as f:
            f.write(rel.get("tag_name", "?"))
        self.log("danser installed:", rel.get("tag_name"))

    # ------------------------------------------------------------ jobs
    def submit(self, job):
        job.setdefault("status", "queued")
        job.setdefault("progress", 0)
        job["log"] = collections.deque(maxlen=40)
        job["created"] = time.time()
        with self.lock:
            self.queue.append(job)
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._worker, daemon=True)
                self.thread.start()
        return job

    def cancel(self):
        with self.lock:
            for j in self.queue:
                j["status"] = "cancelled"
            self.queue.clear()
            if self.job:
                self.job["cancel"] = True
            p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def ffmpeg(self):
        for root, _d, files in os.walk(self.dir) if os.path.isdir(self.dir) else []:
            for fn in files:
                if fn.lower() in ("ffmpeg.exe", "ffmpeg"):
                    return os.path.join(root, fn)
        return shutil.which("ffmpeg")

    def video_duration(self, path):
        ff = self.ffmpeg()
        if not ff:
            return None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        r = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True, errors="replace",
                           creationflags=flags)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if not m:
            return None
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    # ------------------------------------------------------------ HUD compositing
    def hud_feed(self, data, final=False):
        """Receives a batch of HUD frames from the page and pipes them to ffmpeg as raw RGBA.
        Entry: [4 byte length][zlib-compressed RGBA]; length 0 = same picture as the frame before.
        (danser's ffmpeg has no PNG support, raw video is the only thing it can read.)"""
        job = self.job
        if not job or job.get("status") != "hud":
            raise RuntimeError("no HUD job active")
        h = job["hud"]
        h["last_feed"] = time.time()
        if h.get("proc") is None:
            ff = self.ffmpeg()
            if not ff:
                raise RuntimeError("ffmpeg not found")
            cfg = job["cfg"]
            args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", h["base"],
                    "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f'{h["w"]}x{h["h"]}', "-framerate", str(h["fps"]), "-i", "-",
                    "-filter_complex", "[0:v][1:v]overlay=0:0:eof_action=pass:format=auto[v]",
                    "-map", "[v]", "-map", "0:a?"] + h.get("enc_args", FFMPEG_ENC["x264"]) + \
                   ["-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", h["final"]]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            os.makedirs(self.diag_dir, exist_ok=True)
            h["errlog_path"] = os.path.join(self.diag_dir, "ffmpeg_hud.log")
            h["errlog"] = open(h["errlog_path"], "wb")
            h["errlog"].write((" ".join(f'"{a}"' for a in args) + "\n\n").encode("utf-8", "replace"))
            h["errlog"].flush()
            h["proc"] = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=h["errlog"], creationflags=flags)
        if h.get("failed"):
            raise RuntimeError("Burning in the HUD failed: " + h["failed"])
        proc = h["proc"]
        cfg_size = h["w"] * h["h"] * 4
        i = 0
        n = 0
        try:
            while i + 4 <= len(data):
                ln = int.from_bytes(data[i:i + 4], "little")
                if ln:
                    h["raw"] = zlib.decompress(data[i + 4:i + 4 + ln])
                    if len(h["raw"]) != cfg_size:
                        raise RuntimeError(f"HUD frame has the wrong size ({len(h['raw'])} statt {cfg_size})")
                if h.get("raw") is None:
                    h["raw"] = bytes(cfg_size)
                proc.stdin.write(h["raw"])
                i += 4 + ln
                n += 1
        except (BrokenPipeError, OSError):
            time.sleep(0.3)
            h["failed"] = self._ffmpeg_tail(h) or "ffmpeg stopped unexpectedly"
            raise RuntimeError("Burning in the HUD failed: " + h["failed"])
        h["received"] += n
        job["progress"] = round(100 * h["received"] / max(1, h["frames"]), 1)
        if final:
            proc.stdin.close()
            h["final_sent"] = True
        return h["received"]

    def status(self):
        with self.lock:
            j = dict(self.job) if self.job else None
            queued = len(self.queue)
        if j:
            j["log"] = list(j.get("log") or [])[-8:]
            cfg = j.pop("cfg", None) or {}
            if j.get("hud"):
                h = j["hud"]
                j["hud"] = {k: h[k] for k in ("fps", "frames", "w", "h", "start_ms", "received", "offset", "play") if k in h}
            j["want_hud"] = bool(cfg.get("hud"))
        return {"job": j, "queued": queued, "installed": bool(self.exe()), "version": self.installed_version()}

    def _worker(self):
        while True:
            with self.lock:
                if not self.queue:
                    return
                job = self.queue.popleft()
                self.job = job
            try:
                self._run(job)
            except Exception as e:
                job.update(status="error", error=str(e))
                self.log("Render error:", e)
            finally:
                with self.lock:
                    self.proc = None

    def settings_file(self):
        """danser's own settings (written by danser on its first start)."""
        return os.path.join(self.dir, "settings", "default.json")

    def _patch(self, cfg):
        enc_key = cfg.get("encoder", "nvenc_h264")
        enc_name, enc_opts = ENCODERS.get(enc_key, ENCODERS["x264"])
        mb = int(cfg.get("mb", 0))
        rec = {"FrameWidth": cfg["w"], "FrameHeight": cfg["h"], "FPS": cfg["fps"], "Encoder": enc_name,
               "Container": "mp4", "OutputDir": cfg["out_dir"],
               "MotionBlur": {"Enabled": mb > 1, "OversampleMultiplier": max(2, mb),
                              "BlendFrames": max(2, int(mb * 1.5))}}
        if enc_opts:
            rec[enc_name] = enc_opts
        z = cfg.get("dz") or {}
        on = lambda k, d: bool(z.get(k, d))
        own = bool(cfg.get("hud"))          # our HUD replaces danser's own counters
        show = lambda k, d: {"Show": (not own) and on(k, d)}
        d = max(0.0, min(1.0, float(cfg.get("dim", 0.8))))
        db = max(0.0, min(1.0, float(z.get("dim_break", max(0.0, d - 0.3)))))
        cs = max(0.5, min(2.0, float(z.get("cursor_size", 1))))
        patch = {
            "General": {"OsuSongsDir": cfg["songs"], "OsuSkinsDir": cfg["skins"]},
            "Skin": {"CurrentSkin": cfg.get("skin") or "default", "UseColorsFromSkin": True,
                     "UseBeatmapColors": bool(cfg.get("map_colors")),
                     "Cursor": {"UseSkinCursor": on("skin_cursor", True), "Scale": cs}},
            "Cursor": {"CursorSize": 12 * cs, "CursorRipples": on("ripples", False)},
            "Objects": {"DrawApproachCircles": on("approach", True), "DrawComboNumbers": on("combo_nums", True),
                        "DrawFollowPoints": on("follow", True), "ScaleToTheBeat": on("beat", False),
                        "Sliders": {"Snaking": {"In": on("snake_in", True), "Out": on("snake_out", True)},
                                    "SliderMerge": on("merge", False)}},
            "Audio": {"GeneralVolume": 1.0, "MusicVolume": cfg.get("music", 0.6), "SampleVolume": cfg.get("hits", 0.7),
                      "IgnoreBeatmapSamples": not cfg.get("map_hits"), "PlayNightcoreSamples": on("nightcore", True)},
            "Playfield": {"Background": {"LoadStoryboards": on("storyboard", True), "LoadVideos": on("video", True),
                                         "Dim": {"Intro": d, "Normal": d, "Breaks": db},
                                         "Parallax": {"Amount": 0.1 if on("parallax", False) else 0.0}},
                          "SeizureWarning": {"Enabled": False}},
            "Gameplay": {"Score": {"Show": on("score", False)}, "HpBar": {"Show": on("hp", False)},
                         "Mods": {"Show": on("mods", True)}, "StrainGraph": {"Show": on("strain", False)},
                         "AimErrorMeter": {"Show": on("aimerr", False)}, "ScoreBoard": {"Show": False},
                         "Boundaries": {"Enabled": on("borders", False)},
                         "ShowResultsScreen": on("results", True), "ShowHitLighting": on("hitlight", False),
                         "ComboCounter": show("d_combo", True), "PPCounter": dict(show("d_pp", True), ShowInResults=(not own) and on("d_pp", True)),
                         "HitCounter": dict(show("d_hits", True), ShowSliderBreaks=on("d_sb", False)),
                         "HitErrorMeter": dict(show("d_err", True), ShowUnstableRate=on("d_ur", True)),
                         "KeyOverlay": show("d_keys", True)},
            "Recording": rec,
        }
        patch["Playfield"]["Logo"] = {"Enabled": on("logo", False)}      # danser's spinning logo / coin
        # own danser settings from the Studio ("all danser settings") win over everything above
        for path, val in (cfg.get("danser_over") or {}).items():
            node = patch
            keys = [k for k in str(path).split(".") if k]
            if not keys:
                continue
            for k in keys[:-1]:
                if not isinstance(node.get(k), dict):
                    node[k] = {}
                node = node[k]
            node[keys[-1]] = val
        return patch

    def preview(self, cfg, t_ms, secs=4.0):
        """Renders a short clip with danser itself (same settings as the final video) around map time t_ms."""
        with self.lock:
            busy = (self.job and self.job.get("status") not in ("done", "error", "cancelled")) or self.queue \
                or getattr(self, "_previewing", False)
            if busy:
                raise RuntimeError("busy")
            self._previewing = True
        try:
            exe = self.exe()
            if not exe:
                raise RuntimeError("notinstalled")
            out_dir = os.path.join(os.path.dirname(self.dir), "preview")
            os.makedirs(out_dir, exist_ok=True)
            c = dict(cfg)
            scale = 720 / c["h"]
            c.update({"w": int(round(c["w"] * scale / 2) * 2), "h": 720, "fps": min(int(c["fps"]), 60),
                      "out_dir": out_dir, "name": "preview"})
            start = max(0.0, t_ms / 1000 - 1.0)
            args = [exe, f'-replay={c["replay"]}', "-record", "-out=preview", "-noupdatecheck", "-quickstart",
                    f"-start={start:.3f}", f"-end={start + secs:.3f}", "-sPatch=" + json.dumps(self._patch(c))]
            if c.get("skin"):
                args.append(f'-skin={c["skin"]}')
            out = os.path.join(out_dir, "preview.mp4")
            if os.path.exists(out):
                os.remove(out)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            r = subprocess.run(args, cwd=os.path.dirname(exe), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=flags, timeout=180)
            os.makedirs(self.diag_dir, exist_ok=True)
            with open(os.path.join(self.diag_dir, "danser_preview.log"), "w", encoding="utf-8") as f:
                f.write(" ".join(f'"{a}"' for a in args) + "\n\n" + (r.stdout or "") + (r.stderr or ""))
            if r.returncode != 0 or not os.path.isfile(out):
                tail = " | ".join((r.stdout or "").strip().splitlines()[-3:])
                raise RuntimeError(f"danser: {tail}"[:300])
            return out, int(start * 1000)
        finally:
            self._previewing = False

    def _run(self, job):
        if not self.exe():
            self.install(job)
        exe = self.exe()
        if not exe:
            raise RuntimeError("danser was not found")
        cfg = job["cfg"]
        os.makedirs(cfg["out_dir"], exist_ok=True)
        patch = self._patch(cfg)
        base_name = cfg["name"] + (" (base)" if cfg.get("hud") else "")
        args = [exe, f'-replay={cfg["replay"]}', "-record", f'-out={base_name}',
                "-noupdatecheck", "-preciseprogress", "-sPatch=" + json.dumps(patch)]
        if cfg.get("hud"):
            # known start time so the HUD frames line up: video frame 0 = map time start_ms
            args += ["-quickstart", f'-start={cfg["start_ms"] / 1000:.3f}']
        else:
            args.append("-skip")
        if cfg.get("offset"):
            args.append(f'-offset={int(cfg["offset"])}')
        if cfg.get("skin"):
            args.append(f'-skin={cfg["skin"]}')   # -sPatch alone wasn't enough for the skin
        job.update(status="starting", progress=0, detail="")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        p = subprocess.Popen(args, cwd=os.path.dirname(exe), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=flags, bufsize=1, text=True, encoding="utf-8", errors="replace")
        with self.lock:
            self.proc = p
        os.makedirs(self.diag_dir, exist_ok=True)
        dlog = open(os.path.join(self.diag_dir, "danser_last.log"), "w", encoding="utf-8")
        dlog.write(" ".join(f'"{a}"' for a in args) + "\n\n")
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            dlog.write(line + "\n")
            dlog.flush()
            job["log"].append(line)
            low = line.lower()
            m = re.search(r"progress[^0-9]*(\d{1,3})\s*%", low) or (re.search(r"(\d{1,3})%", low) if "record" in low or "render" in low else None)
            if m:
                job["status"] = "rendering"
                job["progress"] = min(100, int(m.group(1)))
                eta = re.search(r"eta[:\s]+([0-9hms:.]+)", low)
                if eta:
                    job["eta"] = eta.group(1)
                fps = re.search(r"fps[:\s]+([0-9.]+)", low)
                if fps:
                    job["fps"] = fps.group(1)
            elif "import" in low or "database" in low or "loading beatmaps" in low or "scanning" in low:
                job["status"] = "importing"
                job["detail"] = line[-120:]
            if job.get("cancel"):
                break
        p.wait()
        dlog.close()
        if job.get("cancel"):
            job["status"] = "cancelled"
            return
        out = os.path.join(cfg["out_dir"], base_name + ".mp4")
        if p.returncode == 0 and os.path.isfile(out) and cfg.get("hud"):
            self._hud_phase(job, out)
        elif p.returncode == 0 and os.path.isfile(out):
            job.update(status="done", progress=100, output=out, size=os.path.getsize(out))
            self.log("Video done:", out)
        else:
            tail = " | ".join(list(job["log"])[-3:])
            raise RuntimeError(f"danser exited with code {p.returncode}: {tail}"[:400])

    def _ffmpeg_tail(self, h):
        try:
            h["errlog"].flush()
        except Exception:
            pass
        try:
            txt = open(h["errlog_path"], "rb").read().decode("utf-8", "replace")
            lines = [l for l in txt.splitlines()[1:] if l.strip()]
            return " | ".join(lines[-4:])[-400:]
        except Exception:
            return ""

    def _preflight(self, base, enc_key):
        """Checks that ffmpeg can read the base video and use the encoder; falls back to libx264."""
        ff = self.ffmpeg()
        if not ff:
            raise RuntimeError("ffmpeg not found (danser folder)")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        notes = []
        for key in [enc_key, "x264"] if enc_key != "x264" else ["x264"]:
            args = [ff, "-hide_banner", "-loglevel", "error", "-t", "0.5", "-i", base,
                    "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "64x64", "-framerate", "60", "-i", "-",
                    "-filter_complex", "[0:v][1:v]overlay=0:0:eof_action=pass:format=auto[v]", "-map", "[v]",
                    "-frames:v", "5"] + \
                   FFMPEG_ENC.get(key, FFMPEG_ENC["x264"]) + ["-pix_fmt", "yuv420p", "-f", "null", "-"]
            try:
                r = subprocess.run(args, input=bytes(64 * 64 * 4 * 6), capture_output=True, creationflags=flags, timeout=45)
                r.stderr = (r.stderr or b"").decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                notes.append(f"{key}: Zeitlimit")
                continue
            if r.returncode == 0:
                return FFMPEG_ENC.get(key, FFMPEG_ENC["x264"]), notes
            notes.append(f"{key}: {r.stderr.strip()[-250:]}")
        raise RuntimeError("ffmpeg test failed: " + " || ".join(notes))

    def _hud_phase(self, job, base):
        cfg = job["cfg"]
        job.update(status="hud", progress=0, detail="")
        try:
            enc_args, notes = self._preflight(base, cfg.get("encoder"))
        except Exception as e:
            plain = os.path.join(cfg["out_dir"], cfg["name"] + " (no HUD).mp4")
            os.replace(base, plain)
            job.update(status="done", progress=100, output=plain, size=os.path.getsize(plain), note=str(e)[:400])
            self.log("HUD pre-check failed:", e)
            return
        if notes:
            self.log("HUD: encoder fallback to x264:", notes)
        dur = self.video_duration(base) or 0
        final = os.path.join(cfg["out_dir"], cfg["name"] + ".mp4")
        hfps = min(int(cfg["fps"]), 60)
        job["hud"] = {"fps": hfps, "frames": int(dur * hfps), "w": cfg["w"], "h": cfg["h"],
                      "start_ms": cfg["start_ms"], "offset": cfg.get("hud_offset", 0), "play": job["play"],
                      "base": base, "final": final, "received": 0, "proc": None, "last_feed": time.time(),
                      "enc_args": enc_args}
        job.update(status="hud", progress=0, detail="")
        h = job["hud"]
        while True:
            time.sleep(0.5)
            if job.get("cancel"):
                if h.get("proc"):
                    h["proc"].kill()
                job["status"] = "cancelled"
                return
            if h.get("final_sent"):
                break
            if h.get("failed"):
                # keep the plain danser video so the render isn't lost
                plain = os.path.join(cfg["out_dir"], cfg["name"] + " (no HUD).mp4")
                try:
                    os.replace(base, plain)
                except OSError:
                    plain = base
                job.update(status="done", progress=100, output=plain, size=os.path.getsize(plain),
                           note="HUD failed: " + h["failed"])
                self.log("Burning in the HUD failed:", h["failed"])
                return
            if time.time() - h["last_feed"] > 90:
                # page closed / not responding -> keep the video without HUD
                if h.get("proc"):
                    h["proc"].kill()
                plain = os.path.join(cfg["out_dir"], cfg["name"] + ".mp4")
                os.replace(base, plain)
                job.update(status="done", progress=100, output=plain, size=os.path.getsize(plain),
                           note="no HUD (the window was closed)")
                return
        proc = h["proc"]
        proc.wait()
        err = self._ffmpeg_tail(h)
        if proc.returncode == 0 and os.path.isfile(final):
            try:
                os.remove(base)
            except OSError:
                pass
            job.update(status="done", progress=100, output=final, size=os.path.getsize(final))
            self.log("Video with HUD done:", final)
        else:
            plain = os.path.join(cfg["out_dir"], cfg["name"] + " (no HUD).mp4")
            try:
                os.replace(base, plain)
            except OSError:
                plain = base
            job.update(status="done", progress=100, output=plain, note=f"HUD failed: {err[-300:]}")
            self.log("HUD ffmpeg error:", proc.returncode, err)
