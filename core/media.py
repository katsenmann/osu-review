"""Finds the song + background for a beatmap.

lazer stores files under hashed names, so for lazer replays the audio can't be found next to
the .osu. If the same map also exists in the stable Songs folder (same set ID, or same artist +
title), its song and background are used instead.
"""
import os
import re
import threading

_HDR = re.compile(r"^(AudioFilename|Title|Artist|BeatmapSetID|Version)\s*:\s*(.*)$")


def read_header(path):
    """Reads the parts of a .osu needed to identify the song (stops at [HitObjects])."""
    meta = {}
    section = None
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    section = line
                    if section == "[HitObjects]":
                        break
                    continue
                m = _HDR.match(line)
                if m:
                    meta[m.group(1)] = m.group(2).strip()
                elif section == "[Events]" and "bg" not in meta and (line.startswith("0,0,") or line.startswith("Background,")):
                    parts = line.split(",")
                    if len(parts) >= 3:
                        meta["bg"] = parts[2].strip().strip('"')
    except OSError:
        pass
    return meta


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


class StableMedia:
    def __init__(self):
        self.lock = threading.Lock()
        self.sig = None
        self.by_set = {}    # set id -> [(folder, header)]
        self.by_name = {}   # (artist, title) -> [(folder, header)]

    def _ensure(self, stable_paths):
        sig = len(stable_paths)
        with self.lock:
            if self.sig == sig:
                return
            by_set, by_name = {}, {}
            for p in stable_paths:
                h = read_header(p)
                folder = os.path.dirname(p)
                sid = h.get("BeatmapSetID", "")
                if sid and sid not in ("0", "-1"):
                    by_set.setdefault(sid, []).append((folder, h))
                key = (_norm(h.get("Artist")), _norm(h.get("Title")))
                if key[1]:
                    by_name.setdefault(key, []).append((folder, h))
            self.by_set, self.by_name, self.sig = by_set, by_name, sig

    def find(self, stable_paths, meta, audio_name=None, bg_name=None):
        """Returns {"dir", "audio", "bg"} from the stable twin of this map, or None."""
        self._ensure(stable_paths)
        cands = []
        sid = str(meta.get("BeatmapSetID", "")).strip()
        if sid and sid not in ("0", "-1"):
            cands += self.by_set.get(sid, [])
        cands += self.by_name.get((_norm(meta.get("Artist")), _norm(meta.get("Title"))), [])
        best = None
        for folder, h in cands:
            audio = audio_name if audio_name and os.path.isfile(os.path.join(folder, audio_name)) else h.get("AudioFilename")
            if not audio or not os.path.isfile(os.path.join(folder, audio)):
                continue
            bg = bg_name if bg_name and os.path.isfile(os.path.join(folder, bg_name)) else h.get("bg")
            if bg and not os.path.isfile(os.path.join(folder, bg)):
                bg = None
            score = (h.get("Version") == meta.get("Version")) * 2 + (audio == audio_name)
            if best is None or score > best[0]:
                best = (score, {"dir": folder, "audio": audio, "bg": bg})
        return best[1] if best else None
