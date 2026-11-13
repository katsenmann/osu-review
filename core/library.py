"""Finds osu! installs and maps beatmap MD5 hashes to .osu files (stable + lazer)."""
import hashlib
import json
import os
import re
import threading
import time

from .osr import looks_like_osr
from .osudb import read_osudb


def default_stable_path():
    base = os.environ.get("LOCALAPPDATA")
    if base:
        p = os.path.join(base, "osu!")
        if os.path.isdir(p):
            return p
    return ""


def default_lazer_path():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return ""
    p = os.path.join(appdata, "osu")
    ini = os.path.join(p, "storage.ini")
    if os.path.isfile(ini):
        try:
            for line in open(ini, encoding="utf-8", errors="replace"):
                m = re.match(r"\s*FullPath\s*=\s*(.+)", line)
                if m and os.path.isdir(m.group(1).strip()):
                    return m.group(1).strip()
        except OSError:
            pass
    return p if os.path.isdir(p) else ""


REWIND_DOWNLOAD = "https://github.com/abstrakt8/rewind/releases/latest"


def default_rewind_exe():
    """Finds an installed Rewind (electron-builder installs per user to %LOCALAPPDATA%\\Programs)."""
    roots = []
    for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        b = os.environ.get(env)
        if b:
            roots.append(os.path.join(b, "Programs") if env == "LOCALAPPDATA" else b)
    for root in roots:
        try:
            dirs = [d for d in os.listdir(root) if "rewind" in d.lower()]
        except OSError:
            continue
        for d in dirs:
            folder = os.path.join(root, d)
            try:
                exes = [f for f in os.listdir(folder) if f.lower().endswith(".exe") and "rewind" in f.lower()
                        and "uninstall" not in f.lower()]
            except OSError:
                continue
            if exes:
                return os.path.join(folder, sorted(exes, key=len)[0])
    return ""


def stable_songs_dir(stable):
    if not stable:
        return ""
    try:
        for fn in os.listdir(stable):
            if fn.lower().startswith("osu!.") and fn.lower().endswith(".cfg") and fn.lower() != "osu!.cfg":
                for line in open(os.path.join(stable, fn), encoding="utf-8", errors="replace"):
                    m = re.match(r"\s*BeatmapDirectory\s*=\s*(.+)", line)
                    if m:
                        d = m.group(1).strip()
                        d = d if os.path.isabs(d) else os.path.join(stable, d)
                        if os.path.isdir(d):
                            return d
    except OSError:
        pass
    d = os.path.join(stable, "Songs")
    return d if os.path.isdir(d) else ""


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class BeatmapIndex:
    """md5 -> path of .osu. Cached on disk so restarts are fast."""

    def __init__(self, cache_file):
        self.cache_file = cache_file
        self.lock = threading.Lock()
        self.stable = {}   # path -> [size, mtime, md5]
        self.lazer = {}    # hashed file path -> md5 or "" (not an .osu)
        self.by_md5 = {}
        self.osudb = {}      # md5 -> path from stable's osu!.db (instant)
        self.on_done = []    # callbacks after a scan finished
        self.status = "none"
        self.secs = 0
        self.busy = False
        try:
            data = json.load(open(cache_file, encoding="utf-8"))
            self.stable = data.get("stable", {})
            self.lazer = data.get("lazer", {})
        except Exception:
            pass
        self._rebuild_lookup()

    def _rebuild_lookup(self):
        m = {}
        for p, v in self.lazer.items():
            if v:
                m[v] = p
        for p, v in self.stable.items():
            m[v[2]] = p  # stable wins (has background images next to it)
        self.by_md5 = m

    def save(self):
        tmp = self.cache_file + ".tmp"
        with self.lock:
            json.dump({"stable": self.stable, "lazer": self.lazer}, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, self.cache_file)

    def scan(self, songs_dir, lazer_dir, notify=True):
        if self.busy:
            return
        self.busy = True
        try:
            t0 = time.time()
            n_new = 0
            if songs_dir and os.path.isdir(songs_dir):
                self.status = "stable"
                db = os.path.join(os.path.dirname(songs_dir.rstrip("/\\")), "osu!.db")
                if os.path.isfile(db):
                    self.osudb = read_osudb(db, songs_dir)
                seen = set()
                for root, _dirs, files in os.walk(songs_dir):
                    for fn in files:
                        if not fn.lower().endswith(".osu"):
                            continue
                        p = os.path.join(root, fn)
                        seen.add(p)
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        old = self.stable.get(p)
                        if old and old[0] == st.st_size and old[1] == int(st.st_mtime):
                            continue
                        try:
                            self.stable[p] = [st.st_size, int(st.st_mtime), md5_file(p)]
                            n_new += 1
                        except OSError:
                            pass
                for p in list(self.stable):
                    if p not in seen:
                        del self.stable[p]
                self._rebuild_lookup()   # stable maps usable right away, lazer keeps indexing
                if n_new:
                    self.save()
            files_dir = os.path.join(lazer_dir, "files") if lazer_dir else ""
            if files_dir and os.path.isdir(files_dir):
                self.status = "lazer"
                for root, _dirs, files in os.walk(files_dir):
                    for fn in files:
                        p = os.path.join(root, fn)
                        if p in self.lazer:
                            continue
                        v = self._sniff_osu(p)
                        self.lazer[p] = v
                        n_new += 1
                        if v and v not in self.by_md5:
                            self.by_md5[v] = p   # usable immediately
                        if n_new % 20000 == 0:
                            self.save()
            self._rebuild_lookup()
            if n_new:
                self.save()
            self.status = "done"
            self.secs = round(time.time() - t0, 1)
        finally:
            self.busy = False
        for cb in (list(self.on_done) if notify else []):
            try:
                cb()
            except Exception:
                pass

    @staticmethod
    def _sniff_osu(p):
        try:
            with open(p, "rb") as f:
                head = f.read(64)
            h = head.lstrip(b"\xef\xbb\xbf").lstrip()
            if h.startswith(b"osu file format"):
                return md5_file(p)
        except OSError:
            pass
        return ""

    def add_lazer_file(self, p):
        if p not in self.lazer:
            v = self._sniff_osu(p)
            self.lazer[p] = v
            if v and v not in self.by_md5:
                self.by_md5[v] = p

    def find(self, md5):
        p = self.by_md5.get(md5)
        if p and os.path.isfile(p):
            return p
        p = self.osudb.get(md5)
        if p and os.path.isfile(p):
            return p
        return None


