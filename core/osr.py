"""Parser for osu! replay files (.osr) - works for stable and lazer replays."""
import io
import json
import lzma
import struct
import datetime

MOD_NAMES = [
    (1, "NF"), (2, "EZ"), (4, "TD"), (8, "HD"), (16, "HR"), (32, "SD"),
    (64, "DT"), (128, "RX"), (256, "HT"), (512, "NC"), (1024, "FL"),
    (2048, "AT"), (4096, "SO"), (8192, "AP"), (16384, "PF"), (1 << 29, "V2"),
]
MODE_NAMES = {0: "osu!", 1: "taiko", 2: "catch", 3: "mania"}


def mods_to_str(mods):
    names = [n for bit, n in MOD_NAMES if mods & bit]
    if "NC" in names and "DT" in names:
        names.remove("DT")
    if "PF" in names and "SD" in names:
        names.remove("SD")
    return names


class _Reader:
    def __init__(self, data):
        self.b = io.BytesIO(data)

    def byte(self):
        return struct.unpack("<B", self.b.read(1))[0]

    def short(self):
        return struct.unpack("<H", self.b.read(2))[0]

    def int(self):
        return struct.unpack("<i", self.b.read(4))[0]

    def long(self):
        return struct.unpack("<q", self.b.read(8))[0]

    def uleb(self):
        result = shift = 0
        while True:
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    def string(self):
        flag = self.byte()
        if flag == 0x00:
            return ""
        if flag != 0x0B:
            raise ValueError("bad string marker")
        n = self.uleb()
        return self.b.read(n).decode("utf-8", "replace")

    def raw(self, n):
        return self.b.read(n)


def looks_like_osr(head: bytes) -> bool:
    """Quick sniff on the first ~40 bytes (used for lazer's hash-named files)."""
    if len(head) < 40:
        return False
    if head[0] > 3:
        return False
    version = struct.unpack("<i", head[1:5])[0]
    if not (20000000 <= version <= 40000000):
        return False
    return head[5] == 0x0B and head[6] == 32 and all(c in b"0123456789abcdef" for c in head[7:39])


def _unlzma(data, limit=64 << 20):
    """lzma with a size cap - a crafted replay can't unpack into gigabytes and freeze the PC."""
    d = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
    out = d.decompress(data, max_length=limit)
    if not d.eof and d.needs_input is False:
        raise ValueError("replay data too large")
    return out


def parse_osr(data: bytes, with_frames=True):
    r = _Reader(data)
    rep = {}
    rep["mode"] = r.byte()
    rep["version"] = r.int()
    rep["beatmap_md5"] = r.string()
    rep["player"] = r.string()
    rep["replay_md5"] = r.string()
    rep["n300"] = r.short()
    rep["n100"] = r.short()
    rep["n50"] = r.short()
    rep["ngeki"] = r.short()
    rep["nkatu"] = r.short()
    rep["nmiss"] = r.short()
    rep["score"] = r.int()
    rep["max_combo"] = r.short()
    rep["perfect"] = r.byte()
    rep["mods"] = r.int()
    rep["mods_str"] = mods_to_str(rep["mods"])
    rep["life"] = r.string()
    ticks = r.long()
    try:
        ts = datetime.datetime(1, 1, 1) + datetime.timedelta(microseconds=ticks // 10)
        rep["timestamp"] = ts.replace(tzinfo=datetime.timezone.utc).isoformat()
    except Exception:
        rep["timestamp"] = None
    n = r.int()
    compressed = r.raw(n)
    try:
        rep["online_id"] = r.long()
    except Exception:
        rep["online_id"] = 0
    rep["is_lazer"] = rep["version"] >= 30000000
    # lazer appends its own score info (mods with settings, full hit statistics) as LZMA-packed JSON
    rep["lazer_info"] = None
    if rep["is_lazer"]:
        try:
            m = r.int()
            if 0 < m < 10_000_000:
                info = json.loads(_unlzma(r.raw(m), 4 << 20).decode("utf-8"))
                rep["lazer_info"] = {"mods": info.get("mods"), "statistics": info.get("statistics"),
                                     "maximum_statistics": info.get("maximum_statistics")}
        except Exception:
            rep["lazer_info"] = None
    frames = []
    seed = None
    if with_frames and n > 0:
        text = _unlzma(compressed).decode("ascii", "replace")
        t = 0
        for part in text.split(","):
            if not part:
                continue
            bits = part.split("|")
            if len(bits) < 4:
                continue
            w = int(float(bits[0]))
            if w == -12345:
                seed = int(float(bits[3]))
                continue
            x, y, k = float(bits[1]), float(bits[2]), int(float(bits[3]))
            t += w
            frames.append((t, x, y, k))
    rep["frames"] = frames
    rep["seed"] = seed
    acc_div = 300 * (rep["n300"] + rep["n100"] + rep["n50"] + rep["nmiss"])
    rep["accuracy"] = (
        round(100 * (300 * rep["n300"] + 100 * rep["n100"] + 50 * rep["n50"]) / acc_div, 2) if acc_div else 0
    )
    rep["passed"] = None  # filled in by caller if known
    return rep


# ---------- writer (used for tests / demo replays) ----------

def _w_string(s):
    if not s:
        return b"\x00"
    data = s.encode()
    out = bytearray(b"\x0b")
    n = len(data)
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out) + data


def write_osr(rep, frames):
    parts = [struct.pack("<Bi", rep.get("mode", 0), rep.get("version", 20240101))]
    parts.append(_w_string(rep["beatmap_md5"]))
    parts.append(_w_string(rep.get("player", "Player")))
    parts.append(_w_string(rep.get("replay_md5", "0" * 32)))
    parts.append(struct.pack("<6H", rep["n300"], rep["n100"], rep["n50"], 0, 0, rep["nmiss"]))
    parts.append(struct.pack("<iHBi", rep.get("score", 0), rep.get("max_combo", 0), 0, rep.get("mods", 0)))
    parts.append(_w_string(""))
    ticks = int((datetime.datetime.utcnow() - datetime.datetime(1, 1, 1)).total_seconds() * 10_000_000)
    parts.append(struct.pack("<q", ticks))
    prev = 0
    txt = []
    for (t, x, y, k) in frames:
        txt.append(f"{t - prev}|{x}|{y}|{k}")
        prev = t
    txt.append("-12345|0|0|0")
    comp = lzma.compress(",".join(txt).encode(), format=lzma.FORMAT_ALONE)
    parts.append(struct.pack("<i", len(comp)) + comp)
    parts.append(struct.pack("<q", 0))
    return b"".join(parts)
