"""Slider geometry the way osu! computes it (port of osu!lazer's PathApproximator / SliderPath).

path(o) -> (points, cum)   points: [(x, y), ...] along the slider body, cum: running length per point
"""
import bisect
import math

BEZIER_TOL = 0.25
ARC_TOL = 0.1
CATMULL_DETAIL = 50


def _flat(c):
    lim = BEZIER_TOL * BEZIER_TOL * 4
    for i in range(1, len(c) - 1):
        dx = c[i - 1][0] - 2 * c[i][0] + c[i + 1][0]
        dy = c[i - 1][1] - 2 * c[i][1] + c[i + 1][1]
        if dx * dx + dy * dy > lim:
            return False
    return True


def _subdivide(c):
    n = len(c)
    mid = list(c)
    l = [None] * n
    r = [None] * n
    for i in range(n):
        l[i] = mid[0]
        r[n - i - 1] = mid[n - i - 1]
        for j in range(n - i - 1):
            mid[j] = ((mid[j][0] + mid[j + 1][0]) / 2, (mid[j][1] + mid[j + 1][1]) / 2)
    return l, r


def _bezier(pts):
    n = len(pts)
    if n < 2:
        return list(pts)
    out = []
    stack = [list(pts)]
    guard = 0
    while stack:
        guard += 1
        parent = stack.pop()
        if _flat(parent) or guard > 20000:
            l, r = _subdivide(parent)
            l2 = l + r[1:]
            out.append(parent[0])
            for i in range(1, n - 1):
                k = 2 * i
                out.append((0.25 * (l2[k - 1][0] + 2 * l2[k][0] + l2[k + 1][0]),
                            0.25 * (l2[k - 1][1] + 2 * l2[k][1] + l2[k + 1][1])))
            continue
        l, r = _subdivide(parent)
        stack.append(r)
        stack.append(l)
    out.append(pts[-1])
    return out


def _arc(pts):
    a, b, c = pts
    if abs((b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])) < 1e-3:
        return None
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if d == 0:
        return None
    a2, b2, c2 = a[0] ** 2 + a[1] ** 2, b[0] ** 2 + b[1] ** 2, c[0] ** 2 + c[1] ** 2
    cx = (a2 * (b[1] - c[1]) + b2 * (c[1] - a[1]) + c2 * (a[1] - b[1])) / d
    cy = (a2 * (c[0] - b[0]) + b2 * (a[0] - c[0]) + c2 * (b[0] - a[0])) / d
    rad = math.hypot(a[0] - cx, a[1] - cy)
    t0 = math.atan2(a[1] - cy, a[0] - cx)
    t1 = math.atan2(c[1] - cy, c[0] - cx)
    while t1 < t0:
        t1 += 2 * math.pi
    direction = 1.0
    rng = t1 - t0
    ox, oy = c[1] - a[1], -(c[0] - a[0])
    if ox * (b[0] - a[0]) + oy * (b[1] - a[1]) < 0:
        direction = -1.0
        rng = 2 * math.pi - rng
    if 2 * rad <= ARC_TOL:
        n = 2
    else:
        n = max(2, int(math.ceil(rng / (2 * math.acos(1 - ARC_TOL / rad)))))
    if n >= 1000:
        return None
    return [(cx + math.cos(t0 + direction * i / (n - 1) * rng) * rad,
             cy + math.sin(t0 + direction * i / (n - 1) * rng) * rad) for i in range(n)]


def _catmull_pt(v1, v2, v3, v4, t):
    t2, t3 = t * t, t * t * t
    f = lambda a, b, c, d: 0.5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3)
    return (f(v1[0], v2[0], v3[0], v4[0]), f(v1[1], v2[1], v3[1], v4[1]))