class LazerFilesWatcher:
    """Watches lazer's hashed files/ store for new replays (and new .osu files)."""

    def __init__(self, lazer_dir):
        self.files_dir = os.path.join(lazer_dir, "files") if lazer_dir else ""
        self.dir_mtimes = {}
        self.known = set()
        self.primed = False

    def _subdirs(self):
        out = []
        try:
            for a in os.scandir(self.files_dir):
                if a.is_dir():
                    for b in os.scandir(a.path):
                        if b.is_dir():
                            out.append(b.path)
        except OSError:
            pass
        return out

    def poll(self):
        """Returns list of new file paths that look like replays."""
        if not self.files_dir or not os.path.isdir(self.files_dir):
            return [], []
        new_replays, new_other = [], []
        for d in self._subdirs():
            try:
                mt = os.stat(d).st_mtime
            except OSError:
                continue
            if self.dir_mtimes.get(d) == mt:
                continue
            self.dir_mtimes[d] = mt
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for fn in names:
                p = os.path.join(d, fn)
                if p in self.known:
                    continue
                self.known.add(p)
                if not self.primed:
                    continue
                try:
                    with open(p, "rb") as f:
                        head = f.read(48)
                except OSError:
                    self.known.discard(p)
                    continue
                if looks_like_osr(head):
                    new_replays.append(p)
                else:
                    new_other.append(p)
        self.primed = True
        return new_replays, new_other


class FolderWatcher:
    """Watches a plain folder for new *.osr files."""

    def __init__(self, folder):
        self.folder = folder
        self.known = None

    def poll(self):
        if not self.folder or not os.path.isdir(self.folder):
            return []
        try:
            names = {n for n in os.listdir(self.folder) if n.lower().endswith(".osr")}
        except OSError:
            return []
        if self.known is None:
            self.known = names
            return []
        new = names - self.known
        self.known |= new
        return [os.path.join(self.folder, n) for n in sorted(new)]
