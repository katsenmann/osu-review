"""OpenTabletDriver settings + mapping between osu! pixels and tablet millimetres.

OTD stores areas center-anchored:  Width/Height in mm, X/Y = center of the area in mm
(Display: px). Works with the 0.5 (flat keys) and 0.6 (Profiles/OutputMode/Settings)
settings formats by searching the JSON for the right objects.
"""
import json
import math
import os
import sys
import random

# physical digitizer sizes (mm) of common osu! tablets - user can override in settings
KNOWN_TABLETS = [
    ("CTL-472", 152.0, 95.0), ("CTL-672", 216.0, 135.0),
    ("CTL-480", 152.0, 95.0), ("CTH-480", 152.0, 95.0), ("CTL-490", 152.0, 95.0), ("CTH-490", 152.0, 95.0),
    ("CTL-680", 216.0, 135.0), ("CTH-680", 216.0, 135.0), ("CTL-690", 216.0, 135.0), ("CTH-690", 216.0, 135.0),
    ("CTL-4100", 152.0, 95.0), ("CTL-6100", 216.0, 135.0),
    ("PTH-460", 157.0, 98.0), ("PTH-660", 224.0, 148.0), ("PTH-451", 157.0, 98.0), ("PTH-651", 224.0, 140.0),
    ("Star G640", 152.4, 101.6), ("Deco 01", 254.0, 158.75), ("Deco Fun S", 160.0, 100.0),
    ("H430P", 121.9, 76.2), ("H640P", 160.0, 100.0), ("HS64", 160.0, 102.0), ("H420", 106.0, 61.0),
]