def _catmull(pts):
    out = []
    n = len(pts)
    for i in range(n - 1):
        v1 = pts[i - 1] if i > 0 else pts[i]
        v2 = pts[i]
        v3 = pts[i + 1] if i < n - 1 else (2 * v2[0] - v1[0], 2 * v2[1] - v1[1])
        v4 = pts[i + 2] if i < n - 2 else (2 * v3[0] - v2[0], 2 * v3[1] - v2[1])
        for c in range(CATMULL_DETAIL):
            out.append(_catmull_pt(v1, v2, v3, v4, c / CATMULL_DETAIL))
            out.append(_catmull_pt(v1, v2, v3, v4, (c + 1) / CATMULL_DETAIL))
    return out


def _segments(pts, curve):
    """Legacy sliders: a repeated control point starts a new segment."""
    segs = []
    start = 0
    end = 0
    n = len(pts)
    while True:
        end += 1
        if end >= n:
            break
        if pts[end] != pts[end - 1]:
            continue
        if curve == "C" and end > 1:
            continue
        if end == n - 1:
            continue
        segs.append(pts[start:end])
        start = end
    if end > start:
        segs.append(pts[start:end])
    return segs


def path(o):
    pts = [tuple(p) for p in o["pts"]]
    curve = o.get("curve", "B")
    if curve == "P":
        if len(pts) != 3:
            curve = "B"
        elif abs((pts[1][1] - pts[0][1]) * (pts[2][0] - pts[0][0]) - (pts[1][0] - pts[0][0]) * (pts[2][1] - pts[0][1])) < 1e-3:
            curve = "L"
    out = []
    for seg in _segments(pts, curve):
        if len(seg) < 2:
            sub = list(seg)
        elif curve == "L":
            sub = list(seg)
        elif curve == "P":
            sub = _arc(seg) if len(seg) == 3 else None
            if not sub:
                sub = _bezier(seg)
        elif curve == "C":
            sub = _catmull(seg)
        else:
            sub = _bezier(seg)
        for p in sub:
            if not out or out[-1] != p:
                out.append(p)
    if not out:
        out = [pts[0]]
    cum = [0.0]
    for i in range(len(out) - 1):
        cum.append(cum[-1] + math.hypot(out[i + 1][0] - out[i][0], out[i + 1][1] - out[i][1]))
    expected = float(o.get("len") or 0)
    total = cum[-1]
    if expected > 0 and total != expected and len(out) >= 2:
        if out[-1] == out[-2] and expected > total:
            return out, cum
        cum.pop()
        end = len(out) - 1
        if total > expected:
            while cum and cum[-1] >= expected:
                cum.pop()
                out.pop()
                end -= 1
        if end <= 0:
            return [out[0]], [0.0]
        dx, dy = out[end][0] - out[end - 1][0], out[end][1] - out[end - 1][1]
        d = math.hypot(dx, dy) or 1.0
        rest = expected - cum[-1]
        out[end] = (out[end - 1][0] + dx / d * rest, out[end - 1][1] + dy / d * rest)
        cum.append(expected)
    return out, cum


def pos_at(pts, cum, progress):
    """Position at 0..1 of the path length."""
    if len(pts) < 2 or cum[-1] <= 0:
        return pts[0]
    d = max(0.0, min(1.0, progress)) * cum[-1]
    i = bisect.bisect_left(cum, d)
    if i <= 0:
        return pts[0]
    if i >= len(pts):
        return pts[-1]
    seg = cum[i] - cum[i - 1]
    if seg <= 0:
        return pts[i]
    f = (d - cum[i - 1]) / seg
    return (pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f)


def thin(pts, min_dist=4.0, max_turn=0.12):
    """Fewer points for storage/drawing; corners are kept."""
    if len(pts) <= 2:
        return [[round(p[0], 1), round(p[1], 1)] for p in pts]
    out = [pts[0]]
    acc = 0.0
    for i in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[i], pts[i + 1]
        acc = math.hypot(b[0] - a[0], b[1] - a[1])
        ang = abs(math.atan2(c[1] - b[1], c[0] - b[0]) - math.atan2(b[1] - a[1], b[0] - a[0]))
        if ang > math.pi:
            ang = 2 * math.pi - ang
        if acc >= min_dist or (ang > max_turn and acc > 0.5):
            out.append(b)
    out.append(pts[-1])
    return [[round(p[0], 1), round(p[1], 1)] for p in out]
