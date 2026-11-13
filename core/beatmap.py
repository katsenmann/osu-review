"""Minimal .osu beatmap parser (osu!standard focus)."""
import re

from . import sliderpath


def parse_osu(text: str):
    bm = {"general": {}, "metadata": {}, "difficulty": {}, "timing": [], "objects": [], "background": None}
    m0 = re.match(r"\s*\ufeff?osu file format v(\d+)", text)
    bm["format"] = int(m0.group(1)) if m0 else 14
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"^\[(\w+)\]$", line)
        if m:
            section = m.group(1)
            continue
        if section in ("General", "Metadata", "Difficulty"):
            if ":" in line:
                k, v = line.split(":", 1)
                bm[section.lower()][k.strip()] = v.strip()
        elif section == "Events":
            if bm["background"] is None and (line.startswith("0,0,") or line.startswith("Background,")):
                parts = line.split(",")
                if len(parts) >= 3:
                    bm["background"] = parts[2].strip().strip('"')
        elif section == "TimingPoints":
            p = line.split(",")
            if len(p) < 2:
                continue
            try:
                t = float(p[0])
                bl = float(p[1])
            except ValueError:
                continue
            uninherited = (p[6].strip() == "1") if len(p) > 6 else True
            try:
                sset = int(p[3]) if len(p) > 3 else 0
                sidx = int(p[4]) if len(p) > 4 else 0
                svol = int(p[5]) if len(p) > 5 else 100
            except ValueError:
                sset, sidx, svol = 0, 0, 100
            bm["timing"].append((t, bl, uninherited, sset, svol, sidx))
        elif section == "HitObjects":
            p = line.split(",")
            if len(p) < 4:
                continue
            x, y, t, typ = float(p[0]), float(p[1]), int(float(p[2])), int(p[3])
            obj = {"x": x, "y": y, "t": t}
            if typ & 1:
                obj["k"] = "c"
            elif typ & 2:
                obj["k"] = "s"
                curve = p[5].split("|")
                obj["curve"] = curve[0]
                pts = [[x, y]]
                for c in curve[1:]:
                    if ":" in c:
                        a, b = c.split(":")
                        pts.append([float(a), float(b)])
                obj["pts"] = pts
                obj["slides"] = int(p[6]) if len(p) > 6 else 1
                obj["len"] = float(p[7]) if len(p) > 7 else 0.0
            elif typ & 8:
                obj["k"] = "sp"
                obj["end"] = int(float(p[5])) if len(p) > 5 else t
            else:
                continue  # mania holds etc.
            obj["nc"] = bool(typ & 4)
            try:
                obj["hs"] = int(p[4])
            except (ValueError, IndexError):
                obj["hs"] = 0
            obj["cskip"] = (typ >> 4) & 7
            # hitSample "normalSet:additionSet:index:volume:file" (circles: field 5, sliders: field 10)
            hsamp = p[5] if obj["k"] == "c" and len(p) > 5 else (p[10] if obj["k"] == "s" and len(p) > 10 else "")
            try:
                hb = [int(v) for v in hsamp.split(":")[:2]]
                obj["_hset"] = (hb + [0, 0])[:2]
            except ValueError:
                obj["_hset"] = [0, 0]
            if obj["k"] == "s":
                # per slider node (head, every repeat, tail): own hitsound + sample sets
                try:
                    obj["_es"] = [int(v) for v in p[8].split("|")] if len(p) > 8 and p[8] else None
                except ValueError:
                    obj["_es"] = None
                try:
                    obj["_eset"] = [[int(a) for a in v.split(":")[:2]] for v in p[9].split("|")] if len(p) > 9 and p[9] else None
                except ValueError:
                    obj["_eset"] = None
            bm["objects"].append(obj)

    d = bm["difficulty"]
    cs = float(d.get("CircleSize", 4))
    od = float(d.get("OverallDifficulty", 5))
    ar = float(d.get("ApproachRate", od))
    hp = float(d.get("HPDrainRate", 5))
    sm = float(d.get("SliderMultiplier", 1.4))
    bm["cs"], bm["od"], bm["ar"], bm["hp"], bm["sm"] = cs, od, ar, hp, sm
    try:
        bm["tick_rate"] = float(d.get("SliderTickRate", 1)) or 1.0
    except ValueError:
        bm["tick_rate"] = 1.0
    bm["mode"] = int(bm["general"].get("Mode", 0))
    bm["audio"] = bm["general"].get("AudioFilename", "").strip()
    _slider_durations(bm)
    return bm


SAMPLE_SETS = {1: "normal", 2: "soft", 3: "drum"}


