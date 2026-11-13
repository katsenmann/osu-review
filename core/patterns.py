"""Stream and jump analysis of one play (everything the "Streams" / "Jumps" cards and the home page show).

Works on the stored play: beatmap objects (positions, times), the simulation results per object
(hit result, timing error, key, where the click landed) and the raw replay frames.
All times are map times; BPM values include the speed mod (DT = 1.5x).
"""
import bisect
import math
import statistics


def _ang(dx, dy):
    return math.degrees(math.atan2(-dy, dx)) % 360          # osu! y points down


DIRS = ["right", "up-right", "up", "up-left", "left", "down-left", "down", "down-right"]


def _end(o):
    if o["k"] == "s" and o.get("ex") is not None:
        return o["ex"], o["ey"]
    return o["x"], o["y"]


def _clean(results):
    """a stretch counts as clean when everything is a 300 (sliders: no break)"""
    return all(r == "300" for r in results)


def _key_lane(k):
    return 0 if k in ("K1", "M1") else 1 if k in ("K2", "M2") else None


def _holds(frames):
    """press durations per lane: list of (start, end) for lane 0 (K1/M1) and lane 1 (K2/M2)"""
    lanes, open_ = [[], []], [None, None]
    for f in frames:
        for i, bits in enumerate((f[3] & 5, f[3] & 10)):
            if bits and open_[i] is None:
                open_[i] = f[0]
            elif not bits and open_[i] is not None:
                lanes[i].append((open_[i], f[0]))
                open_[i] = None
    return lanes


def _hold_at(lane, t):
    """duration of the press of this lane that started closest to t (within 60 ms)"""
    starts = [h[0] for h in lane]
    i = bisect.bisect_left(starts, t - 60)
    best = None
    while i < len(lane) and lane[i][0] <= t + 60:
        if best is None or abs(lane[i][0] - t) < abs(best[0] - t):
            best = lane[i]
        i += 1
    return (best[1] - best[0]) if best else None


def _slope(ys):
    n = len(ys)
    if n < 3:
        return 0.0
    mx = (n - 1) / 2
    my = sum(ys) / n
    den = sum((i - mx) ** 2 for i in range(n)) or 1
    return sum((i - mx) * (y - my) for i, y in enumerate(ys)) / den


