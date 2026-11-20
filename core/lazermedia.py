"""Song + background for osu!lazer maps.

lazer has no Songs folder: every file sits in <lazer>/files/ under its hash, and only lazer's own
database knows which file is a map's audio or background. tosu knows it for the map that is selected
right now, so whenever a lazer map is selected or played we remember its files here. After that the
replay viewer has music and background for that map - without needing the same map in osu!stable.

Remembered as paths into lazer's file store (nothing copied). Only if tosu reports a path we can't
find on disk, the two files of a map you have plays on are fetched once from tosu and kept in
<data>/lazer_media/<md5>/.
"""
import json
import os
import re
import threading
import urllib.parse
import urllib.request

MAX_FILE = 80 * 1024 * 1024


def sniff_type(path):
    """Content type from the first bytes (lazer's files have no extension)."""
    try:
        with open(path, "rb") as f:
            h = f.read(12)
    except OSError:
        return None
    if h.startswith(b"ID3") or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "audio/mpeg"
    if h.startswith(b"OggS"):
        return "audio/ogg"
    if h.startswith(b"RIFF") and h[8:12] == b"WAVE":
        return "audio/wav"
    if h.startswith(b"\x89PNG"):
        return "image/png"
    if h.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if h.startswith(b"BM"):
        return "image/bmp"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "image/webp"
    return None


class LazerMedia:
    def __init__(self, data_dir, log=None):
        self.file = os.path.join(data_dir, "lazer_media.json")
        self.cache = os.path.join(data_dir, "lazer_media")
        self.log = log or (lambda *a: None)
        self.lock = threading.Lock()
        self.busy = set()
        try:
            self.map = json.load(open(self.file, encoding="utf-8"))
        except Exception:
            self.map = {}

    def _save(self):
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.map, f)
        os.replace(tmp, self.file)

    # ------------------------------------------------------------ lookup
    def lookup(self, md5):
        """-> {"dir", "audio", "bg"} (audio/bg relative to dir, None if missing) or None."""
        e = self.map.get((md5 or "").lower())
        if not e or not e.get("dir") or not os.path.isdir(e["dir"]):
            return None
        a = e.get("audio") if e.get("audio") and os.path.isfile(os.path.join(e["dir"], e["audio"])) else None
        b = e.get("bg") if e.get("bg") and os.path.isfile(os.path.join(e["dir"], e["bg"])) else None
        if not a and not b:
            return None
        return {"dir": e["dir"], "audio": a, "bg": b}

    # ------------------------------------------------------------ learning from tosu
    @staticmethod
    def _is_lazer(live, lazer_dir):
        c = str(live.get("client") or "").lower()
        if "lazer" in c:
            return True
        songs = str((live.get("folders") or {}).get("songs") or "")
        return bool(lazer_dir and songs and os.path.normcase(os.path.abspath(songs)).startswith(
            os.path.normcase(os.path.abspath(lazer_dir))))

    @staticmethod
    def _find(rel, roots):
        """(root, relative path) of a file tosu named, searched in lazer's store."""
        if not rel:
            return None
        rel = str(rel).replace("/", os.sep).replace("\\", os.sep)
        if os.path.isabs(rel) and os.path.isfile(rel):
            for r in roots:
                if r and os.path.normcase(rel).startswith(os.path.normcase(os.path.join(r, ""))):
                    return r, os.path.relpath(rel, r)
            return os.path.dirname(rel), os.path.basename(rel)
        if ".." in rel.split(os.sep):
            return None
        for r in roots:
            if r and os.path.isfile(os.path.join(r, rel)):
                return r, rel
        return None

    def note(self, live, lazer_dir, need=False, tosu_url="http://127.0.0.1:24050"):
        """Called when tosu shows a (new) map. Cheap when the map is already known."""
        md5 = (live.get("md5") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", md5) or not self._is_lazer(live, lazer_dir):
            return
        if self.lookup(md5):
            return
        d = live.get("direct") or {}
        folders = live.get("folders") or {}
        roots = [folders.get("songs"), os.path.join(lazer_dir, "files") if lazer_dir else None,
                 os.path.join(folders.get("game") or "", "files") if folders.get("game") else None]
        roots = [os.path.abspath(r) for r in roots if r and os.path.isdir(r)]
        a = self._find(d.get("beatmapAudio"), roots)
        b = self._find(d.get("beatmapBackground"), roots)
        if a or b:
            base = (a or b)[0]
            entry = {"dir": base, "audio": a[1] if a and a[0] == base else None,
                     "bg": b[1] if b and b[0] == base else None}
            with self.lock:
                self.map[md5] = entry
                self._save()
            self.log(f"lazer media: remembered {'song' if entry['audio'] else ''}{' + ' if entry['audio'] and entry['bg'] else ''}"
                     f"{'background' if entry['bg'] else ''} for {live.get('title') or md5}")
            return
        if need:
            self._fetch(md5, d, live, tosu_url)

    def _fetch(self, md5, d, live, tosu_url):
        """tosu named files we can't see on disk -> get them through tosu's file server (maps you played only)."""
        if md5 in self.busy:
            return
        self.busy.add(md5)
        try:
            out = os.path.join(self.cache, md5)
            got = {}
            for key, rel in (("audio", d.get("beatmapAudio")), ("bg", d.get("beatmapBackground"))):
                if not rel:
                    continue
                url = f"{tosu_url.rstrip('/')}/files/beatmap/" + urllib.parse.quote(str(rel).replace("\\", "/"))
                if not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/", url):
                    return
                try:
                    with urllib.request.urlopen(url, timeout=20) as r:
                        blob = r.read(MAX_FILE + 1)
                except Exception:
                    continue
                if not blob or len(blob) > MAX_FILE:
                    continue
                os.makedirs(out, exist_ok=True)
                fn = key
                with open(os.path.join(out, fn), "wb") as f:
                    f.write(blob)
                got[key] = fn
            if got:
                with self.lock:
                    self.map[md5] = {"dir": out, "audio": got.get("audio"), "bg": got.get("bg")}
                    self._save()
                self.log(f"lazer media: saved song/background of {live.get('title') or md5} via tosu")
        finally:
            self.busy.discard(md5)
