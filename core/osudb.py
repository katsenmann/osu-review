"""Reads osu! stable's osu!.db to map beatmap MD5 -> .osu path instantly (no hashing needed).

Defensive: any parse problem just returns what was read so far; the MD5 scan of the
Songs folder remains the fallback.
"""
import os
import struct


class _R:
    def __init__(self, data):
        self.d = data
        self.i = 0

    def take(self, n):
        v = self.d[self.i:self.i + n]
        if len(v) < n:
            raise EOFError
        self.i += n
        return v

    def skip(self, n):
        self.i += n
        if self.i > len(self.d):
            raise EOFError

    def byte(self):
        return self.take(1)[0]

    def short(self):
        return struct.unpack("<h", self.take(2))[0]

    def int(self):
        return struct.unpack("<i", self.take(4))[0]

    def long(self):
        return struct.unpack("<q", self.take(8))[0]

    def uleb(self):
        res = shift = 0
        while True:
            b = self.byte()
            res |= (b & 0x7F) << shift
            if not b & 0x80:
                return res
            shift += 7

    def string(self):
        f = self.byte()
        if f == 0:
            return ""
        if f != 0x0B:
            raise ValueError("bad string")
        return self.take(self.uleb()).decode("utf-8", "replace")

    def skip_string(self):
        f = self.byte()
        if f == 0x0B:
            self.skip(self.uleb())
        elif f != 0:
            raise ValueError("bad string")


def read_osudb(path, songs_dir):
    """Returns {md5: absolute .osu path}."""
    out = {}
    try:
        r = _R(open(path, "rb").read())
    except OSError:
        return out
    try:
        version = r.int()
        r.int()          # folder count
        r.byte()         # account unlocked
        r.long()         # unlock date
        r.skip_string()  # player
        count = r.int()
        for _ in range(count):
            start = r.i
            entry_size = r.int() if version < 20191106 else None
            for _s in range(7):              # artist, artist u, title, title u, creator, diff, audio
                r.skip_string()
            md5 = r.string()
            osu_file = r.string()
            r.skip(1 + 2 * 3 + 8)            # status, circles/sliders/spinners, last modified
            r.skip(4 * 4 if version >= 20140609 else 4)  # AR CS HP OD
            r.skip(8)                        # slider velocity
            if version >= 20140609:
                for _m in range(4):          # star ratings per mode: int-(double|float) pairs
                    n = r.int()
                    for _p in range(n):
                        r.skip(1 + 4)        # 0x08 + int
                        t = r.byte()
                        r.skip(8 if t == 0x0D else 4)
            r.skip(4 * 3)                    # drain, total, preview
            n = r.int()
            r.skip(17 * n)                   # timing points
            r.skip(4 * 3 + 4 + 2 + 4 + 1)    # ids, grades, offset, stack leniency, mode
            r.skip_string()                  # source
            r.skip_string()                  # tags
            r.skip(2)                        # online offset
            r.skip_string()                  # font
            r.skip(1 + 8 + 1)                # unplayed, last played, osz2
            folder = r.string()
            r.skip(8 + 5)                    # last checked, 5 bools
            if version < 20140609:
                r.skip(2)
            r.skip(4 + 1)                    # last modification, mania scroll
            if entry_size is not None:
                r.i = start + 4 + entry_size
            if md5 and folder and osu_file:
                out[md5] = os.path.join(songs_dir, folder, osu_file)
        r.int()  # permissions
    except Exception:
        pass
    return out
