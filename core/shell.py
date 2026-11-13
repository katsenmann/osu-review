"""Native program window: sets up its own runtime once and starts/raises window.py.

Windows: a private Python 3.12 (embeddable, ~11 MB) + pywebview in <data>/runtime - the user's
own Python is not touched (pywebview's .NET bridge does not exist yet for very new Pythons).
macOS / Linux: the current Python, pywebview is installed for the user if missing.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

PY_EMBED = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
GET_PIP = "https://bootstrap.pypa.io/get-pip.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Shell:
    def __init__(self, here, data, log):
        self.here, self.log = here, log
        self.rt = os.path.join(data, "runtime")
        self.state = "unknown"       # unknown | setup | ready | failed
        self.msg = ""
        self.proc = None
        self.last_poll = 0.0
        self.pending = None          # {"hash": "..."} -> window should come to the front
        self.quit = False
        self.fail_count = 0
        self.fallback = None         # called when the program window cannot start (opens the old window)
        self._lock = threading.Lock()
        self.state = "ready" if self._python() else "unknown"

    # ---------------------------------------------------------------- runtime
    def _python(self):
        """Interpreter that can run window.py, or None."""
        if os.name == "nt":
            # the installer ships its own Python with pywebview next to the program
            own = os.path.join(self.here, "python", "pythonw.exe")
            if os.path.isfile(own) and os.path.isdir(os.path.join(self.here, "python", "Lib", "site-packages", "webview")):
                return own
            exe = os.path.join(self.rt, "py312", "pythonw.exe")
            ok = os.path.isfile(exe) and os.path.isdir(os.path.join(self.rt, "py312", "Lib", "site-packages", "webview"))
            return exe if ok else None
        try:
            import importlib.util
            if importlib.util.find_spec("webview"):
                return sys.executable
        except Exception:
            pass
        return None

    def ensure(self):
        """Starts the one-time setup in the background (no-op when ready or already running)."""
        with self._lock:
            if self.state in ("ready", "setup"):
                return self.state
            if self.state == "failed" and self.fail_count >= 2:
                return self.state
            self.state = "setup"
        threading.Thread(target=self._setup, daemon=True).start()
        return "setup"

    def _run(self, args, timeout=600):
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW, cwd=self.rt)
        if r.returncode != 0:
            lines = (r.stderr or r.stdout or "").strip().splitlines()
            raise RuntimeError(lines[-1] if lines else "exit %d" % r.returncode)
        return r

    def _setup(self):
        try:
            os.makedirs(self.rt, exist_ok=True)
            if os.name == "nt":
                py_dir = os.path.join(self.rt, "py312")
                if not os.path.isfile(os.path.join(py_dir, "python.exe")):
                    self.msg = "Downloading Python for the app window …"
                    self.log("App window: downloading a private Python runtime (once, ~11 MB)")
                    z = os.path.join(self.rt, "py.zip")
                    urllib.request.urlretrieve(PY_EMBED, z)
                    shutil.rmtree(py_dir, ignore_errors=True)
                    with zipfile.ZipFile(z) as f:
                        f.extractall(py_dir)
                    os.remove(z)
                    for fn in os.listdir(py_dir):           # let pip / site-packages work
                        if fn.endswith("._pth"):
                            p = os.path.join(py_dir, fn)
                            lines = open(p, encoding="utf-8").read().splitlines()
                            lines = [("import site" if l.strip() == "#import site" else l) for l in lines]
                            if "import site" not in lines:
                                lines.append("import site")
                            if "Lib\\site-packages" not in lines:
                                lines.insert(1, "Lib\\site-packages")
                            open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
                py = os.path.join(py_dir, "python.exe")
                if not os.path.isdir(os.path.join(py_dir, "Lib", "site-packages", "pip")):
                    self.msg = "Setting up the package manager …"
                    gp = os.path.join(self.rt, "get-pip.py")
                    urllib.request.urlretrieve(GET_PIP, gp)
                    self._run([py, gp, "--no-warn-script-location", "--disable-pip-version-check"])
                self.msg = "Installing the window module (pywebview) …"
                pip = [py, "-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check"]
                # some pywebview helpers only ship as source -> need setuptools/wheel to build
                self._run(pip + ["setuptools", "wheel"])
                self._run(pip + ["pywebview>=5,<7"])
            else:
                self.msg = "Installing the window module (pywebview) …"
                self._run([sys.executable, "-m", "pip", "install", "--user", "pywebview>=5,<7"])
            if not self._python():
                raise RuntimeError("pywebview not found after installation")
            self.state, self.msg = "ready", ""
            self.log("App window: ready")
        except Exception as e:
            self.fail_count += 1
            self.state, self.msg = "failed", str(e)[:300]
            self.log(f"App window: setup failed ({e}) – using the Edge/Chrome window for now")

    # ---------------------------------------------------------------- window
    def alive(self):
        if self.proc and self.proc.poll() is None:
            return True
        return time.time() - self.last_poll < 3

    def open(self, hash_=""):
        """True when the native window shows (or will show) the app."""
        if self.alive():
            self.pending = {"hash": hash_}
            return True
        py = self._python()
        if not py or self.state == "failed":
            return False
        args = [py, os.path.join(self.here, "window.py")] + ([hash_] if hash_ else [])
        self.quit = False
        self.proc = subprocess.Popen(args, cwd=self.here, creationflags=NO_WINDOW)
        threading.Thread(target=self._watch, args=(self.proc,), daemon=True).start()
        return True

    def _watch(self, proc):
        t0 = time.time()
        self.user_closed = False
        code = proc.wait()
        if self.proc is proc:
            self.proc = None
        ran = time.time() - t0
        if self.user_closed or self.quit or code == 0:
            if ran >= 15:
                self.log(f"App window closed (ran {ran / 60:.1f} min)")
        else:
            # the window went away without being closed: a crash (WebView2 / .NET / Python)
            self.log(f"App window crashed: exit code {code} ({self._code_name(code)}) after {ran / 60:.1f} min – details in window.log")
            now = time.time()
            self.crashes = [t for t in getattr(self, "crashes", []) if now - t < 600] + [now]
            if ran >= 15 and len(self.crashes) <= 3:
                self.log("App window: reopening after the crash")
                threading.Timer(1.5, lambda: self.open("")).start()
            elif ran >= 15:
                self.log("App window: crashed 3 times in 10 minutes – not reopening automatically")
        if code not in (0, None) and ran < 15:
            self.fail_count += 1
            why = "the Windows WebView2 runtime is missing" if code == 4 else f"Code {code}, details in window.log"
            self.log(f"App window does not start ({why}) – using the Edge/Chrome window")
            self.state, self.msg = "failed", why
            if self.fallback:
                try:
                    self.fallback()
                except Exception:
                    pass

    def poll(self, pid):
        self.last_poll = time.time()
        out = {"show": False, "quit": self.quit}
        if self.pending is not None:
            out.update(show=True, hash=self.pending.get("hash", ""))
            self.pending = None
        return out

    @staticmethod
    def _code_name(code):
        if code is None:
            return "unknown"
        c = code & 0xFFFFFFFF
        names = {0xC0000005: "access violation", 0xC0000409: "stack buffer overrun / fail-fast", 0xC00000FD: "stack overflow",
                 0xE0434352: ".NET exception", 0xC0000374: "heap corruption", 0x80000003: "breakpoint", 0xC0000017: "out of memory",
                 1: "Python error", 3: "Python error (see window.log)", 4: "WebView2 missing"}
        return names.get(c, hex(c) if c > 255 else str(code))

    def closed(self):
        self.user_closed = True
        self.last_poll = 0.0
        self.proc = None

    def status(self):
        return {"state": self.state, "msg": self.msg, "open": self.alive()}