def default_settings_path():
    """Where OpenTabletDriver keeps its settings on this system (Windows, Linux, macOS)."""
    cands = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(os.path.join(la, "OpenTabletDriver", "settings.json"))
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    cands.append(os.path.join(xdg, "OpenTabletDriver", "settings.json"))
    cands.append(os.path.join(home, "Library", "Application Support", "OpenTabletDriver", "settings.json"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def _area(d):
    """Normalises an area dict from either format."""
    if not isinstance(d, dict):
        return None
    w = d.get("Width")
    h = d.get("Height")
    x = d.get("X", d.get("XOffset"))
    y = d.get("Y", d.get("YOffset"))
    pos = d.get("Position")
    if isinstance(pos, dict):
        x, y = pos.get("X", x), pos.get("Y", y)
    if w is None or h is None:
        return None
    try:
        return {"w": float(w), "h": float(h), "x": float(x if x is not None else w / 2),
                "y": float(y if y is not None else h / 2), "rot": float(d.get("Rotation", 0) or 0)}
    except (TypeError, ValueError):
        return None


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def read_otd(path):
    if not path or not os.path.isfile(path):
        return {"found": False, "path": path or "", "tablet": None, "area": None, "display": None,
                "mode": None, "filters": []}
    try:
        data = json.load(open(path, encoding="utf-8-sig"))
    except Exception as e:
        return {"found": False, "path": path, "error": str(e), "tablet": None, "area": None,
                "display": None, "mode": None, "filters": []}
    return parse_otd(data, path)


def parse_otd(data, path="", prefer=None):
    """prefer: names of the tablets that are connected right now - their profile wins."""
    res = {"found": True, "path": path or "", "tablet": None, "area": None, "display": None,
           "mode": None, "filters": []}
    profile = data
    profiles = data.get("Profiles") if isinstance(data, dict) else None
    if isinstance(profiles, list) and profiles:
        # prefer a profile in absolute mode
        absolute = [p for p in profiles if "Absolute" in json.dumps(p.get("OutputMode", ""))]
        want = [n for n in (prefer or []) if n]
        profile = (next((p for p in absolute if p.get("Tablet") in want), None)
                   or next((p for p in profiles if p.get("Tablet") in want), None)
                   or (absolute[0] if absolute else profiles[0]))
        res["profiles"] = [p.get("Tablet") for p in profiles]
        res["tablet"] = profile.get("Tablet")
        om = profile.get("OutputMode") or {}
        res["mode"] = (om.get("Path") or "").split(".")[-1] or None
        for s in om.get("Settings", []) if isinstance(om.get("Settings"), list) else []:
            prop = str(s.get("Property", "")).lower()
            if prop == "tablet":
                res["area"] = _area(s.get("Value"))
            elif prop == "display":
                res["display"] = _area(s.get("Value"))
        # newer layouts: profile["AbsoluteModeSettings"]["Tablet"/"Display"]
        if not res["area"] or not res["display"]:
            for d in _walk(profile):
                if not res["area"] and isinstance(d.get("Tablet"), dict):
                    res["area"] = _area(d["Tablet"])
                if not res["display"] and isinstance(d.get("Display"), dict):
                    res["display"] = _area(d["Display"])
        for f in profile.get("Filters", []) or []:
            if isinstance(f, dict) and f.get("Enable", True):
                res["filters"].append((f.get("Path") or "").split(".")[-1])
    else:
        # 0.5 flat format
        d = data
        if "TabletWidth" in d:
            res["area"] = {"w": float(d["TabletWidth"]), "h": float(d["TabletHeight"]),
                           "x": float(d.get("TabletX", d.get("TabletXOffset", 0))),
                           "y": float(d.get("TabletY", d.get("TabletYOffset", 0))),
                           "rot": float(d.get("TabletRotation", 0) or 0)}
        if "DisplayWidth" in d:
            res["display"] = {"w": float(d["DisplayWidth"]), "h": float(d["DisplayHeight"]),
                              "x": float(d.get("DisplayX", d.get("DisplayXOffset", 0))),
                              "y": float(d.get("DisplayY", d.get("DisplayYOffset", 0))), "rot": 0}
        res["mode"] = (d.get("OutputMode") or "").split(".")[-1] or None
        res["filters"] = [(f or "").split(".")[-1] for f in d.get("Filters", []) or []]
    return res


def tablet_size(name, override_w=0, override_h=0):
    if override_w and override_h:
        return float(override_w), float(override_h), "manual"
    if name:
        n = name.lower().replace(" ", "")
        for key, w, h in KNOWN_TABLETS:
            if key.lower().replace(" ", "") in n:
                return w, h, "table"
    return None, None, None


def screen_size():
    if os.name == "nt":
        try:
            import ctypes
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
            w, h = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass
    return 1920, 1080


# ------------------------------------------------------------- mapping osu px -> tablet mm

def osu_screen(disp, size=None):
    """The screen osu! runs on, in OTD's desktop coordinates: [w, h, x, y].

    OTD measures its display area from the top-left corner of the whole desktop, so with a second
    monitor left of / above the main one the main screen does not start at 0,0. We take the monitor
    the display area is mapped to (the main one if the area covers it, else the one under its centre)."""
    mons = monitors()
    pick = None
    if disp and disp.get("w") and disp.get("h"):
        dx0, dy0 = disp["x"] - disp["w"] / 2, disp["y"] - disp["h"] / 2
        dx1, dy1 = dx0 + disp["w"], dy0 + disp["h"]

        def overlap(m):
            return max(0, min(dx1, m["x"] + m["w"]) - max(dx0, m["x"])) * max(0, min(dy1, m["y"] + m["h"]) - max(dy0, m["y"]))
        hit = [m for m in mons if overlap(m) > 0]
        pick = next((m for m in hit if m.get("primary")), None) \
            or next((m for m in hit if m["x"] <= disp["x"] < m["x"] + m["w"] and m["y"] <= disp["y"] < m["y"] + m["h"]), None) \
            or (max(hit, key=overlap) if hit else None)
    if pick is None:
        pick = next((m for m in mons if m.get("primary")), None) or (mons[0] if mons else {"x": 0, "y": 0, "w": 1920, "h": 1080})
    w, h = (size if size and size[0] and size[1] else (pick["w"], pick["h"]))
    return [int(w), int(h), int(pick["x"]), int(pick["y"])]


def make_mapper(otd, screen):
    """Returns f(osu_x, osu_y) -> (tablet_x_mm, tablet_y_mm) or None if not possible.

    osu! (stable & lazer, fullscreen): playfield height = 0.8 * screen height, centered.
    OTD absolute mode: display area (px, centered at X/Y) <- tablet area (mm, centered at X/Y).
    """
    area, disp = otd.get("area"), otd.get("display")
    if not area or not disp or not screen:
        return None
    sw, sh = screen[0], screen[1]
    if len(screen) >= 4:
        ox, oy = screen[2] or 0, screen[3] or 0
    else:                                     # older snapshot without the screen position
        _w, _h, ox, oy = osu_screen(disp, (sw, sh))
    if not sw or not sh or not disp.get("w") or not disp.get("h") or not area.get("w") or not area.get("h"):
        return None
    scale = 0.8 * sh / 384.0
    offx = ox + (sw - 512 * scale) / 2
    offy = oy + (sh - 384 * scale) / 2
    mmx = area["w"] / disp["w"]
    mmy = area["h"] / disp["h"]
    rot = math.radians(area.get("rot", 0) or 0)
    c, s = math.cos(rot), math.sin(rot)

    def f(x, y):
        spx = offx + x * scale
        spy = offy + y * scale
        dx = (spx - disp["x"]) * mmx
        dy = (spy - disp["y"]) * mmy
        # rotate from area space back to tablet space
        rx = dx * c - dy * s
        ry = dx * s + dy * c
        return area["x"] + rx, area["y"] + ry

    f.mm_per_osu = ((scale * mmx) + (scale * mmy)) / 2
    f.screen_bounds_osu = (-(offx - ox) / scale, -(offy - oy) / scale, (sw - (offx - ox)) / scale, (sh - (offy - oy)) / scale)
    return f


def cursor_sample(frames, t0, t1, n=1500):
    pts = [(round(x, 1), round(y, 1)) for (t, x, y, k) in frames if t0 <= t <= t1]
    if len(pts) > n:
        random.seed(1)
        pts = random.sample(pts, n)
    return pts


# ------------------------------------------------------------- full OTD view (mirror of OTD's "Output" tab)

def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _box(v):
    if not isinstance(v, dict):
        return None
    g = lambda *ks: next((v[k] for k in ks if k in v), None)
    w, h = g("Width", "width", "w"), g("Height", "height", "h")
    if w is None or h is None:
        return None
    return {"w": _f(w), "h": _f(h), "x": _f(g("X", "x")), "y": _f(g("Y", "y")), "rot": _f(g("Rotation", "rotation", "rot"))}


def _setting_list(lst):
    out = {}
    for s in lst or []:
        if isinstance(s, dict) and "Property" in s:
            out[str(s["Property"])] = s.get("Value")
    return out


def profile_view(p):
    """One OTD profile -> everything the OTD output tab shows."""
    om = p.get("OutputMode") or {}
    path = om.get("Path") or ""
    mode = path.split(".")[-1] or None
    am = p.get("AbsoluteModeSettings") or {}
    rm = p.get("RelativeModeSettings") or {}
    st = _setting_list(om.get("Settings"))
    disp = _box(am.get("Display")) or _box(st.get("Display"))
    area = _box(am.get("Tablet")) or _box(st.get("Tablet"))
    filters = []
    for f in p.get("Filters") or []:
        if isinstance(f, dict):
            filters.append({"name": (f.get("Path") or "").split(".")[-1], "plugin": (f.get("Path") or "").rsplit(".", 1)[0],
                            "on": bool(f.get("Enable", True)), "settings": _setting_list(f.get("Settings"))})
    bind = p.get("Bindings") or {}
    tip = bind.get("TipButton") if isinstance(bind, dict) else None
    return {
        "tablet": p.get("Tablet"), "mode": mode, "mode_path": path,
        "relative": "Relative" in path,
        "display": disp, "area": area,
        "clipping": am.get("EnableClipping"), "area_limiting": am.get("EnableAreaLimiting"),
        "lock_aspect": am.get("LockAspectRatio"),
        "rel": {"xs": rm.get("XSensitivity"), "ys": rm.get("YSensitivity"), "rot": rm.get("RelativeRotation"),
                "reset": rm.get("ResetTime")} if rm else None,
        "filters": filters,
        "tip": ((tip or {}).get("Path") or "").split(".")[-1] if isinstance(tip, dict) else None,
        "tip_pressure": bind.get("TipActivationThreshold") if isinstance(bind, dict) else None,
    }


_mon_cache = [0.0, None]


def monitors():
    """All screens in desktop coordinates (like OTD's display area editor). Cached for a few seconds."""
    import time
    if _mon_cache[1] is not None and time.time() - _mon_cache[0] < 5:
        return _mon_cache[1]
    _mon_cache[0], _mon_cache[1] = time.time(), _monitors()
    return _mon_cache[1]


def _monitors():
    out = []
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

            class MI(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

            CB = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

            get_info = ctypes.windll.user32.GetMonitorInfoW
            get_info.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MI)]

            def cb(hm, _hdc, _r, _lp):
                mi = MI()
                mi.cbSize = ctypes.sizeof(MI)
                if get_info(hm, ctypes.byref(mi)):
                    r = mi.rcMonitor
                    out.append({"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                                "primary": bool(mi.dwFlags & 1), "name": mi.szDevice.replace("\\\\.\\", "")})
                return 1
            ctypes.windll.user32.EnumDisplayMonitors(None, None, CB(cb), 0)
        except Exception:
            out = []
    elif sys.platform.startswith("linux"):
        try:
            import re
            import subprocess
            txt = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=3).stdout
            for m in re.finditer(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)", txt, re.M):
                out.append({"name": m.group(1), "primary": bool(m.group(2)), "w": int(m.group(3)), "h": int(m.group(4)),
                            "x": int(m.group(5)), "y": int(m.group(6))})
        except Exception:
            out = []
    if not out:
        w, h = screen_size()
        out = [{"x": 0, "y": 0, "w": w, "h": h, "primary": True, "name": "Display"}]
    # OTD measures the display area from the top-left corner of the whole desktop
    mx, my = min(m["x"] for m in out), min(m["y"] for m in out)
    for m in out:
        m["x"] -= mx
        m["y"] -= my
    return out


def otd_view(settings, tablets, connected, info=None, source="live", path=""):
    """Everything for the mirrored OTD view. tablets: parsed digitizer specs of connected tablets."""
    profiles = settings.get("Profiles") if isinstance(settings, dict) else None
    views = []
    if isinstance(profiles, list):
        views = [profile_view(p) for p in profiles if isinstance(p, dict)]
    elif isinstance(settings, dict):            # 0.5 flat settings
        o = parse_otd(settings)
        views = [{"tablet": None, "mode": o.get("mode"), "display": o.get("display"), "area": o.get("area"),
                  "filters": [{"name": n, "on": True, "settings": {}} for n in o.get("filters", [])]}]
    specs = {t.get("name"): t for t in tablets or [] if t.get("name")}
    for v in views:
        v["connected"] = v.get("tablet") in specs
        v["digitizer"] = specs.get(v.get("tablet"))
    # connected tablets first (that is the one in use), then the rest
    views.sort(key=lambda v: (not v["connected"], (v.get("tablet") or "")))
    return {"source": source, "connected": connected, "path": path,
            "version": (info or {}).get("Version") if isinstance(info, dict) else None,
            "os": {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux"),
            "profiles": views, "monitors": monitors(),
            "lock_display": settings.get("LockUsableAreaDisplay") if isinstance(settings, dict) else None,
            "lock_tablet": settings.get("LockUsableAreaTablet") if isinstance(settings, dict) else None}
