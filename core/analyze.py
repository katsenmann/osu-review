"""Hit / miss analysis for osu!standard replays.

This re-simulates hits from the cursor + key data. It mirrors osu!'s rules closely
(hit windows, circle radius, notelock-ish ordering) but is an approximation:
the totals from the replay header are always the authoritative numbers.
"""
import bisect
import math
import statistics


def hit_windows(od):
    """osu! cuts the windows to whole milliseconds and a hit has to be *inside* (|error| < window)."""
    return int(80 - 6 * od), int(140 - 8 * od), int(200 - 10 * od)


def radius(cs):
    return (54.4 - 4.48 * cs) * 1.00041


def preempt(ar):
    if ar < 5:
        return 1200 + 600 * (5 - ar) / 5
    return 1200 - 750 * (ar - 5) / 5


def _direction(dx, dy):
    # osu! y axis points down
    ang = math.degrees(math.atan2(-dy, dx)) % 360
    names = ["right", "up-right", "up", "up-left", "left", "down-left", "down", "down-right"]
    return names[int(((ang + 22.5) % 360) // 45)]


def _cursor_at(times, frames, t):
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        i = 0
    f = frames[i]
    return f[1], f[2]


L, R = 1, 2
MISS_RANGE = 400          # a click more than this early does nothing (the circle only shakes)
RELAX_EARLY = 12         # relax: how early the automatic click can happen


def analyze(rep, bm, cs, od, ar, end_time=None):
    """Replays the score with osu!'s own rules. Where a slider end was a sub-pixel call, the totals
    stored in the replay decide (the game's own curve maths differs from ours by fractions of a pixel)."""
    a = _simulate(rep, bm, cs, od, ar, end_time)
    if not a["exact"] and end_time is None and not rep.get("is_lazer"):
        c = a["counts_sim"]
        d300, d100 = c["300"] - rep["n300"], c["100"] - rep["n100"]
        if d300 == -d100 and 0 < abs(d300) <= 3 and c["50"] == rep["n50"] and c["miss"] == rep["nmiss"]:
            k = abs(d300)
            if d300 > 0:      # we gave slider ends the game did not
                cand = sorted((ro["_em"], ro["i"]) for ro in a["objects"]
                              if ro["k"] == "s" and ro["res"] == "300" and ro.get("_em") is not None and ro["_em"] < 0.15)
                want = False
            else:             # we dropped slider ends the game counted
                cand = sorted((-ro["_em"], ro["i"]) for ro in a["objects"]
                              if ro["k"] == "s" and ro["res"] == "100" and ro.get("end_miss") and ro.get("got", 0) == ro.get("parts", 0) - 1
                              and ro.get("_em") is not None and ro["_em"] > -0.6)
                want = True
            tries = [[i for _, i in cand[:k]]] + ([[i] for _, i in cand[1:6]] if k == 1 else [])
            for pick in tries:
                if len(pick) < k:
                    break
                b = _simulate(rep, bm, cs, od, ar, end_time, force={i: want for i in pick})
                if b["exact"]:
                    b["adjusted"] = len(pick)
                    a = b
                    break
    if end_time is not None and not rep.get("is_lazer") and not a["exact"]:
        # failed play: the game still judges the next few objects as misses while the fail plays out
        c = a["counts_sim"]
        deficit = rep["nmiss"] - c["miss"]
        same = (c["300"], c["100"], c["50"]) == (rep["n300"], rep["n100"], rep["n50"])
        open_ = [ro for ro in a["objects"] if ro["res"] == "unplayed"]
        if same and 0 < deficit <= min(12, len(open_)):
            for ro in open_[:deficit]:
                ro["res"] = "miss"
                ro["reason"] = "failed"
                ro["jt"] = int(end_time)
            c["miss"] += deficit
            a["reasons"]["failed"] = a["reasons"].get("failed", 0) + deficit
            a["exact"] = True
    for ro in a["objects"]:
        ro.pop("_em", None)
    return a


def _simulate(rep, bm, cs, od, ar, end_time=None, force=None):
    """One pass over the replay, frame by frame (clicks, note lock, slider follow, ticks, slider
    ends, spinners) - the same way the game judged it."""
    from .beatmap import compute_paths, ball_at
    compute_paths(bm)
    frames = sorted(rep["frames"], key=lambda f: f[0])
    times = [f[0] for f in frames]
    mods = rep["mods"]
    rate = 1.5 if mods & 64 else 0.75 if mods & 256 else 1.0
    # windows / approach time are fixed in *map* time; DT only makes that time pass faster
    w300, w100, w50 = hit_windows(od)
    r = radius(cs)
    pre = preempt(ar)
    relax = bool(mods & 128) or bool(mods & 8192) or bool(mods & 2048)
    lazer = bool(rep.get("is_lazer"))
    objs = bm["objects"]
    n = len(objs)
    res = [{"i": i, "t": o["t"], "k": o["k"], "res": None, "err": None, "reason": None} for i, o in enumerate(objs)]
    st = [dict(done=False, head=False, head_ok=False, scored=0, missed=0, pi=0, sliding=False, down=0,
               tick_miss=0, end_ok=None, rot=0.0, ang=None, near=None, shaken=False, early=None, brk=False)
          for _ in objs]
    combo = 0
    maxc = 0
    combo_ev = []            # [time, combo] whenever it changes
    judged = []              # object indices in the order the game judged them

    def add_combo(t, d):
        nonlocal combo, maxc
        combo = combo + d if d > 0 else 0
        maxc = max(maxc, combo)
        if not combo_ev or combo_ev[-1][1] != combo:
            combo_ev.append([int(t), combo])

    def finish(i, t, result, **extra):
        s = st[i]
        s["done"] = True
        ro = res[i]
        ro["res"] = result
        ro["jt"] = int(t)
        ro.update(extra)
        if result == "miss":
            add_combo(t, 0)
        ro["c"], ro["mc"] = combo, maxc
        judged.append(i)

    btn_l = btn_r = False
    down_state = False
    last_b = last_b2 = down_b = 0
    active = []
    nxt = 0

    def can_hit(i, t):
        """osu!'s note lock: an earlier object that is still open blocks this one."""
        o = objs[i]
        if lazer:
            block = None
            for j in active:
                if j == i:
                    break
                if objs[j]["k"] != "sp" and not (st[j]["head"] if objs[j]["k"] == "s" else st[j]["done"]):
                    block = j
            if block is not None and t < objs[block]["t"]:
                return "shake"
        else:
            if o["k"] == "c":
                k = active.index(i)
                if k > 0 and objs[active[k - 1]].get("stack", 0) > 0 and not st[active[k - 1]]["done"]:
                    return "ignore"
            for j in active:
                if j == i:
                    break
                if not st[j]["done"]:
                    oj = objs[j]
                    if oj.get("_end", oj.get("end", oj["t"])) + 3 < o["t"]:
                        return "shake"
        if abs(o["t"] - t) >= MISS_RANGE:
            return "shake"
        return "click"

    def step(t, x, y, left, right, final=False):
        nonlocal btn_l, btn_r, down_state, last_b, last_b2, down_b, nxt
        while nxt < n and objs[nxt]["t"] - pre <= t:
            active.append(nxt)
            nxt += 1
        lc = left and not btn_l
        rc = right and not btn_r
        if left != btn_l or right != btn_r:
            down_state = left or right
            last_b2, last_b = last_b, down_b
            down_b = (L if left else 0) | (R if right else 0)
        btn_l, btn_r = left, right
        ce = [lc, rc]            # clicks still available in this frame
        key = "K1" if lc else "K2"

        # ---- clicks
        if (lc or rc) and not relax:
            for i in list(active):
                if not (ce[0] or ce[1]):
                    break
                o, s = objs[i], st[i]
                if o["k"] == "sp" or s["done"] or (o["k"] == "s" and s["head"]):
                    continue
                dx, dy = x - o["x"], y - o["y"]
                dist = math.hypot(dx, dy)
                dt = t - o["t"]
                if dist > r:
                    if -MISS_RANGE < dt and (s["near"] is None or dist < s["near"][1]):
                        s["near"] = (dt, dist, dx, dy)
                    continue
                act = can_hit(i, t)
                if act != "click":
                    ce[0] = ce[1] = False
                    if act == "shake" and abs(dt) < MISS_RANGE:
                        s["shaken"] = True
                    continue
                used = "K1" if ce[0] else "K2"
                if ce[0]:
                    ce[0] = False
                else:
                    ce[1] = False
                rel = abs(dt)
                hit = {"key": used, "dist": round(dist, 1), "px": round(x, 1), "py": round(y, 1)}
                if lazer:
                    for j in active:           # lazer: hitting a later object misses the open ones before it
                        if j == i:
                            break
                        oj, sj = objs[j], st[j]
                        if oj["k"] == "c" and not sj["done"]:
                            finish(j, t, "miss", reason="skipped")
                        elif oj["k"] == "s" and not sj["head"]:
                            sj["head"] = True
                            sj["brk"] = True
                            add_combo(t, 0)
                if o["k"] == "c":
                    if rel < w50:
                        add_combo(t, 1)
                        finish(i, t, "300" if rel < w300 else "100" if rel < w100 else "50", err=dt, **hit)
                    else:
                        s["early"] = dt
                        finish(i, t, "miss", err=dt, reason="early" if dt < 0 else "late", **hit)
                else:
                    s["head"] = True
                    s["down"] = L if lc else R if rc else down_b
                    res[i].update(hit)
                    res[i]["err"] = dt
                    if rel < w50:
                        s["head_ok"] = True
                        s["scored"] += 1
                        s["hres"] = "300" if rel < w300 else "100" if rel < w100 else "50"
                        add_combo(t, 1)
                    else:
                        s["brk"] = True
                        s["early"] = dt
                        add_combo(t, 0)

        # ---- relax: the game clicks by itself as soon as the cursor is on the circle (from shortly
        # before its time on); there are no recorded key presses to go by
        if relax:
            for i in list(active):
                o, s = objs[i], st[i]
                if o["k"] == "sp" or s["done"] or (o["k"] == "s" and s["head"]):
                    continue
                dt = t - o["t"]
                if dt < -RELAX_EARLY or dt >= w50:
                    continue
                dx, dy = x - o["x"], y - o["y"]
                dist = math.hypot(dx, dy)
                if dist > r:
                    if s["near"] is None or dist < s["near"][1]:
                        s["near"] = (dt, dist, dx, dy)
                    continue
                rel = abs(dt)
                hit = {"key": "R", "dist": round(dist, 1), "px": round(x, 1), "py": round(y, 1)}
                if o["k"] == "c":
                    add_combo(t, 1)
                    finish(i, t, "300" if rel < w300 else "100" if rel < w100 else "50", err=dt, **hit)
                else:
                    s["head"] = True
                    s["head_ok"] = True
                    s["scored"] += 1
                    s["hres"] = "300" if rel < w300 else "100" if rel < w100 else "50"
                    res[i].update(hit)
                    res[i]["err"] = dt
                    add_combo(t, 1)

        # ---- sliders being followed, spinners being spun
        for i in active:
            o, s = objs[i], st[i]
            if s["done"]:
                continue
            if o["k"] == "s" and t >= o["t"]:
                ok_btn = False
                swap = down_state and not (last_b == (L | R) and last_b2 == down_b)
                if down_state:
                    if s["down"] == 0 or (down_b != (L | R) and swap):
                        s["down"] = down_b
                        ok_btn = True
                    elif down_b & s["down"]:
                        ok_btn = True
                else:
                    s["down"] = 0
                ok_btn = ok_btn or swap or relax
                bx, by = ball_at(o, min(t, o["_end"]))
                need = r * 2.4 if s["sliding"] else r
                allow = ok_btn and math.hypot(x - bx, y - by) <= need
                was = s["sliding"]
                if allow and not s["sliding"]:
                    s["sliding"] = True
                # a tick / repeat / end only counts when the slider was already being followed before this frame
                allow_pt = allow and was
                margin = (need - math.hypot(x - bx, y - by)) / r if ok_btn else None
                ev = o["_ev"]
                # osu! works with whole milliseconds and often lands 1 ms before the exact moment;
                # it stores a replay frame right there, so that frame decides
                while s["pi"] < len(ev) and math.floor(ev[s["pi"]][0]) - 1 <= t:
                    kind = ev[s["pi"]][1]
                    s["pi"] += 1
                    hit_pt = allow_pt
                    if kind == "e":
                        s["em"] = margin if (was or not allow) else -0.5
                        if force and i in force:
                            hit_pt = force[i]
                    if hit_pt:
                        s["scored"] += 1
                        add_combo(t, 1)
                        if kind == "e":
                            s["end_ok"] = True
                    elif kind == "e":
                        s["end_ok"] = False
                        s["missed"] += 1
                    else:
                        s["missed"] += 1
                        s["tick_miss"] += 1
                        s["brk"] = True
                        add_combo(t, 0)
                if s["sliding"] and not allow:
                    s["sliding"] = False
            elif o["k"] == "sp" and o["t"] <= t <= o["end"]:
                # osu! limits how fast a spinner can speed up and caps it at 477 rpm; it counts half turns
                ang = math.atan2(y - 192, x - 256)
                if s["ang"] is not None and t > s["lt"]:
                    real = (t - s["lt"]) / rate
                    d = ang - s["ang"]
                    if d > math.pi:
                        d -= 2 * math.pi
                    elif d < -math.pi:
                        d += 2 * math.pi
                    if not (down_state or relax):
                        d = 0.0
                    theo = d / real
                    acc = (0.00008 + max(0.0, (5000 - (o["end"] - o["t"]) / rate) / 1000 / 2000)) * real
                    v = s.get("vel", 0.0)
                    v = v + min(theo - v, acc) if theo > v else v - min(v - theo, acc)
                    v = max(-0.05, min(0.05, v))
                    s["vel"] = v
                    s["rot"] += abs(v * real) / math.pi
                s["ang"], s["lt"] = ang, t

        # ---- things that ran out of time
        for i in list(active):
            o, s = objs[i], st[i]
            if s["done"]:
                continue
            if o["k"] == "c":
                if t > o["t"] + w50 or final:
                    finish(i, t, "miss")
            elif o["k"] == "s":
                if not s["head"] and (t > o["t"] + w50 or final):
                    s["head"] = True
                    s["brk"] = True
                    add_combo(t, 0)
                if (t >= math.floor(o["_end"]) - 1 and s["pi"] >= len(o["_ev"])) or final:
                    if not s["head"]:            # short slider that was never clicked
                        s["head"] = True
                        s["brk"] = True
                        add_combo(t, 0)
                    parts = len(o["_ev"]) + 1
                    if lazer:
                        out = s.get("hres") if s["head_ok"] else "miss"
                    else:
                        q = s["scored"] / parts
                        out = "300" if q == 1 else "100" if q >= 0.5 else "50" if q > 0 else "miss"
                    extra = {"kind": "slider", "parts": parts, "got": s["scored"]}
                    if s["tick_miss"]:
                        extra["tick_miss"] = s["tick_miss"]
                    if s["end_ok"] is False:
                        extra["end_miss"] = True
                    if not s["head_ok"]:
                        extra["head_miss"] = True
                    if s["brk"]:
                        extra["sb"] = True
                    extra["_em"] = s.get("em")
                    finish(i, t, out, **extra)
            else:
                if t >= o["end"] or final:
                    need = int((o["end"] - o["t"]) / 1000 * (3 + 2 * od / 5 if od <= 5 else 5 + 2.5 * (od - 5) / 5))
                    spins = int(s["rot"])          # half turns, like the game counts them
                    out = "300" if need <= 0 or spins > need + 1 else "100" if spins > need else "50" if spins > need * 0.25 else "miss"
                    if out != "miss":
                        add_combo(t, 1)
                    finish(i, t, out, kind="spinner", spins=round(s["rot"] / 2, 1), need=round(need / 2, 1))
        if any(st[i]["done"] for i in active):
            active[:] = [i for i in active if not st[i]["done"]]

    stop = end_time
    for (t, x, y, k) in frames:
        if stop is not None and t > stop:
            break
        step(t, x, y, bool(k & 5), bool(k & 10))
    if end_time is None and frames:
        # whatever is still open after the last recorded frame
        last = frames[-1]
        tail = max([last[0]] + [o.get("_end", o.get("end", o["t"])) + w50 + 1 for o in objs])
        step(tail, last[1], last[2], bool(last[3] & 5), bool(last[3] & 10), final=True)

    # ---- explain what went wrong
    for i, (o, ro, s) in enumerate(zip(objs, res, st)):
        if ro["res"] is None:
            ro["res"] = "unplayed"
            continue
        if o["k"] == "s":
            ro["kind"] = "slider"
            if ro["res"] != "300" and not ro.get("reason"):
                ro["reason"] = "shead" if ro.get("head_miss") else "sbreak" if ro.get("tick_miss") else "send"
            if ro.get("head_miss") and s["near"] and "px" not in ro:
                ro["off_px"] = round(s["near"][1] - r, 1)
                ro["dir"] = _direction(s["near"][2], s["near"][3])
            continue
        if ro["res"] != "miss" or o["k"] != "c" or ro.get("reason"):
            continue
        cx, cy = _cursor_at(times, frames, o["t"]) if frames else (0, 0)
        cdist = math.hypot(cx - o["x"], cy - o["y"])
        ro["cursor_dist"] = round(cdist, 1)
        if s["near"] and s["near"][0] <= w50:
            e, dist, dx, dy = s["near"]
            ro["reason"] = "notelock" if s["shaken"] else "aim"
            ro["err"] = e
            ro["off_px"] = round(dist - r, 1)
            ro["px"] = round(o["x"] + dx, 1)
            ro["py"] = round(o["y"] + dy, 1)
            ro["dir"] = _direction(dx, dy)
        elif s["shaken"]:
            ro["reason"] = "notelock"
            ro["off_px"] = 0
            ro["dir"] = _direction(cx - o["x"], cy - o["y"])
        else:
            late = None
            lo = bisect.bisect_right(times, o["t"] + w50)
            prev = frames[lo - 1][3] if lo > 0 else 0
            for f in frames[lo:]:
                if f[0] > o["t"] + MISS_RANGE:
                    break
                if ((f[3] & 5) and not (prev & 5)) or ((f[3] & 10) and not (prev & 10)):
                    if math.hypot(f[1] - o["x"], f[2] - o["y"]) <= r * 1.5:
                        late = f[0] - o["t"]
                        break
                prev = f[3]
            if late is not None:
                ro["reason"] = "late"
                ro["err"] = late
            elif cdist > r * 3:
                ro["reason"] = "away"
                ro["dir"] = _direction(cx - o["x"], cy - o["y"])
            else:
                ro["reason"] = "nopress"

    aim = _aim(bm, res, r)

    errs = [ro["err"] for ro in res if ro["err"] is not None and (ro["res"] in ("300", "100", "50") and (ro["k"] == "c" or not ro.get("head_miss")))]
    stats = {}
    if len(errs) >= 2:
        mean = statistics.fmean(errs)
        sd = statistics.pstdev(errs)
        early = [e for e in errs if e < 0]
        late = [e for e in errs if e >= 0]
        stats = {
            "ur": round(sd * 10, 2),
            "ur_real": round(sd * 10 / rate, 2),      # in real time (DT/HT)
            "mean": round(mean, 2),
            "early_avg": round(statistics.fmean(early), 2) if early else 0,
            "late_avg": round(statistics.fmean(late), 2) if late else 0,
            "early_pct": round(100 * len(early) / len(errs), 1),
        }
    counts = {"300": 0, "100": 0, "50": 0, "miss": 0, "hit": 0, "slider_head_miss": 0, "sb": 0}
    reasons = {}
    for ro in res:
        if ro["res"] in counts:
            counts[ro["res"]] += 1
        if ro.get("sb") and ro["res"] != "miss":
            counts["sb"] += 1
        if ro["res"] == "miss" or ro.get("sb"):
            reasons[ro["reason"]] = reasons.get(ro["reason"], 0) + 1
    hdr = (rep.get("n300"), rep.get("n100"), rep.get("n50"), rep.get("nmiss"))
    sim = (counts["300"], counts["100"], counts["50"], counts["miss"])
    exact = end_time is None and sim == hdr and (lazer or maxc == rep.get("max_combo"))

    key_use = {}
    for ro in res:
        if ro.get("key"):
            key_use[ro["key"]] = key_use.get(ro["key"], 0) + 1

    return {
        "objects": res,
        "windows": {"w300": w300 - 0.5, "w100": w100 - 0.5, "w50": w50 - 0.5},
        "radius": r,
        "preempt": pre,
        "rate": rate,
        "stats": stats,
        "counts_sim": counts,
        "max_combo_sim": maxc,
        "combo": combo_ev,
        "order": judged,
        "exact": bool(exact),
        "reasons": reasons,
        "keys": key_use,
        "relax": relax,
        "aim": aim,
        "presses": sum(1 for a, b in zip(frames, frames[1:]) if ((b[3] & 5) and not (a[3] & 5)) or ((b[3] & 10) and not (a[3] & 10))),
    }


def _end_pos(o):
    if o["k"] == "s" and "ex" in o:
        return [o["ex"], o["ey"]]
    return [o["x"], o["y"]]


def _aim(bm, results, r):
    """Where did each click land relative to the circle, seen from the direction you came from?

    par  > 0 = overshoot (past the circle), < 0 = undershoot
    perp > 0 = landed rotated clockwise (screen y points down)
    """
    objs = bm["objects"]
    pars, perps, rel, offs = [], [], [], []
    for i, (o, ro) in enumerate(zip(objs, results)):
        if o["k"] not in ("c", "s") or "px" not in ro:
            continue
        vx, vy = ro["px"] - o["x"], ro["py"] - o["y"]
        if ro["res"] != "miss":
            offs.append((vx, vy))
        if i == 0 or objs[i - 1]["k"] == "sp":
            continue
        ex, ey = _end_pos(objs[i - 1])
        jx, jy = o["x"] - ex, o["y"] - ey
        d = math.hypot(jx, jy)
        if d < 2 * r or o["t"] - objs[i - 1].get("end", objs[i - 1]["t"]) > 1000:
            continue  # stacked/streamy or a long pause: not a real jump
        ux, uy = jx / d, jy / d
        par = vx * ux + vy * uy
        perp = ux * vy - uy * vx
        ro["jd"] = round(d, 1)
        ro["par"] = round(par, 1)
        ro["perp"] = round(perp, 1)
        if ro["res"] != "miss":
            pars.append(par / d)
            perps.append(perp / d)
        rel.append([round(perp / r, 3), round(par / r, 3), ro["res"]])
    out = {"n_hits": len(offs), "n_jumps": len(pars), "rel": rel}
    if offs:
        mx = statistics.fmean(v[0] for v in offs)
        my = statistics.fmean(v[1] for v in offs)
        out["mean_off"] = [round(mx, 2), round(my, 2)]
        out["spread"] = round(math.sqrt(statistics.fmean((v[0] - mx) ** 2 + (v[1] - my) ** 2 for v in offs)), 2)
        out["offs"] = [[round(a / r, 3), round(b / r, 3)] for a, b in offs]
    if len(pars) >= 5:
        out["over_pct"] = round(100 * statistics.fmean(pars), 2)
        out["rot_deg"] = round(math.degrees(statistics.fmean(perps)), 2)
    return out