def _timing_at(timing, t):
    """last timing point (inherited or not) at or before t - that's where sample set / volume come from"""
    lo, hi = 0, len(timing)
    while lo < hi:
        m = (lo + hi) // 2
        if timing[m][0] <= t + 1:
            lo = m + 1
        else:
            hi = m
    return timing[lo - 1] if lo else None


def _slider_durations(bm):
    timing = sorted(bm["timing"], key=lambda x: x[0])
    beat = 500.0
    sv = 1.0
    i = 0
    default_set = {"normal": 1, "soft": 2, "drum": 3}.get(bm["general"].get("SampleSet", "Normal").lower(), 1)
    sset, svol, sidx = default_set, 100, 0
    for o in bm["objects"]:
        while i < len(timing) and timing[i][0] <= o["t"]:
            t, bl, unh, ts, tv, ti = timing[i]
            sset = ts or default_set
            svol = tv
            sidx = ti
            if unh and bl > 0:
                beat = bl
                sv = 1.0
            elif bl < 0:
                sv = max(0.1, min(10.0, -100.0 / bl))
            i += 1
        hn, ha = o.get("_hset") or [0, 0]
        o["ss"] = SAMPLE_SETS.get(hn or sset, "normal")
        o["as"] = SAMPLE_SETS.get(ha or hn or sset, o["ss"])   # whistle/finish/clap use the addition set
        o["sv"] = svol
        o["si"] = sidx      # custom sample index of the map (0 = skin sounds)
        if o["k"] == "s":
            px_per_beat = bm["sm"] * 100 * sv
            dur = o["len"] / px_per_beat * beat if px_per_beat else 0
            o["end"] = int(o["t"] + dur * o["slides"])
            o["_span"] = dur                                   # ms for one pass over the body
            # sounds of every node: head (0), each reverse arrow, tail (slides) - sets from the timing point there
            nodes = []
            for k in range(o["slides"] + 1):
                nt = o["t"] + dur * k
                tp = _timing_at(timing, nt)
                tset, tvol, tidx = (tp[3] or default_set, tp[4], tp[5]) if tp else (default_set, 100, 0)
                es = o.get("_es")
                bits = es[k] if es and k < len(es) else o.get("hs", 0)
                en, ea = (o.get("_eset")[k] if o.get("_eset") and k < len(o["_eset"]) else [0, 0])
                en = en or hn or tset
                nodes.append({"t": round(nt), "hs": bits, "ss": SAMPLE_SETS.get(en, "normal"),
                              "as": SAMPLE_SETS.get(ea or ha or en, "normal"), "sv": tvol, "si": tidx})
            o["nodes"] = nodes
            o["_vel"] = px_per_beat / beat if beat else 0      # osu!px per ms
            # distance between slider ticks (old maps ignore the green-line speed here)
            o["_tick"] = (px_per_beat if bm.get("format", 14) >= 8 else bm["sm"] * 100) / bm["tick_rate"]
        elif o["k"] == "c":
            o["end"] = o["t"]


def apply_mods(bm, mods):
    """Returns adjusted (cs, od, ar) and flips objects for HR."""
    cs, od, ar = bm["cs"], bm["od"], bm["ar"]
    if mods & 16:  # HR
        cs = min(10, cs * 1.3)
        od = min(10, od * 1.4)
        ar = min(10, ar * 1.4)
        for o in bm["objects"]:
            o["y"] = 384 - o["y"]
            if "pts" in o:
                o["pts"] = [[a, 384 - b] for a, b in o["pts"]]
    if mods & 2:  # EZ
        cs, od, ar = cs / 2, od / 2, ar / 2
    return cs, od, ar


def compute_paths(bm):
    """Real slider bodies + the moments a slider gives combo (ticks, repeats, end). Call after apply_mods."""
    for o in bm["objects"]:
        if o["k"] != "s" or "_p" in o:
            continue
        pts, cum = sliderpath.path(o)
        o["_p"], o["_c"] = pts, cum
        length = cum[-1]
        span = o.get("_span", 0.0)
        slides = max(1, o.get("slides", 1))
        ev = []      # (time, kind, path progress)  kind: t = tick, r = repeat, e = end
        tick = min(max(o.get("_tick", 0.0), 0.0), length)
        min_end = o.get("_vel", 0.0) * 10
        if tick > 0 and length > 0:
            for sp in range(slides):
                st = o["t"] + sp * span
                rev = sp % 2 == 1
                ticks = []
                d = tick
                while d <= length:
                    if d >= length - min_end:
                        break
                    pr = d / length
                    ticks.append((st + (1 - pr if rev else pr) * span, "t", pr))
                    d += tick
                if rev:
                    ticks.reverse()
                ev.extend(ticks)
                if sp < slides - 1:
                    ev.append((st + span, "r", 0.0 if rev else 1.0))
        total = span * slides
        fin = o["t"] + (slides - 1) * span
        last_t = max(o["t"] + total / 2, fin + span - 36)        # osu!'s "legacy last tick"
        pr = (last_t - fin) / span if span else 1.0
        if slides % 2 == 0:
            pr = 1 - pr
        ev.append((last_t, "e", pr))
        o["_ev"] = ev
        o["_end"] = o["t"] + total
        e = pts[-1] if slides % 2 == 1 else pts[0]
        o["ex"], o["ey"] = round(e[0], 2), round(e[1], 2)


