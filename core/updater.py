"""Update check and one-click update from the project's GitHub releases.

check():  asks GitHub for the latest release and compares it with version.json.
apply():  downloads that release's ZIP, verifies it, backs up the current program files and
          replaces them. app.py's own watcher then restarts the program. Scores and settings
          live in the data folder and are never touched.
"""
import io
import json
import os
import re
import shutil
import threading
import time
import urllib.request
import zipfile

REPO = "katsenmann/osu-review"
API = os.environ.get("OSR_UPDATE_API") or f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = os.environ.get("OSR_UPDATE_PREFIX") or f"https://github.com/{REPO}/releases/download/"
PAGE = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY = 6 * 3600
MAX_ZIP = 40 * 1024 * 1024
# only these may be written by an update
TOP_FILES = {"app.py", "window.py", "version.json", "Install.bat", "Start.bat", "Uninstall.bat", "README.txt"}
TOP_DIRS = {"core", "web"}


def parse_version(v):
    """'v1.0.0-beta.2' -> ((1, 0, 0), (0, 'beta', 2)); a final release sorts after its betas."""
    v = (v or "").strip().lstrip("vV")
    main, _, pre = v.partition("-")
    nums = tuple(int(x) for x in re.findall(r"\d+", main)[:3])
    nums = nums + (0,) * (3 - len(nums))
    if not pre:
        return nums, (1, "", 0)
    m = re.match(r"([a-zA-Z]+)\.?(\d+)?", pre)
    return nums, (0, (m.group(1).lower() if m else pre), int(m.group(2) or 0) if m else 0)


def is_newer(latest, current):
    try:
        return parse_version(latest) > parse_version(current)
    except Exception:
        return False


def _get(url, timeout=30, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": "osu-review", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read((limit or MAX_ZIP) + 1)
    if limit and len(data) > limit:
        raise ValueError("download too large")
    return data


class Updater:
    def __init__(self, here, data_dir, log, current_version):
        self.here, self.data, self.log = here, data_dir, log
        self.current = current_version            # callable -> "1.0.0-beta.1"
        self.lock = threading.Lock()
        self.state = {"current": None, "latest": None, "available": False, "notes": "", "page": PAGE,
                      "zip": None, "checked": 0, "error": None, "busy": False, "progress": ""}

    # ------------------------------------------------------------ check
    def check(self):
        st = self.state
        st["current"] = self.current()
        try:
            rel = json.loads(_get(API, limit=2 * 1024 * 1024).decode("utf-8"))
            tag = rel.get("tag_name") or ""
            zips = [a.get("browser_download_url") for a in rel.get("assets") or []
                    if (a.get("name") or "").lower().endswith(".zip")]
            zips = [z for z in zips if z and z.startswith(DOWNLOAD_PREFIX)]
            st.update(latest=tag.lstrip("vV"), notes=(rel.get("body") or "")[:4000], page=rel.get("html_url") or PAGE,
                      zip=zips[0] if zips else None, error=None,
                      available=bool(tag) and is_newer(tag, st["current"]))
        except Exception as e:
            st["error"] = f"{type(e).__name__}: {e}"
        st["checked"] = time.time()
        return self.public()

    def public(self):
        s = dict(self.state)
        s["current"] = self.current()
        s["can_apply"] = bool(s["available"] and s["zip"])
        s.pop("zip", None)
        return s

    def loop(self):
        time.sleep(15)
        while True:
            self.check()
            if self.state["available"]:
                self.log(f'update available: {self.state["latest"]} (installed {self.state["current"]})')
            time.sleep(CHECK_EVERY)

    # ------------------------------------------------------------ apply
    def apply(self):
        """Returns (ok, message). Never leaves the folder half-updated on a verification error."""
        if not self.lock.acquire(blocking=False):
            return False, "An update is already running."
        st = self.state
        st["busy"] = True
        try:
            if not (st["available"] and st["zip"]):
                self.check()
            if not (st["available"] and st["zip"]):
                return False, "No newer version found."
            st["progress"] = "Downloading …"
            raw = _get(st["zip"], timeout=120, limit=MAX_ZIP)
            st["progress"] = "Checking …"
            files = self._read_zip(raw)
            ver = json.loads(files["version.json"].decode("utf-8")).get("version")
            if parse_version(ver) != parse_version(st["latest"]):
                return False, f"The download does not match the release (it is {ver}, expected {st['latest']})."
            if not is_newer(ver, self.current()):
                return False, f"The download is not newer (it is {ver})."
            for name, blob in files.items():
                if name.endswith(".py"):
                    compile(blob.decode("utf-8"), name, "exec")      # refuse a broken build
            st["progress"] = "Installing …"
            before = self.current()
            self._backup(files)
            self._write(files)
            self.log(f"update installed: {before} -> {ver} ({len(files)} files)")
            st.update(available=False, progress="Restarting …")
            return True, ver
        except PermissionError as e:
            self.log("update failed:", e)
            return False, "Can't write to the program folder – move it somewhere you own (e.g. Documents) or update by hand."
        except Exception as e:
            self.log("update failed:", f"{type(e).__name__}: {e}")
            return False, f"{type(e).__name__}: {e}"
        finally:
            st["busy"] = False
            self.lock.release()

    def _read_zip(self, raw):
        z = zipfile.ZipFile(io.BytesIO(raw))
        out, total = {}, 0
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir():
                continue
            parts = name.split("/")
            if len(parts) < 2 or parts[0] != "osu-review":
                continue                                             # only the program folder
            rel = parts[1:]
            if any(p in ("", ".", "..") or ":" in p for p in rel):
                raise ValueError(f"unsafe path in the download: {name}")
            ok = (len(rel) == 1 and rel[0] in TOP_FILES) or (len(rel) == 2 and rel[0] in TOP_DIRS)
            if not ok:
                continue
            total += info.file_size
            if total > 4 * MAX_ZIP:
                raise ValueError("download unpacks too large")
            out["/".join(rel)] = z.read(info)
        for must in ("app.py", "version.json", "web/index.html", "core/analyze.py"):
            if must not in out:
                raise ValueError(f"the download is incomplete ({must} missing)")
        return out

    def _backup(self, files):
        dst = os.path.join(self.data, "backup", f'{self.current()}-{time.strftime("%Y%m%d-%H%M%S")}')
        for name in files:
            src = os.path.join(self.here, *name.split("/"))
            if os.path.isfile(src):
                tgt = os.path.join(dst, *name.split("/"))
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
                shutil.copy2(src, tgt)
        # keep the three newest backups
        root = os.path.join(self.data, "backup")
        old = sorted((d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))),
                     key=lambda d: os.path.getmtime(os.path.join(root, d)))[:-3]
        for d in old:
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)

    def _write(self, files):
        # everything is staged next to its target first, then swapped in; app.py goes last so the
        # restart watcher only ever sees a complete set
        staged = []
        try:
            for name, blob in files.items():
                tgt = os.path.join(self.here, *name.split("/"))
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
                tmp = tgt + ".upd"
                with open(tmp, "wb") as f:
                    f.write(blob)
                staged.append((name, tmp, tgt))
        except Exception:
            for _, tmp, _ in staged:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        staged.sort(key=lambda s: (s[0] == "app.py", s[0] == "version.json", s[0]))
        for _, tmp, tgt in staged:
            os.replace(tmp, tgt)
