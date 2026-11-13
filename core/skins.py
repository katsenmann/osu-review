"""Reads osu! (stable) skin folders: skin.ini + image elements."""
import os
import re
import threading

_cache = {}
_lock = threading.Lock()


def list_skins(skins_dir):
    if not skins_dir or not os.path.isdir(skins_dir):
        return []
    out = []
    for name in sorted(os.listdir(skins_dir), key=str.lower):
        if os.path.isdir(os.path.join(skins_dir, name)):
            out.append(name)
    return out


def _skin_dir(skins_dir, name):
    d = os.path.abspath(os.path.join(skins_dir, name))
    base = os.path.abspath(skins_dir)
    if os.path.dirname(d) != base or not os.path.isdir(d):
        raise FileNotFoundError(name)
    return d


def _file_map(d):
    """lowercased relative path (with /) -> real path. Cached per skin folder."""
    try:
        mt = os.stat(d).st_mtime
    except OSError:
        mt = 0
    with _lock:
        c = _cache.get(d)
        if c and c[0] == mt:
            return c[1]
    m = {}
    for root, _dirs, files in os.walk(d):
        rel_root = os.path.relpath(root, d)
        for fn in files:
            rel = fn if rel_root == "." else os.path.join(rel_root, fn)
            m[rel.replace("\\", "/").lower()] = os.path.join(root, fn)
    with _lock:
        _cache[d] = (mt, m)
    return m


def _read_text(p):
    raw = open(p, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _rgb(v):
    parts = [p.strip() for p in v.split(",")]
    try:
        nums = [max(0, min(255, int(float(x)))) for x in parts[:3]]
        return nums if len(nums) == 3 else None
    except ValueError:
        return None


def skin_ini(skins_dir, name):
    d = _skin_dir(skins_dir, name)
    fm = _file_map(d)
    ini = {"General": {}, "Colours": {}, "Fonts": {}, "combo": []}
    p = fm.get("skin.ini")
    if p:
        section = None
        for line in _read_text(p).splitlines():
            line = line.split("//", 1)[0].strip()
            if not line:
                continue
            m = re.match(r"^\[(\w+)\]$", line)
            if m:
                section = m.group(1)
                continue
            if section in ("General", "Colours", "Fonts") and ":" in line:
                k, v = line.split(":", 1)
                ini[section][k.strip()] = v.strip()
    combos = []
    for i in range(1, 9):
        v = ini["Colours"].get(f"Combo{i}")
        if v and _rgb(v):
            combos.append(_rgb(v))
    ini["combo"] = combos
    for k in ("SliderBorder", "SliderTrackOverride"):
        v = ini["Colours"].get(k)
        ini[k] = _rgb(v) if v else None
    f = ini["Fonts"]
    ini["prefix"] = f.get("HitCirclePrefix", "default").replace("\\", "/").strip("/") or "default"
    try:
        ini["overlap"] = float(f.get("HitCircleOverlap", -2))
    except ValueError:
        ini["overlap"] = -2
    g = ini["General"]
    ini["overlay_above"] = g.get("HitCircleOverlayAboveNumber", g.get("HitCircleOverlayAboveNumer", "1")).strip() != "0"
    ini["ball_tint"] = g.get("AllowSliderBallTint", "0").strip() == "1"
    flag = lambda key, default: g.get(key, default).strip() != "0"
    ini["ball_flip"] = flag("SliderBallFlip", "1")
    ini["cursor_rotate"] = flag("CursorRotate", "1")
    ini["cursor_expand"] = flag("CursorExpand", "1")
    ini["cursor_centre"] = flag("CursorCentre", "1")
    ini["burst_random"] = g.get("ComboBurstRandom", "0").strip() == "1"
    ini["trail_rotate"] = flag("CursorTrailRotate", "0") if "CursorTrailRotate" in g else ini["cursor_rotate"]
    try:
        ini["anim_fps"] = float(g.get("AnimationFramerate", -1))
    except ValueError:
        ini["anim_fps"] = -1
    try:
        ini["version"] = 99.0 if g.get("Version", "1").strip().lower() == "latest" else float(g.get("Version", "1"))
    except ValueError:
        ini["version"] = 1.0
    ini["name"] = g.get("Name", name)
    ini["author"] = g.get("Author", "")
    return ini


ANIMATED = {"followpoint", "sliderb", "sliderfollowcircle", "hit0", "hit50", "hit100", "hit300", "hit300g", "hit300k", "hit100k", "hitcircleoverlay",
            "sliderstartcircleoverlay", "sliderendcircleoverlay"}


def skin_frames(skins_dir, name, element):
    """How many animation frames (name-0, name-1, ...) the skin has for an element (0 = no animation)."""
    fm = _file_map(_skin_dir(skins_dir, name))
    e = element.replace("\\", "/").strip("/").lower()
    n = 0
    while n < 200 and (f"{e}-{n}.png" in fm or f"{e}-{n}@2x.png" in fm):
        n += 1
    return n


def skin_element(skins_dir, name, element):
    """Returns (path, scale) for an element like 'hitcircle' or 'Fonts/num-3', or None."""
    d = _skin_dir(skins_dir, name)
    fm = _file_map(d)
    e = element.replace("\\", "/").strip("/").lower()
    if ".." in e.split("/"):
        return None
    still = ((f"{e}@2x.png", 2), (f"{e}.png", 1))
    anim = ((f"{e}-0@2x.png", 2), (f"{e}-0.png", 1))
    # osu! prefers an animation (name-0, name-1, ...) over the single picture for these elements
    order = anim + still if e in ANIMATED else still + anim
    for cand, scale in order + ((f"{e}@2x.jpg", 2), (f"{e}.jpg", 1)):
        p = fm.get(cand)
        if p and os.path.getsize(p) > 0:
            return p, scale
    return None


def skin_sound(skins_dir, name, sound):
    """Returns path of a hitsound like 'normal-hitnormal' (wav/ogg/mp3) in the skin, or None."""
    d = _skin_dir(skins_dir, name)
    fm = _file_map(d)
    e = sound.replace("\\", "/").strip("/").lower()
    if "/" in e or ".." in e:
        return None
    for ext in (".wav", ".ogg", ".mp3"):
        p = fm.get(e + ext)
        if p and os.path.getsize(p) > 0:
            return p
    return None