def ball_at(o, t):
    """Slider ball position at map time t."""
    span = o.get("_span", 0.0)
    if span <= 0:
        return o["_p"][0]
    q = (t - o["t"]) / span
    q = max(0.0, min(float(o.get("slides", 1)), q))
    n = int(q)
    f = q - n
    if n >= o.get("slides", 1):
        n, f = o["slides"] - 1, 1.0
    if n % 2 == 1:
        f = 1 - f
    return sliderpath.pos_at(o["_p"], o["_c"], f)


def public_objects(bm):
    """Objects as stored with a play: private (_x) helpers dropped, slider body thinned for drawing."""
    out = []
    for o in bm["objects"]:
        d = {k: v for k, v in o.items() if not k.startswith("_")}
        if o["k"] == "s" and "_p" in o:
            d["path"] = sliderpath.thin(o["_p"])
            d["ticks"] = [[round(t), round(pr, 4)] for (t, kind, pr) in o.get("_ev", []) if kind == "t"]
        out.append(d)
    return out


def _slider_end(o):
    if o["k"] == "s" and "ex" in o:
        return [o["ex"], o["ey"]]
    if o["k"] == "s" and o.get("pts") and o.get("slides", 1) % 2 == 1:
        return o["pts"][-1]
    return [o["x"], o["y"]]


def apply_stacking(bm, cs, ar):
    """osu!'s stacking (format v6+): stacked notes are shifted up-left so they stay readable.
    Call after apply_mods. Shifts x/y (and slider points) in place."""
    objs = bm["objects"]
    compute_paths(bm)
    if bm.get("format", 14) < 6 or len(objs) < 2:
        return
    try:
        leniency = float(bm["general"].get("StackLeniency", 0.7))
    except ValueError:
        leniency = 0.7
    pre = 1200 + 600 * (5 - ar) / 5 if ar < 5 else 1200 - 750 * (ar - 5) / 5
    thresh = pre * leniency
    dist = lambda a, b: ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    pos = lambda o: [o["x"], o["y"]]
    sh = [0] * len(objs)
    for i in range(len(objs) - 1, 0, -1):
        n = i
        oi = i
        if sh[oi] != 0 or objs[oi]["k"] == "sp":
            continue
        if objs[oi]["k"] == "c":
            while n - 1 >= 0:
                n -= 1
                on = objs[n]
                if on["k"] == "sp":
                    continue
                if objs[oi]["t"] - on.get("end", on["t"]) > thresh:
                    break
                if on["k"] == "s" and dist(_slider_end(on), pos(objs[oi])) < 3:
                    off = sh[oi] - sh[n] + 1
                    for j in range(n + 1, i + 1):
                        if dist(_slider_end(on), pos(objs[j])) < 3:
                            sh[j] -= off
                    break
                if dist(pos(on), pos(objs[oi])) < 3:
                    sh[n] = sh[oi] + 1
                    oi = n
        else:
            while n - 1 >= 0:
                n -= 1
                on = objs[n]
                if on["k"] == "sp":
                    continue
                if objs[oi]["t"] - on["t"] > thresh:
                    break
                if dist(_slider_end(on), pos(objs[oi])) < 3:
                    sh[n] = sh[oi] + 1
                    oi = n
    r = (54.4 - 4.48 * cs) * 1.00041
    for o, h in zip(objs, sh):
        if h:
            d = h * r * 0.1
            o["x"] -= d
            o["y"] -= d
            if "pts" in o:
                o["pts"] = [[a - d, b - d] for a, b in o["pts"]]
            if "_p" in o:
                o["_p"] = [(a - d, b - d) for a, b in o["_p"]]
                o["ex"], o["ey"] = round(o["ex"] - d, 2), round(o["ey"] - d, 2)
            o["stack"] = h
