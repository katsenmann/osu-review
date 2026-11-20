"""Bundled tosu: downloads tosu (https://github.com/tosuapp/tosu) once and runs it in the background.

tosu reads osu! while you play. osu!review uses it for the pp of osu!lazer's calculator and to notice
when you fail a map (auto-save of failed plays). With this the user doesn't have to install anything:
    - first start: download the newest tosu-windows-*.zip from tosu's GitHub releases into
      <data>/tools/tosu (about 30-60 MB), checked for updates every few days
    - tosu.env is written so that tosu runs quietly (no dashboard in the browser, no own updater)
    - started hidden when osu!review starts, restarted if it dies, stopped when osu!review quits
    - if a tosu is already running (port 24050 answers), that one is used and ours stays off
Windows only.
"""
import hashlib
import io
import json
import os
import subprocess
import threading
import time
import urllib.request
import zipfile

API = "https://api.github.com/repos/tosuapp/tosu/releases/latest"
CHECK_EVERY = 3 * 24 * 3600
ENV = """# written by osu!review - tosu runs in the background for it
OPEN_DASHBOARD_ON_STARTUP=false
ENABLE_AUTOUPDATE=false
ENABLE_INGAME_OVERLAY=false
SERVER_IP=127.0.0.1
SERVER_PORT=24050
"""


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "osu-review", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class TosuManager:
    def __init__(self, data_dir, log, port=24050):
        self.dir = os.path.join(data_dir, "tools", "tosu")
        self.meta_file = os.path.join(self.dir, "osu-review.json")
        self.log = log
        self.port = port
        self.proc = None
        self.state = "off"          # off | downloading | running | external | error | disabled
        self.error = None
        self.stopping = False
        self.lock = threading.Lock()
        self.env_extra = {}         # e.g. ENABLE_INGAME_OVERLAY=true when the osu!review counter is used in-game

    # ---------------------------------------------------------------- info
    def meta(self):
        try:
            return json.load(open(self.meta_file, encoding="utf-8"))
        except Exception:
            return {}

    def exe(self):
        p = os.path.join(self.dir, "tosu.exe")
        return p if os.path.isfile(p) else None

    def status(self):
        m = self.meta()
        return {"state": self.state, "version": m.get("version"), "error": self.error,
                "managed": bool(self.proc and self.proc.poll() is None)}

    def _port_open(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/v2", timeout=1.5) as r:
                r.read(64)
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- install / update
    def _install(self, force_check=False):
        m = self.meta()
        if self.exe() and not force_check and time.time() - m.get("checked", 0) < CHECK_EVERY:
            return True
        try:
            rel = json.loads(_get(API, 20))
            tag = rel.get("tag_name") or ""
            asset = next((a for a in rel.get("assets", []) if a.get("name", "").startswith("tosu-windows")
                          and a["name"].endswith(".zip")), None)
            if not asset:
                raise RuntimeError("no Windows download in tosu's latest release")
            url = asset.get("browser_download_url") or ""
            if not url.startswith("https://github.com/tosuapp/tosu/releases/download/"):
                raise RuntimeError("unexpected download address")
            if self.exe() and m.get("version") == tag:
                m["checked"] = time.time()
                self._save_meta(m)
                return True
            self.state = "downloading"
            self.log(f"tosu: downloading {asset['name']} ({asset.get('size', 0) // 1048576} MB) …")
            blob = _get(url, 600)
            dg = asset.get("digest") or ""
            if dg.startswith("sha256:") and hashlib.sha256(blob).hexdigest() != dg.split(":", 1)[1].lower():
                raise RuntimeError("download doesn't match GitHub's checksum")
            was_running = self.proc and self.proc.poll() is None
            if was_running:
                self.stop()
            os.makedirs(self.dir, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for n in z.namelist():
                    if n.endswith("/"):
                        continue
                    rel_path = os.path.normpath(n)
                    if rel_path.startswith("..") or os.path.isabs(rel_path):
                        raise RuntimeError("unsafe path in the tosu download")
                    target = os.path.join(self.dir, rel_path)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target + ".new", "wb") as f:
                        f.write(z.read(n))
                    os.replace(target + ".new", target)
            if not self.exe():
                # some releases keep tosu.exe in a subfolder
                for base, _, files in os.walk(self.dir):
                    if "tosu.exe" in files and base != self.dir:
                        os.replace(os.path.join(base, "tosu.exe"), os.path.join(self.dir, "tosu.exe"))
                        break
            if not self.exe():
                raise RuntimeError("tosu.exe not found in the download")
            self._save_meta({"version": tag, "checked": time.time()})
            self.log(f"tosu {tag} installed")
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            if self.exe():                      # keep using the installed one
                self.log(f"tosu: update check failed ({self.error}) – using the installed one")
                return True
            self.log(f"tosu: install failed – {self.error}")
            self.state = "error"
            return False

    def _save_meta(self, m):
        try:
            os.makedirs(self.dir, exist_ok=True)
            json.dump(m, open(self.meta_file, "w", encoding="utf-8"))
        except OSError:
            pass

    def _write_env(self):
        # tosu keeps its settings in tosu.env next to tosu.exe; ours only sets what keeps it quiet
        p = os.path.join(self.dir, "tosu.env")
        try:
            cur = open(p, encoding="utf-8").read() if os.path.isfile(p) else ""
        except OSError:
            cur = ""
        want = {k: v for k, v in (l.split("=", 1) for l in ENV.splitlines() if "=" in l and not l.startswith("#"))}
        want.update(self.env_extra)
        lines, seen = [], set()
        for l in cur.splitlines():
            k = l.split("=", 1)[0].strip()
            if k in want:
                lines.append(f"{k}={want[k]}")
                seen.add(k)
            else:
                lines.append(l)
        if not cur:
            lines = [f"{k}={v}" for k, v in want.items()]
        else:
            lines += [f"{k}={v}" for k, v in want.items() if k not in seen]
        try:
            open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        except OSError:
            pass

    def set_env(self, key, value):
        """-> True if tosu has to restart for the change"""
        old = self.env_extra.get(key)
        self.env_extra[key] = value
        if not self.exe():
            return False
        before = self._read_env().get(key)
        self._write_env()
        return before != value and old != value

    def _read_env(self):
        p = os.path.join(self.dir, "tosu.env")
        try:
            return dict(l.split("=", 1) for l in open(p, encoding="utf-8").read().splitlines() if "=" in l and not l.startswith("#"))
        except OSError:
            return {}

    # ---------------------------------------------------------------- run
    def start_async(self):
        if os.name != "nt":
            self.state = "disabled"
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.stopping = False
        if self._port_open():
            self.state = "external"
            self.log("tosu: one is already running – using that one")
            return
        if not self._install():
            return
        self._write_env()
        crashes = []
        while not self.stopping:
            if self._port_open() and not (self.proc and self.proc.poll() is None):
                self.state = "external"
                return
            with self.lock:
                try:
                    flags = 0x08000000                          # CREATE_NO_WINDOW
                    self.proc = subprocess.Popen([self.exe()], cwd=self.dir, creationflags=flags,
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL)
                except Exception as e:
                    self.error = f"{type(e).__name__}: {e}"
                    self.state = "error"
                    self.log(f"tosu: couldn't start – {self.error}")
                    return
            self.state = "running"
            self.error = None
            self.log(f"tosu started in the background (pid {self.proc.pid})")
            code = self.proc.wait()
            if self.stopping:
                break
            crashes = [t for t in crashes if time.time() - t < 600] + [time.time()]
            self.log(f"tosu stopped (exit code {code})")
            if len(crashes) >= 3:
                self.state = "error"
                self.error = f"tosu keeps closing (exit code {code})"
                self.log("tosu: closed 3 times in 10 minutes – not restarting it")
                return
            time.sleep(3)
        self.state = "off"

    def stop(self):
        self.stopping = True
        with self.lock:
            p = self.proc
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        self.state = "off"