def analyze_patterns(play):
    bm, a = play.get("beatmap") or {}, play.get("analysis") or {}
    objs, res = bm.get("objects") or [], a.get("objects") or []
    if not objs or len(objs) != len(res):
        return None
    rate = a.get("rate") or 1.0
    r = a.get("radius") or 32.0
    mm = (play.get("tablet") or {}).get("mm_per_osu")
    frames = play.get("frames") or []
    ft = [f[0] for f in frames]
    lanes = _holds(frames)
    out = {"streams": None, "jumps": None}

    # ------------------------------------------------------------------ streams
    # runs of >= 5 notes with (nearly) equal gaps <= 150 ms (map time) and small spacing
    runs, cur = [], []
    for i in range(len(objs)):
        o = objs[i]
        if o["k"] == "sp":
            if len(cur) >= 5:
                runs.append(cur)
            cur = []
            continue
        if cur:
            p = objs[cur[-1]]
            gap = o["t"] - (p.get("end", p["t"]) if p["k"] == "s" else p["t"])
            first_gap = objs[cur[1]]["t"] - objs[cur[0]]["t"] if len(cur) > 1 else gap
            px, py = _end(p)
            spacing = math.hypot(o["x"] - px, o["y"] - py)
            ok = 0 < gap <= 150 and abs(gap - first_gap) <= max(6, first_gap * 0.12) and spacing <= 4.2 * r and p["k"] == "c"
            if ok:
                cur.append(i)
                continue
            if len(cur) >= 5:
                runs.append(cur)
            cur = [i]
        else:
            cur = [i]
    if len(cur) >= 5:
        runs.append(cur)

    streams, pos_err, all_err, alt_ok, alt_bad, singles, offs_turn, offs_straight = [], [[] for _ in range(10)], [], 0, 0, 0, [], []
    hold = [[], []]
    for run in runs:
        gaps = [objs[run[j + 1]]["t"] - objs[run[j]]["t"] for j in range(len(run) - 1)]
        gap = statistics.median(gaps)
        bpm = 15000.0 / gap * rate
        rs = [res[i].get("res") for i in run]
        errs = [res[i].get("err") for i in run if res[i].get("err") is not None and res[i].get("res") != "miss"]
        sp = statistics.fmean(math.hypot(objs[run[j + 1]]["x"] - objs[run[j]]["x"], objs[run[j + 1]]["y"] - objs[run[j]]["y"]) for j in range(len(run) - 1))
        keys = [res[i].get("key") for i in run]
        lane = [_key_lane(k) for k in keys]
        same = sum(1 for j in range(1, len(lane)) if lane[j] is not None and lane[j] == lane[j - 1])
        pairs = sum(1 for j in range(1, len(lane)) if lane[j] is not None and lane[j - 1] is not None)
        alt_bad += same
        alt_ok += pairs - same
        if pairs and same / pairs > 0.8:
            singles += 1
        for j, i in enumerate(run):
            e = res[i].get("err")
            if e is not None and res[i].get("res") != "miss":
                pos_err[min(9, int(10 * j / len(run)))].append(abs(e))
                all_err.append(e)
            ln = lane[j]
            if ln is not None and e is not None:
                h = _hold_at(lanes[ln], objs[i]["t"] + e)
                if h is not None and h < 400:
                    hold[ln].append(h)
            # aim: offset from the centre, at direction changes vs. straight parts
            if "px" in res[i] and 0 < j < len(run) - 1 and res[i].get("res") != "miss":
                a0 = objs[run[j - 1]]; a1 = objs[i]; a2 = objs[run[j + 1]]
                v1 = (a1["x"] - a0["x"], a1["y"] - a0["y"]); v2 = (a2["x"] - a1["x"], a2["y"] - a1["y"])
                n1, n2 = math.hypot(*v1), math.hypot(*v2)
                off = math.hypot(res[i]["px"] - a1["x"], res[i]["py"] - a1["y"])
                if n1 > 4 and n2 > 4:
                    turn = math.degrees(math.acos(max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))))
                    (offs_turn if turn > 50 else offs_straight).append(off)
        n100 = rs.count("100")
        n50 = rs.count("50")
        nmiss = rs.count("miss")
        drift = _slope(errs) if len(errs) >= 5 else 0.0
        fail_at = next((j for j, x in enumerate(rs) if x != "300"), None)
        streams.append({"t": objs[run[0]]["t"], "end": objs[run[-1]]["t"], "n": len(run), "bpm": round(bpm), "spacing": round(sp / r, 2),
                        "n100": n100, "n50": n50, "miss": nmiss, "clean": _clean(rs),
                        "ur": round(10 * statistics.pstdev(errs), 1) if len(errs) >= 3 else None,
                        "mean": round(statistics.fmean(errs), 1) if errs else None,
                        "drift": round(drift * len(run), 1),            # ms change from first to last note
                        "alt_breaks": same, "first_bad": fail_at})
    if streams:
        tot = sum(s["n"] for s in streams)
        clean = [s for s in streams if s["clean"] and s["n"] >= 8]
        bad = [s for s in streams if not s["clean"]]
        curve = [round(statistics.fmean(v), 1) if v else None for v in pos_err]
        st = {
            "list": streams, "count": len(streams), "notes": tot,
            "share_300": round(100 * sum(s["n"] - s["n100"] - s["n50"] - s["miss"] for s in streams) / tot, 1),
            "ur": round(10 * statistics.pstdev(all_err), 1) if len(all_err) >= 5 else None,
            "mean": round(statistics.fmean(all_err), 1) if all_err else None,
            "drift": round(statistics.fmean(s["drift"] for s in streams if s["n"] >= 8), 1) if any(s["n"] >= 8 for s in streams) else None,
            "curve": curve,
            "alt": round(100 * alt_ok / (alt_ok + alt_bad), 1) if alt_ok + alt_bad else None,
            "alt_breaks": alt_bad,
            "style": "singletap" if singles > len(streams) / 2 else "alternate",
            "hold": [round(statistics.median(h)) if h else None for h in hold],
            "off_turn": round(statistics.fmean(offs_turn) / r * 100) if len(offs_turn) >= 3 else None,
            "off_straight": round(statistics.fmean(offs_straight) / r * 100) if len(offs_straight) >= 3 else None,
            "max_clean_bpm": max((s["bpm"] for s in clean), default=None),
            "max_clean_len": max((s["n"] for s in streams if s["clean"]), default=None),
            "max_bpm": max(s["bpm"] for s in streams),
            "max_len": max(s["n"] for s in streams),
        }
        # where it starts going wrong: shortest bad stream length / lowest bad bpm among longer streams
        if bad:
            st["bad_from_len"] = min(s["n"] for s in bad)
            st["bad_from_bpm"] = min(s["bpm"] for s in bad)
            firsts = [s["first_bad"] for s in bad if s["first_bad"] is not None]
            st["fail_note"] = round(statistics.median(firsts)) if firsts else None
        out["streams"] = st

    # ------------------------------------------------------------------ jumps
    in_stream = set(i for run in runs for i in run[1:])
    jumps = []
    for i in range(1, len(objs)):
        o, p = objs[i], objs[i - 1]
        if o["k"] == "sp" or p["k"] == "sp" or i in in_stream:
            continue
        ro = res[i]
        if ro.get("jd") is None:
            continue
        ex, ey = _end(p)
        d = math.hypot(o["x"] - ex, o["y"] - ey)
        gap = o["t"] - (p.get("end", p["t"]) if p["k"] == "s" else p["t"])
        if d < 110 or gap < 110 or gap > 420:
            continue
        bpm = 30000.0 / gap * rate               # as 1/2 notes
        travel = _ang(o["x"] - ex, o["y"] - ey)
        turn = None
        if i >= 2 and objs[i - 2]["k"] != "sp":
            qx, qy = _end(objs[i - 2])
            v1 = (p["x"] - qx, p["y"] - qy)
            n1 = math.hypot(*v1)
            if n1 > 20:
                c = (v1[0] * (o["x"] - ex) + v1[1] * (o["y"] - ey)) / (n1 * d)
                turn = math.degrees(math.acos(max(-1, min(1, c))))
        # cursor path between leaving the previous note and the click
        t0 = (p.get("end", p["t"]) if p["k"] == "s" else p["t"])
        t1 = o["t"] + (ro.get("err") or 0)
        j0, j1 = bisect.bisect_left(ft, t0), bisect.bisect_right(ft, t1)
        path, peak, corr, straight = 0.0, 0.0, False, 0.0
        if j1 - j0 >= 2:
            straight = math.hypot(frames[j1 - 1][1] - frames[j0][1], frames[j1 - 1][2] - frames[j0][2])
            for k in range(j0 + 1, j1):
                f, g_ = frames[k - 1], frames[k]
                seg = math.hypot(g_[1] - f[1], g_[2] - f[2])
                path += seg
                dt = (g_[0] - f[0]) / rate
                if dt > 0:
                    peak = max(peak, seg / dt)
            # correction: was already near the circle, then still moved a lot right before the click
            # (arrived in the circle's area, stopped, then had to move again by more than a radius)
            arrive = next((k for k in range(j0, j1) if math.hypot(frames[k][1] - o["x"], frames[k][2] - o["y"]) < 1.5 * r), None)
            if arrive is not None and t1 - frames[arrive][0] > 50 * rate:
                stop = None
                for k in range(arrive + 1, j1):
                    dt = frames[k][0] - frames[k - 1][0]
                    if dt > 0 and math.hypot(frames[k][1] - frames[k - 1][1], frames[k][2] - frames[k - 1][2]) / dt < 0.25:
                        stop = k
                        break
                if stop is not None:
                    moved = sum(math.hypot(frames[k][1] - frames[k - 1][1], frames[k][2] - frames[k - 1][2]) for k in range(stop + 1, j1))
                    corr = moved > 1.0 * r
        jumps.append({"i": i, "t": o["t"], "d": round(d, 1), "bpm": round(bpm), "res": ro.get("res"), "err": ro.get("err"),
                      "over": round(100 * ro["par"] / d, 1) if ro.get("par") is not None and ro.get("res") != "miss" else None,
                      "dir": DIRS[int(((travel + 22.5) % 360) // 45)], "turn": None if turn is None else round(turn),
                      "detour": round(100 * (path / straight - 1), 1) if straight > 0.6 * d else None,
                      "peak": round(peak * 1000 * mm) if (peak and mm) else None, "pk": round(peak * 1000) if peak else None, "corr": corr})
    if len(jumps) >= 8:
        def band(lo, hi):
            js = [j for j in jumps if lo <= j["d"] < hi]
            if not js:
                return None
            ov = [j["over"] for j in js if j["over"] is not None]
            er = [j["err"] for j in js if j["err"] is not None and j["res"] != "miss"]
            return {"n": len(js), "clean": round(100 * sum(1 for j in js if j["res"] == "300") / len(js), 1),
                    "over": round(statistics.fmean(ov), 1) if ov else None, "err": round(statistics.fmean(er), 1) if er else None}
        dirs = {}
        for dname in DIRS:
            js = [j for j in jumps if j["dir"] == dname]
            if len(js) >= 3:
                ov = [j["over"] for j in js if j["over"] is not None]
                dirs[dname] = {"n": len(js), "clean": round(100 * sum(1 for j in js if j["res"] == "300") / len(js), 1),
                               "over": round(statistics.fmean(ov), 1) if ov else None}
        shapes = {}
        for name, lo, hi in (("flow", 0, 60), ("angled", 60, 120), ("back", 120, 181)):
            js = [j for j in jumps if j["turn"] is not None and lo <= j["turn"] < hi]
            if len(js) >= 3:
                shapes[name] = {"n": len(js), "clean": round(100 * sum(1 for j in js if j["res"] == "300") / len(js), 1)}
        corr = [j for j in jumps if j["corr"]]
        # jump sections: consecutive jumps close in time
        secs, curs = [], []
        for j in jumps:
            if curs and j["i"] - curs[-1]["i"] <= 2 and j["t"] - curs[-1]["t"] < 1200:
                curs.append(j)
            else:
                if len(curs) >= 4:
                    secs.append(curs)
                curs = [j]
        if len(curs) >= 4:
            secs.append(curs)
        sections = [{"t": s[0]["t"], "end": s[-1]["t"], "n": len(s), "d": round(statistics.fmean(x["d"] for x in s)),
                     "bpm": round(statistics.median(x["bpm"] for x in s)),
                     "n100": sum(1 for x in s if x["res"] in ("100", "50")), "miss": sum(1 for x in s if x["res"] == "miss"),
                     "clean": all(x["res"] == "300" for x in s)} for s in secs]
        # limit: the fastest group of similar jumps (distance x tempo = cursor speed the jump asks for)
        # that was still hit cleanly; onset: the slowest group where it started going wrong
        lim = onset = None
        sv = sorted(jumps, key=lambda j: j["d"] * j["bpm"])
        W = 8
        for k in range(0, len(sv) - W + 1):
            win = sv[k:k + W]
            ok = sum(1 for j in win if j["res"] == "300") / W
            g = {"d": round(statistics.median(j["d"] for j in win)), "bpm": round(statistics.median(j["bpm"] for j in win))}
            if ok >= 0.875:
                lim = g
            elif ok < 0.75 and onset is None:
                onset = g
        detours = [j["detour"] for j in jumps if j["detour"] is not None]
        peaks = [j["peak"] for j in jumps if j["peak"]]
        pks = [j["pk"] for j in jumps if j["pk"]]
        out["jumps"] = {
            "count": len(jumps), "list": jumps[:600], "sections": sections,
            "bands": {"short": band(110, 180), "mid": band(180, 260), "wide": band(260, 9999)},
            "dirs": dirs, "shapes": shapes,
            "corr_pct": round(100 * len(corr) / len(jumps), 1), "corr_n": len(corr),
            "detour": round(statistics.median(detours), 1) if detours else None,
            "peak": round(statistics.median(peaks)) if peaks else None,
            "peak_px": round(statistics.median(pks)) if pks else None,
            "limit": lim, "onset": onset, "mm": mm, "radius": r,
            "max_bpm": max(j["bpm"] for j in jumps), "max_d": max(j["d"] for j in jumps),
        }
    return out


def summary_of(pat):
    """small per-play numbers for the home page ('your limits')"""
    if not pat:
        return None
    s, j = pat.get("streams") or {}, pat.get("jumps") or {}
    w = (j.get("bands") or {}).get("wide") or {}
    return {"s_bpm": s.get("max_clean_bpm"), "s_len": s.get("max_clean_len"), "s_300": s.get("share_300"),
            "s_max_bpm": s.get("max_bpm"), "s_fail": s.get("bad_from_len"),
            "j_d": (j.get("limit") or {}).get("d"), "j_bpm": (j.get("limit") or {}).get("bpm"), "j_mm": j.get("mm"),
            "j_wide_over": w.get("over"), "j_wide_clean": w.get("clean"), "j_n": j.get("count")}
