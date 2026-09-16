# UV layer helpers for CADder.
#
# All import modes write a single "UVMap" layer (Blender's default name):
# the CAD parametric UVs extracted during tessellation are written by the
# mesh apply paths in main.py. This module provides the shared writer, the
# triplanar box-projection generator that works on any finished Blender
# mesh, and the Smart merge that grows islands out of the CAD charts.

import numpy as np


def write_uv_layer(me, name, loop_uvs):
    """Write a per-loop (L, 2) float array as a UV layer on mesh `me`."""
    if loop_uvs is None or len(loop_uvs) == 0 or len(me.loops) != len(loop_uvs):
        return False
    layer = me.uv_layers.get(name)
    if layer is None:
        layer = me.uv_layers.new(name=name, do_init=False)
    if layer is None:  # Blender's 8-UV-layer limit reached
        return False
    arr = np.asarray(loop_uvs, dtype=np.float32).reshape(-1, 2)
    try:
        # Blender 3.5+ float2 attribute accessor
        layer.uv.foreach_set("vector", arr.ravel())
    except AttributeError:
        layer.data.foreach_set("uv", arr.ravel())
    return True


def add_box_uv(me, scale=1.0, name="UVMap"):
    """Add a triplanar box-projected UV layer to a finished Blender mesh.

    Each face is projected along its dominant normal axis. UVs are the two
    remaining vertex coordinates divided by `scale` (world units per tile).
    """
    n_loops = len(me.loops)
    n_polys = len(me.polygons)
    if n_loops == 0 or n_polys == 0:
        return False

    verts = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)

    loop_vidx = np.empty(n_loops, dtype=np.int32)
    me.loops.foreach_get("vertex_index", loop_vidx)
    loop_pos = verts[loop_vidx]  # (L, 3)

    poly_normals = np.empty(n_polys * 3, dtype=np.float32)
    me.polygons.foreach_get("normal", poly_normals)
    poly_normals = poly_normals.reshape(-1, 3)

    loop_starts = np.empty(n_polys, dtype=np.int32)
    me.polygons.foreach_get("loop_start", loop_starts)
    loop_totals = np.empty(n_polys, dtype=np.int32)
    me.polygons.foreach_get("loop_total", loop_totals)

    # Per-loop face normal (expand by loop_total to support ngons)
    loop_face_n = np.repeat(poly_normals, loop_totals, axis=0)

    dominant = np.argmax(np.abs(loop_face_n), axis=1)  # (L,)
    sign = np.sign(loop_face_n[np.arange(n_loops), dominant])
    sign[sign == 0] = 1.0

    # Axis -> (u_axis, v_axis): X->(Y,Z), Y->(X,Z), Z->(X,Y)
    u_axis = np.where(dominant == 0, 1, 0)
    v_axis = np.where(dominant == 2, 1, 2)

    inv = 1.0 / scale if scale != 0 else 1.0
    li = np.arange(n_loops)
    u = loop_pos[li, u_axis] * inv
    v = loop_pos[li, v_axis] * inv
    # Flip U on negative-facing sides for projection continuity
    u = u * sign

    uvs = np.stack([u, v], axis=1).astype(np.float32)
    return write_uv_layer(me, name, uvs)


# ---------------------------------------------------------------------------
# Smart merge: grow each island out of the CAD charts, one face at a time.
#
# CAD Surfaces gives every CAD face a flat chart of its own, in length units.
# On a plane that chart is exact, and on a cylinder it is the surface rolled
# out, circumference by height. So joining a face to an island needs no
# solver: turn its chart until the shared edge lies on the island's edge.
# That is how a paper model is unfolded, and the checks below decide which
# folds the net may make. A face that does not fit that way, such as a
# ring against the straight edge of a rolled out cylinder, can be bent to
# fit, within a limit on how much the bend stretches it.

# The unwrap modes of the UV Map dropdown. Each is one method of Blender's
# unwrap operator, and the identifiers are the operator's own, so a mode is
# passed straight through as the method.
UNWRAP_MODES = ("CONFORMAL", "ANGLE_BASED", "MINIMUM_STRETCH")

# The modes that start from the parametric UVs of the CAD surface. The
# unwrap modes keep them as the content to fall back on if the unwrap fails.
SURFACE_MODES = ("SURFACE", "SMART") + UNWRAP_MODES


def migrate_settings(d):
    """Put the UV settings of an older import in the terms of this version.

    The UV Map dropdown holds what used to be three settings. 2.4 stored
    uv_mode "UNWRAP", which always used the angle based method, and older
    records still carry uv_surface, uv_unwrap and uv_box booleans. The 2.5
    test builds stored the unwrap method and the tangent merge on their
    own. A refresh of any of these must make the same map again, so each
    one is mapped to the mode that does. Changes d in place and returns it.
    """
    mode = d.get("uv_mode")
    method = d.pop("uv_unwrap_method", None)
    merge = d.pop("uv_merge_tangent", None)
    if mode is None:
        if d.get("uv_box"):
            mode = "BOX"
        elif d.get("uv_unwrap"):
            mode = "UNWRAP"
        elif d.get("uv_surface", True):
            mode = "SURFACE"
        else:
            mode = "NONE"
    if mode == "UNWRAP":
        mode = method if method in UNWRAP_MODES else "ANGLE_BASED"
    elif mode == "SURFACE" and merge in ("SMART", "ALL"):
        mode = "SMART"
    d["uv_mode"] = mode
    return d


# How full the packer gets a tile. The side of the tile a part packs into is
# the square root of its surface area over this.
SMART_FILL = 0.7

# Resolution of the overlap test. Every part gets about this many grid cells
# across the surface it could merge, so a large part and a small one are
# tested equally finely. The overlaps that matter are whole faces landing on
# each other, which this resolves many times over. Finer only costs time.
SMART_CELLS = 60000

# The same test inside one island is never coarser than this many cells
# across its first chart. Growth goes largest first, so every chart that
# joins is smaller than the first one. An island of small faces, such as
# the head of a bolt, would otherwise be tested on cells larger than its
# faces, and faces smaller than a cell cover no cell at all.
SMART_SEED_CELLS = 2000

# A chart that covers fewer cells than this is tested again on a grid of
# its own, SMART_SEED_CELLS across it, against the part of the island round
# it. A small face joining a large island covers only a cell or two of the
# island's grid, and the test cannot see where in those cells it lands.
SMART_FINE_KEYS = 200

# How badly a shared edge may fit before the face is bent instead of only
# turned, as a share of the edge length. A plane and a cylinder fit exactly.
SMART_MAX_MISFIT = 0.02

# How much of a face may land on the island before the join counts as an
# overlap, as a share of the face. A little is allowed for the grid itself,
# which is coarse where two faces meet. The grid is only off along the
# shared edge, so the allowance is never more than one cell for each cell
# of edge length either. A whole island joined across a sharp edge would
# otherwise be allowed to land on the other by 2% of its area.
SMART_MAX_OVERLAP = 0.02

# How far apart two islands are set. The net turns charts into new places,
# and a chart that is refused can start an island where a neighbor's edge
# now lies. Blender joins two faces into one island when their shared edge
# carries the same UV, so without this the two islands would read as one,
# on top of each other. It matches the nudge each CAD face already gets.
SMART_ISLAND_NUDGE = 0.001

# How far Smart may bend a face to join it, as an average over the face.
# Squash counts in full, because it lowers the texel density. Stretch counts
# half: a ring unrolled into a strip is stretched along its inner edge, and
# it is still a clean strip that is easy to texture. A ring of 25 and 15 mm
# unrolled from its rim scores 12.5%. A full disk unrolled from its rim
# scores 50% and more, so with this default it stays an island of its own.
SMART_DISTORTION = 0.35
SMART_STRETCH_WEIGHT = 0.5

# The most one triangle can add to that average. A face bent round a point
# stretches without limit at the point, and one triangle there would
# otherwise decide the average for the whole face.
SMART_STRETCH_CAP = 10.0

# Share of a bent face that may come out mirrored. A mirrored triangle is a
# fold, which a texture shows as a tear, so nearly none is allowed.
SMART_MAX_FLIP = 0.002

# When an island is cut in two because the pieces pack better. A cut has to
# save this share of the rectangle round the whole island, counting the
# margin the packer leaves round each piece. Below it the island stays
# whole: fewer islands are worth a little packing space.
SMART_SPLIT_GAIN = 0.10

# The margin round every island, as a share of the side of the UV tile.
# Blender leaves 0.005 of the tile on each side of an island.
SMART_SPLIT_MARGIN = 0.01

_KEY_OFF = 1 << 30


def _labels(a, b, n):
    """Connected components over the pairs (a, b) of n items."""
    labels = np.arange(n, dtype=np.int64)
    if not len(a):
        return labels
    while True:
        prev = labels
        mn = np.minimum(labels[a], labels[b])
        labels = labels.copy()
        np.minimum.at(labels, a, mn)
        np.minimum.at(labels, b, mn)
        labels = labels[labels]
        if np.array_equal(labels, prev):
            return labels


# Directions for the outline of an island, and for the rectangle round it.
# The outline keeps the extreme point in each of 64 directions, and the
# rectangle is the smallest of 45 turned ones. Both are within a fraction
# of a percent of the exact answer, which the tests below never need, and
# both are one numpy product instead of a loop over the points.
_OUTLINE = np.stack([np.cos(np.arange(64) * (np.pi / 32.0)),
                     np.sin(np.arange(64) * (np.pi / 32.0))], axis=1)
_RECT_ANGLES = np.linspace(0.0, np.pi / 2.0, 46)[:-1]
_RECT_DIRS = np.stack([np.cos(_RECT_ANGLES), np.sin(_RECT_ANGLES)], axis=1)
_RECT_NORMS = np.stack([-np.sin(_RECT_ANGLES), np.cos(_RECT_ANGLES)], axis=1)


def _hull(pts):
    """The outline of 2D points: the extreme point in each direction."""
    pts = np.asarray(pts, dtype=np.float64)
    if len(pts) <= 3:
        return pts
    return pts[np.unique(np.argmax(pts @ _OUTLINE.T, axis=0))]


def _rect(pts, margin=0.0):
    """Sides of the smallest rectangle round points, a margin added to both.

    The packer turns islands to fit them, so what has to fit the tile is
    this rectangle, not the box the island happens to sit in.
    """
    if len(pts) < 2:
        return margin, margin
    w = np.ptp(pts @ _RECT_DIRS.T, axis=0) + margin
    h = np.ptp(pts @ _RECT_NORMS.T, axis=0) + margin
    k = int(np.argmin(w * h))
    return float(w[k]), float(h[k])


def _long_side(hull):
    """Long side of the smallest rectangle round an outline."""
    w, h = _rect(hull)
    return max(w, h)


def _raster(tris, cell, window=None):
    """The grid cells whose centers fall inside the triangles, as keys.

    Testing cell centers, not cell areas, is what lets two faces share an
    edge without counting as an overlap: a center lies on one side of the
    edge or the other, never on both. `window`, as (low, high) cell
    numbers, keeps only the cells inside it, so a large chart can be tested
    on a fine grid where a small one lands.
    """
    if not len(tris):
        return np.zeros(0, dtype=np.int64)
    g = tris / cell
    mn = np.floor(g.min(axis=1)).astype(np.int64)
    mx = np.floor(g.max(axis=1)).astype(np.int64)
    if window is not None:
        mn = np.maximum(mn, window[0])
        mx = np.minimum(mx, window[1])
        keep = (mx >= mn).all(axis=1)
        if not keep.any():
            return np.zeros(0, dtype=np.int64)
        g, mn, mx = g[keep], mn[keep], mx[keep]
    w = mx[:, 0] - mn[:, 0] + 1
    h = mx[:, 1] - mn[:, 1] + 1
    cnt = w * h
    total = int(cnt.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64)
    t = np.repeat(np.arange(len(g)), cnt)
    off = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(cnt) - cnt,
                                                       cnt)
    cx = mn[t, 0] + off % w[t]
    cy = mn[t, 1] + off // w[t]
    px = cx + 0.5
    py = cy + 0.5
    a, b, c = g[t, 0], g[t, 1], g[t, 2]

    def side(p, q):
        return (q[:, 0] - p[:, 0]) * (py - p[:, 1]) \
            - (q[:, 1] - p[:, 1]) * (px - p[:, 0])

    e0, e1, e2 = side(a, b), side(b, c), side(c, a)
    inside = (((e0 > 0) & (e1 > 0) & (e2 > 0))
              | ((e0 < 0) & (e1 < 0) & (e2 < 0)))
    # The triangles of one unbent chart do not overlap, and a center on the
    # edge between two of them counts for neither. A bent chart can land on
    # itself, and the caller counts the repeats to find out.
    return (cx[inside] + _KEY_OFF) * (1 << 32) + (cy[inside] + _KEY_OFF)


def _fit(p, q):
    """The turn and shift that best put points p on points q, and the misfit.

    Rotation only, no mirror: every chart is turned the same way up first,
    and a mirror would lay the face on top of the island instead of beside
    it.
    """
    pc = p.mean(axis=0)
    qc = q.mean(axis=0)
    x = p - pc
    y = q - qc
    s = float((x[:, 0] * y[:, 1] - x[:, 1] * y[:, 0]).sum())
    c = float((x * y).sum())
    th = np.arctan2(s, c)
    r = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    t = qc - r @ pc
    err = float(np.sqrt(np.sum((x @ r.T - y) ** 2, axis=1)).max())
    return r, t, err


def _cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _unit(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(n, 1e-300)


def _left(e):
    """The left normal of each 2D direction, unit length."""
    return _unit(np.stack([-e[..., 1], e[..., 0]], axis=-1))


def _chains(pa, pb, qa, qb, va, vb, tol_p, tol_q):
    """Order the shared edges of a join into chains.

    Every edge runs the way the joining face winds, so the face lies to its
    left in its own chart and the island lies to its right. Two edges link
    where one ends at the vertex the next starts at. A chain carries on only
    where both charts carry one UV on both sides of that vertex, and stops
    where either chart is cut.

    Returns the chains as (edges in order, closed_in_p, cycle), and the unit
    normals at both ends of every edge in both charts. A normal at a vertex
    where the chart is not cut is the mean of the two edges, so a curve
    bends smoothly through it.
    """
    m = len(pa)
    starts = {}
    for i in range(m):
        starts.setdefault(int(va[i]), []).append(i)
    next_p = np.full(m, -1, dtype=np.int64)
    next_q = np.full(m, -1, dtype=np.int64)
    for i in range(m):
        cand = starts.get(int(vb[i]), [])
        hit_p = [j for j in cand if j != i
                 and np.abs(pb[i] - pa[j]).sum() <= tol_p]
        hit_q = [j for j in cand if j != i
                 and np.abs(qb[i] - qa[j]).sum() <= tol_q]
        if len(hit_p) == 1:
            next_p[i] = hit_p[0]
        if len(hit_q) == 1:
            next_q[i] = hit_q[0]
    prev_p = np.full(m, -1, dtype=np.int64)
    prev_q = np.full(m, -1, dtype=np.int64)
    for i in range(m):
        if next_p[i] >= 0:
            prev_p[next_p[i]] = i
        if next_q[i] >= 0:
            prev_q[next_q[i]] = i

    n_p = _left(pb - pa)
    n_q = _left(qb - qa)

    def ends(n, nxt, prv):
        s = n.copy()
        e = n.copy()
        has = nxt >= 0
        e[has] = _unit(n[has] + n[nxt[has]])
        has = prv >= 0
        s[has] = _unit(n[has] + n[prv[has]])
        return s, e

    np_s, np_e = ends(n_p, next_p, prev_p)
    nq_s, nq_e = ends(n_q, next_q, prev_q)

    nxt = np.where((next_p >= 0) & (next_p == next_q), next_p, -1)
    prv = np.full(m, -1, dtype=np.int64)
    for i in range(m):
        if nxt[i] >= 0:
            prv[nxt[i]] = i
    seen = np.zeros(m, dtype=bool)
    chains = []
    for i in list(np.where(prv < 0)[0]) + list(range(m)):
        if seen[i]:
            continue
        cycle = prv[i] >= 0
        run = []
        j = i
        while j >= 0 and not seen[j]:
            seen[j] = True
            run.append(j)
            j = nxt[j]
        run = np.array(run, dtype=np.int64)
        closed_p = (not cycle and vb[run[-1]] == va[run[0]]
                    and np.abs(pb[run[-1]] - pa[run[0]]).sum() <= tol_p)
        chains.append((run, bool(closed_p), bool(cycle)))

    # The chains have to follow on from each other in the face's own chart,
    # so that together they are one run of its outline, cut only where the
    # island is cut. A face that meets the island in two places apart would
    # otherwise be torn down the middle to reach both.
    cid = np.empty(m, dtype=np.int64)
    for g, (run, _cp, _cy) in enumerate(chains):
        cid[run] = g
    succ = [int(cid[next_p[run[-1]]]) if next_p[run[-1]] >= 0 else -1
            for run, _cp, _cy in chains]
    has_pred = {s for s in succ if s >= 0}
    heads = [g for g in range(len(chains)) if g not in has_pred]
    one_run = len(heads) <= 1
    if one_run:
        g = heads[0] if heads else 0
        visited = set()
        while g >= 0 and g not in visited:
            visited.add(g)
            g = succ[g]
        one_run = len(visited) == len(chains)
    return chains, (np_s, np_e, nq_s, nq_e), one_run


def _closest(x, p0, p1):
    """Closest segment to each point, with its parameter and distance."""
    e = p1 - p0
    ee = np.maximum((e * e).sum(axis=1), 1e-300)
    best = np.zeros(len(x), dtype=np.int64)
    best_t = np.zeros(len(x))
    best_d = np.full(len(x), np.inf)
    step = max(1, int(1e6 // max(len(p0), 1)))
    for a in range(0, len(x), step):
        w = x[a:a + step, None, :] - p0[None, :, :]
        t = np.clip((w * e[None]).sum(axis=2) / ee[None], 0.0, 1.0)
        d = ((w - t[..., None] * e[None]) ** 2).sum(axis=2)
        k = d.argmin(axis=1)
        r = np.arange(len(k))
        best[a:a + step] = k
        best_t[a:a + step] = t[r, k]
        best_d[a:a + step] = d[r, k]
    return best, best_t, np.sqrt(best_d)


def _bend_chart(x, face, p0, p1, np0, np1, q0, q1, nq0, nq1, chains):
    """Lay a chart against an island edge that it does not fit rigidly.

    Each point of the chart is measured against the shared edge in its own
    chart: how far along the edge it lies, and how far out from it. It is
    then set down the same distance along and out from the island's copy
    of the edge. A ring measured from its rim this way unrolls into a
    strip, a strip laid along a circle wraps round it, and an edge that is
    only slightly the wrong length is shared out along its whole run.

    The distance out is taken along a normal that turns smoothly along the
    edge (the mean of two edges at each vertex), not to the nearest point:
    a nearest point jumps where the edge turns, and whole wedges of the
    face would fall on one line.

    Where the island's copy of the edge is cut but the face's copy runs
    round unbroken, as when a ring meets a cylinder that was rolled out,
    the face has to be cut as well. Each polygon keeps to the side of the
    cut its middle is on, so the cut follows mesh edges.

    x: (n, 2) chart points, face: the polygon of each point. The edge
    arrays hold one row per shared edge, and `chains` orders them.
    Returns the new (n, 2) points.
    """
    out = np.empty_like(x)
    # Which chain each polygon follows, and where along it its middle is.
    fids, finv = np.unique(face, return_inverse=True)
    mid = np.zeros((len(fids), 2))
    np.add.at(mid, finv, x)
    mid /= np.bincount(finv, minlength=len(fids))[:, None]
    seg_chain = np.empty(len(p0), dtype=np.int64)
    for g, (run, _cp, _cy) in enumerate(chains):
        seg_chain[run] = g
    k, _t, _d = _closest(mid, p0, p1)
    fchain = seg_chain[k]
    pchain = fchain[finv]

    for g, (run, closed_p, cycle) in enumerate(chains):
        sel = np.where(pchain == g)[0]
        if not len(sel):
            continue
        a0, a1 = p0[run], p1[run]
        n0, n1 = np0[run], np1[run]
        e = a1 - a0
        seg_len = np.maximum(np.linalg.norm(e, axis=1), 1e-300)
        cum = np.concatenate([[0.0], np.cumsum(seg_len)])
        total = cum[-1]
        ns = len(run)
        wrap = closed_p or cycle
        pts = x[sel]
        j0, tc0, _dd = _closest(pts, a0, a1)
        best_j = j0.copy()
        best_t = tc0.copy()
        # Signed distance to the nearest point: the fallback where the
        # smooth normal finds no foot on the edge.
        w = pts - (a0[j0] + tc0[:, None] * e[j0])
        best_dist = (w * _left(e[j0])).sum(axis=1)
        best_abs = np.full(len(sel), np.inf)
        for off in (-1, 0, 1):
            j = j0 + off
            if wrap:
                j = j % ns
            else:
                ok_j = (j >= 0) & (j < ns)
                j = np.clip(j, 0, ns - 1)
            a = pts - a0[j]
            ej = e[j]
            m0 = n0[j]
            dm = n1[j] - n0[j]
            qa = -_cross(ej, dm)
            qb = _cross(a, dm) - _cross(ej, m0)
            qc = _cross(a, m0)
            roots = []
            lin = np.abs(qa) <= 1e-12 * (np.abs(qb) + 1e-300)
            with np.errstate(divide="ignore", invalid="ignore"):
                t_lin = np.where(lin, -qc / qb, np.nan)
                disc = qb * qb - 4.0 * qa * qc
                sq = np.sqrt(np.where(disc >= 0.0, disc, np.nan))
                r1 = np.where(lin, t_lin, (-qb + sq) / (2.0 * qa))
                r2 = np.where(lin, np.nan, (-qb - sq) / (2.0 * qa))
            roots = [r1, r2]
            # Past the open ends of a chain the normal stops turning and the
            # edge carries straight on.
            if not wrap:
                with np.errstate(divide="ignore", invalid="ignore"):
                    first = j == 0
                    t_lo = np.where(first, _cross(a, m0) / _cross(ej, m0),
                                    np.nan)
                    last = j == ns - 1
                    t_hi = np.where(last, _cross(a, n1[j]) / _cross(ej, n1[j]),
                                    np.nan)
                roots.append(np.where(t_lo < 0.0, t_lo, np.nan))
                roots.append(np.where(t_hi > 1.0, t_hi, np.nan))
            for t in roots:
                ok = np.isfinite(t) & (t >= -1e-9) & (t <= 1.0 + 1e-9)
                if not wrap:
                    ok |= np.isfinite(t) & (((j == 0) & (t < 0.0))
                                            | ((j == ns - 1) & (t > 1.0)))
                    ok &= ok_j
                tt = np.where(np.isfinite(t), t, 0.0)
                tcl = np.clip(tt, 0.0, 1.0)
                nrm = _unit((1.0 - tcl)[:, None] * m0 + tcl[:, None] * n1[j])
                foot = a0[j] + tt[:, None] * ej
                dist = ((pts - foot) * nrm).sum(axis=1)
                take = ok & (np.abs(dist) < best_abs)
                best_abs = np.where(take, np.abs(dist), best_abs)
                best_j = np.where(take, j, best_j)
                best_t = np.where(take, tt, best_t)
                best_dist = np.where(take, dist, best_dist)

        s = cum[best_j] + best_t * seg_len[best_j]
        if closed_p:
            # The face runs round unbroken where the island is cut. Keep
            # each polygon on the side of the cut its middle is on.
            _kk, _tt, _dd = _closest(mid[finv[sel]], a0, a1)
            ref = cum[_kk] + _tt * seg_len[_kk]
            s = s + total * np.round((ref - s) / total)
        if closed_p or not wrap:
            jj = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, ns - 1)
            tt = (s - cum[jj]) / seg_len[jj]
        else:
            s = np.mod(s, total)
            jj = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, ns - 1)
            tt = (s - cum[jj]) / seg_len[jj]
        b0, b1 = q0[run][jj], q1[run][jj]
        tcl = np.clip(tt, 0.0, 1.0)
        nrm = _unit((1.0 - tcl)[:, None] * nq0[run][jj]
                    + tcl[:, None] * nq1[run][jj])
        out[sel] = b0 + tt[:, None] * (b1 - b0) + best_dist[:, None] * nrm
    return out


def _stretch(u, t0, t1, t2, frame, density):
    """Stretch, squash and mirror of each triangle against its 3D shape.

    `frame` holds, for each triangle, the 3D length of its first edge and
    the second edge in the frame of the first (along it, then across it).
    Returns the two stretches (the larger first) as multiples of the part's
    texel density, and whether the triangle is mirrored.
    """
    l1, ex, ey = frame
    b00 = u[t1, 0] - u[t0, 0]
    b10 = u[t1, 1] - u[t0, 1]
    b01 = u[t2, 0] - u[t0, 0]
    b11 = u[t2, 1] - u[t0, 1]
    ja = b00 / l1
    jc = b10 / l1
    jb = (b01 - ja * ex) / ey
    jd = (b11 - jc * ex) / ey
    det = ja * jd - jb * jc
    f = ja * ja + jb * jb + jc * jc + jd * jd
    disc = np.sqrt(np.maximum(f * f - 4.0 * det * det, 0.0))
    s1 = np.sqrt((f + disc) * 0.5) / density
    s2 = np.sqrt(np.maximum(f - disc, 0.0) * 0.5) / density
    return s1, s2, det < 0.0


def _copies(key, u, near):
    """Number the separate UV copies among loops that share a key.

    A chart can hold one vertex twice, once on each side of a cut, and the
    two copies must not be welded as one.
    """
    copy = np.zeros(len(key), dtype=np.int64)
    if not len(key):
        return copy
    order = np.argsort(key, kind="stable")
    ks = key[order]
    start = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
    stop = np.r_[start[1:], len(ks)]
    us = u[order]
    lo = np.minimum.reduceat(us, start, axis=0)
    hi = np.maximum.reduceat(us, start, axis=0)
    for g in np.flatnonzero((hi - lo).max(axis=1) > near):
        reps = []
        for li in order[start[g]:stop[g]]:
            for ci, r in enumerate(reps):
                if np.abs(u[li] - r).sum() <= near:
                    copy[li] = ci
                    break
            else:
                copy[li] = len(reps)
                reps.append(u[li])
    return copy


def _rect_area(pts, margin=0.0):
    """Area of the smallest rectangle round points, with a margin round it.

    The margin stands for the space the packer leaves round every island.
    """
    w, h = _rect(pts, margin)
    return w * h


def _split_islands(members, edges, hull_pts, area, margin, gain):
    """Cut awkward islands along their joins, where the pieces pack better.

    Every island is a tree of charts, joined one at a time. Cutting a join
    splits it in two, and each piece keeps the layout it has. A net that
    grew into a V, or has a long arm out at an angle, fills little of the
    rectangle round it, and its pieces fill theirs far better. So the cut
    that makes the pieces' rectangles smallest together is made, as long as
    it saves at least `gain` of the rectangle of the whole, and the pieces
    are tried again. The margin round every island counts too, so cutting
    off a small piece costs what the packer will leave round it.

    members: {island: [charts]}. edges: tree joins as (chart, chart).
    hull_pts: {chart: (k, 2) hull}. area: UV area of each chart.
    Returns {chart: piece} with a new number for every piece.
    """
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    out = {}
    counter = [0]

    def pts_of(charts):
        return np.vstack([hull_pts[c] for c in charts])

    def piece(charts):
        pid = counter[0]
        counter[0] += 1
        for c in charts:
            out[c] = pid

    stack = [list(ch) for ch in members.values()]
    while stack:
        charts = stack.pop()
        if len(charts) < 2:
            piece(charts)
            continue
        whole = _rect_area(pts_of(charts), margin)
        fill = sum(area[c] for c in charts) / max(whole, 1e-30)
        # No cut can save more than the rectangle the island leaves empty.
        if 1.0 - fill < gain:
            piece(charts)
            continue
        inside = set(charts)
        root = max(charts, key=lambda c: area[c])
        # Depth first from the largest chart: the order, and each chart's
        # parent, so every subtree is a run of the order.
        order, parent = [], {root: None}
        todo = [root]
        while todo:
            c = todo.pop()
            order.append(c)
            for n in adj.get(c, []):
                if n in inside and n not in parent:
                    parent[n] = c
                    todo.append(n)
        below = {c: [c] for c in order}
        for c in reversed(order):
            p = parent[c]
            if p is not None:
                below[p].extend(below[c])
        best, cut = whole * (1.0 - gain), None
        for c in order[1:]:
            part = below[c]
            rest = inside.difference(part)
            if not rest:
                continue
            cost = (_rect_area(pts_of(part), margin)
                    + _rect_area(pts_of(rest), margin))
            if cost < best:
                best, cut = cost, part
        if cut is None:
            piece(charts)
            continue
        cut = set(cut)
        stack.append([c for c in charts if c in cut])
        stack.append([c for c in charts if c not in cut])
    return out


def smart_merge(me, side_3d=None, distortion=SMART_DISTORTION, sharp=False,
                split=True):
    """Grow UV islands out of the CAD charts of one mesh.

    Starts from the largest chart and joins its tangent neighbors one at a
    time, largest first. A neighbor is turned so its shared edge lies on
    the island. If it does not fit the edge that way, it is bent to fit,
    as long as the bend stretches it by no more than `distortion` on
    average. It joins only if it then does not land on the island and does
    not make the island too long for the tile. A neighbor that fails waits
    for a later island. The next island starts from the largest chart
    still left, until none is left.

    With `sharp`, a second pass does the same across sharp edges, with each
    island of the first pass as one piece. The smooth joins come first, so
    a sharp edge is only crossed where the smooth ones left a gap.

    Last, with `split`, an island that grew into an awkward shape is cut
    along its joins where the pieces pack better (_split_islands).

    `side_3d` is the side of the UV tile, in the mesh's own length units.
    None means one tile for this part alone, which is the side of the square
    its surface would pack into. The tile is never smaller than the longest
    chart, because a CAD face is never split.

    Writes the UVs and puts the seams on the island boundaries. Returns
    (charts, islands) for the charts that had a neighbor to join.
    """
    layer = me.uv_layers.active
    nl = len(me.loops)
    npoly = len(me.polygons)
    if layer is None or nl == 0 or npoly == 0:
        return 0, 0
    uv = np.empty(nl * 2, dtype=np.float64)
    layer.uv.foreach_get("vector", uv)
    uv = uv.reshape(-1, 2)
    lv = np.empty(nl, dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    le = np.empty(nl, dtype=np.int64)
    me.loops.foreach_get("edge_index", le)
    starts = np.empty(npoly, dtype=np.int64)
    me.polygons.foreach_get("loop_start", starts)
    totals = np.empty(npoly, dtype=np.int64)
    me.polygons.foreach_get("loop_total", totals)
    parea = np.empty(npoly, dtype=np.float64)
    me.polygons.foreach_get("area", parea)
    ne = len(me.edges)
    sharp_e = np.zeros(ne, dtype=bool)
    me.edges.foreach_get("use_edge_sharp", sharp_e)

    pol = np.repeat(np.arange(npoly), totals)
    nxt = np.arange(nl) + 1
    last = starts + totals - 1
    nxt[last] = starts

    # The two loops of every edge that has exactly two faces.
    order = np.argsort(le, kind="stable")
    cnt = np.bincount(le, minlength=ne)
    first = np.concatenate([[0], np.cumsum(cnt)[:-1]])
    man = np.where(cnt == 2)[0]
    l1 = order[first[man]]
    l2 = order[first[man] + 1]
    # l1 runs from va to vb. In a mesh wound one way, l2 runs back.
    back = lv[l2] != lv[l1]
    a1, b1 = l1, nxt[l1]
    a2 = np.where(back, nxt[l2], l2)
    b2 = np.where(back, l2, nxt[l2])
    p1, p2 = pol[l1], pol[l2]

    span = float(np.ptp(uv, axis=0).max()) if nl else 1.0
    tol = max(span, 1e-12) * 1e-7

    def joined(u):
        return ((np.abs(u[a1] - u[a2]).sum(axis=1) <= tol)
                & (np.abs(u[b1] - u[b2]).sum(axis=1) <= tol))

    def charts_of(u):
        """The faces the UVs hold together, as a chart number per polygon."""
        cont = joined(u)
        _ids, lab = np.unique(_labels(p1[cont], p2[cont], npoly),
                              return_inverse=True)
        return lab.ravel()

    # Fan triangles of every polygon, for areas and the overlap test.
    tcount = totals - 2
    tp = np.repeat(np.arange(npoly), tcount)
    k = np.arange(int(tcount.sum())) - np.repeat(np.cumsum(tcount) - tcount,
                                                 tcount)
    t0 = starts[tp]
    t1 = t0 + k + 1
    t2 = t0 + k + 2

    def signed(u):
        return 0.5 * ((u[t1, 0] - u[t0, 0]) * (u[t2, 1] - u[t0, 1])
                      - (u[t2, 0] - u[t0, 0]) * (u[t1, 1] - u[t0, 1]))

    # The charts the CAD surfaces give. Every one the same way up: a face
    # whose surface runs the other way arrives mirrored, and a mirrored
    # chart cannot be turned into place.
    base_chart = charts_of(uv)
    n_base = int(base_chart.max()) + 1
    csign = np.bincount(base_chart[tp], weights=signed(uv), minlength=n_base)
    uv[(csign < 0)[base_chart[pol]], 0] *= -1.0
    base_area = np.abs(np.bincount(base_chart[tp], weights=signed(uv),
                                   minlength=n_base))
    base_3d = np.bincount(base_chart, weights=parea, minlength=n_base)
    density = np.sqrt(base_area.sum() / max(base_3d.sum(), 1e-30))
    if side_3d is None:
        side_3d = np.sqrt(base_3d.sum() / SMART_FILL)

    # The 3D shape of every triangle, for the stretch of a bent chart: the
    # length of its first edge, and its second edge along and across it.
    frame = None
    tarea = None
    if distortion > 0.0:
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        e1 = co[lv[t1]] - co[lv[t0]]
        e2 = co[lv[t2]] - co[lv[t0]]
        f_l1 = np.linalg.norm(e1, axis=1)
        f_ex = (e1 * e2).sum(axis=1) / np.maximum(f_l1, 1e-300)
        f_ey = np.linalg.norm(np.cross(e1, e2), axis=1) / np.maximum(f_l1,
                                                                    1e-300)
        good = (f_l1 > 0.0) & (f_ey > 1e-9 * np.maximum(f_l1, 1e-300))
        frame = (np.where(good, f_l1, 1.0), f_ex, np.where(good, f_ey, 1.0))
        tarea = np.where(good, 0.5 * f_l1 * f_ey, 0.0)

    # Every join any pass makes, as a pair of the charts above. Together
    # they are the tree each island grew as, which the split walks.
    tree = []
    took_part = set()

    def grow(allow_sharp):
        """One pass of the growth, over the charts the UVs now hold."""
        chart = charts_of(uv)
        nc = int(chart.max()) + 1
        tchart = chart[tp]
        lchart = chart[pol]
        carea_uv = np.abs(np.bincount(tchart, weights=signed(uv),
                                      minlength=nc))
        carea_3d = np.bincount(chart, weights=parea, minlength=nc)

        between = chart[p1] != chart[p2]
        if not allow_sharp:
            between &= ~sharp_e[man]
        if not between.any():
            return
        ja, jb = chart[p1][between], chart[p2][between]
        ja1, jb1 = a1[between], b1[between]
        ja2, jb2 = a2[between], b2[between]
        comp = _labels(ja, jb, nc)
        sizes = np.bincount(comp, minlength=nc)
        busy = sizes[comp] > 1
        work = np.where(busy)[0]
        if not len(work):
            return

        # For each pair of charts, the loops that meet along their shared
        # edge: (loop in the lower chart, loop in the higher chart), both
        # ends.
        joins = {}
        for i in range(len(ja)):
            a, b = int(ja[i]), int(jb[i])
            if a < b:
                rec = ((ja1[i], ja2[i]), (jb1[i], jb2[i]))
            else:
                a, b = b, a
                rec = ((ja2[i], ja1[i]), (jb2[i], jb1[i]))
            joins.setdefault((a, b), []).append(rec)
        nbrs = {}
        for a, b in joins:
            nbrs.setdefault(a, []).append(b)
            nbrs.setdefault(b, []).append(a)
        # Both arrays hold the first ends of the shared edges, then the
        # second ends in the same order, so an edge is item i and item
        # i + half.
        pairs = {}
        for key, recs in joins.items():
            lo = np.array([r[0][0] for r in recs] + [r[1][0] for r in recs])
            hi = np.array([r[0][1] for r in recs] + [r[1][1] for r in recs])
            pairs[key] = (lo, hi)

        # Loops and triangles of each chart that takes part.
        lorder = np.argsort(lchart, kind="stable")
        lcnt = np.bincount(lchart, minlength=nc)
        lfirst = np.concatenate([[0], np.cumsum(lcnt)[:-1]])
        torder = np.argsort(tchart, kind="stable")
        tcnt = np.bincount(tchart, minlength=nc)
        tfirst = np.concatenate([[0], np.cumsum(tcnt)[:-1]])

        def loops_of(c):
            return lorder[lfirst[c]:lfirst[c] + lcnt[c]]

        def tri_ids(c):
            return torder[tfirst[c]:tfirst[c] + tcnt[c]]

        def tris_of(c, u=uv):
            idx = tri_ids(c)
            return np.stack([u[t0[idx]], u[t1[idx]], u[t2[idx]]], axis=1)

        hulls = {int(c): _hull(uv[loops_of(c)]) for c in work}

        # The tile. It is never smaller than the longest chart of the part,
        # because a chart is one CAD face and is never split.
        longest = max((_long_side(h) for h in hulls.values()), default=0.0)
        alone = np.where(~busy)[0]
        if len(alone):
            lo = np.full((nc, 2), np.inf)
            hi = np.full((nc, 2), -np.inf)
            np.minimum.at(lo, lchart, uv)
            np.maximum.at(hi, lchart, uv)
            ext = (hi - lo)[alone]
            longest = max(longest, float(ext.max()))
        limit = max(side_3d * density, longest)
        cell = np.sqrt(carea_uv[work].sum() / SMART_CELLS)
        if not (cell > 0.0):
            return

        def bend(c, mine, theirs, u):
            """Bend chart c against the island. New UVs of its loops, or
            None."""
            half = len(mine) // 2
            ma, mb = mine[:half].copy(), mine[half:].copy()
            ta, tb = theirs[:half].copy(), theirs[half:].copy()
            # Every edge the way chart c winds, so the chart lies on its
            # left.
            fwd = nxt[ma] == mb
            ma[~fwd], mb[~fwd] = mb[~fwd], ma[~fwd].copy()
            ta[~fwd], tb[~fwd] = tb[~fwd], ta[~fwd].copy()
            pa, pb = u[ma], u[mb]
            qa, qb = u[ta], u[tb]
            if np.linalg.norm(pb - pa, axis=1).min() <= 0.0:
                return None
            chains, (np_s, np_e, nq_s, nq_e), one_run = _chains(
                pa, pb, qa, qb, lv[ma], lv[mb], tol * 10.0, tol * 10.0)
            if not one_run:
                return None
            idx = loops_of(c)
            return _bend_chart(u[idx], pol[idx], pa, pb, np_s, np_e,
                               qa, qb, nq_s, nq_e, chains)

        def too_bent(c, u):
            """Whether a bent chart stretches, squashes or folds too much."""
            idx = tri_ids(c)
            w = tarea[idx]
            tot = w.sum()
            if not (tot > 0.0):
                return True
            s1, s2, mirrored = _stretch(u, t0[idx], t1[idx], t2[idx],
                                        tuple(f[idx] for f in frame), density)
            if w[mirrored].sum() > SMART_MAX_FLIP * tot:
                return True
            stretch = SMART_STRETCH_WEIGHT * np.maximum(s1 - 1.0, 0.0)
            squash = np.maximum(1.0 / np.maximum(s2, 1e-9) - 1.0, 0.0)
            score = np.minimum(stretch + squash, SMART_STRETCH_CAP)
            return float((score * w).sum() / tot) > distortion

        def attempt(c, n, bent_ok, occ, hull, long_now, cell_i):
            """Join chart c to the island along its edge with placed chart n.

            Tries a turn first. With `bent_ok`, a chart that does not fit
            that way is bent instead. Returns what the island needs to take
            the chart in, or None, and leaves the UVs as they were when it
            fails.
            """
            lo, hi = pairs[(min(c, n), max(c, n))]
            mine, theirs = (lo, hi) if c < n else (hi, lo)
            src = uv[mine]
            dst = uv[theirs]
            half = len(src) // 2
            edge = float(np.linalg.norm(src[:half] - src[half:],
                                        axis=1).sum())
            if edge <= 0.0:
                return None
            idx = loops_of(c)
            r, s, err = _fit(src, dst)
            if err <= SMART_MAX_MISFIT * edge:
                is_bent = False
                chull = hulls[c] @ r.T + s
            elif bent_ok:
                new = bend(c, mine, theirs, uv)
                if new is None or not np.isfinite(new).all():
                    return None
                is_bent = True
                chull = _hull(new)
            else:
                return None
            # The cheap tests first. The tile only needs the outline.
            grown = _hull(np.vstack([hull, chull]))
            ls = _long_side(grown)
            if ls > max(limit, long_now) * (1.0 + 1e-9):
                return None
            old = uv[idx].copy()
            uv[idx] = new if is_bent else old @ r.T + s
            ok = not (is_bent and too_bent(c, uv))
            if ok:
                keys = _raster(tris_of(c), cell_i)
                allow = min(SMART_MAX_OVERLAP * len(keys), edge / cell_i) + 2
                # A bent chart can land on itself, which an unbent one
                # cannot. Count the cells it covers twice.
                if is_bent:
                    own = len(keys) - len(np.unique(keys))
                    ok = own <= allow
            if ok:
                hit = len(occ.intersection(keys.tolist()))
                ok = hit <= allow
            if ok and len(keys) < SMART_FINE_KEYS:
                ok = not lands_small(c, edge)
            if not ok:
                uv[idx] = old
                return None
            return keys, grown, ls, (mine, theirs)

        def lands_small(c, edge):
            """Whether a small chart lands on the island, on a grid of its
            own. Only the island charts whose box meets the chart's box are
            rastered, and only inside that box."""
            tri_c = tris_of(c)
            fine = float(np.sqrt(max(carea_uv[c], 0.0) / SMART_SEED_CELLS))
            if not (fine > 0.0):
                return False
            pts = tri_c.reshape(-1, 2)
            lo, hi = pts.min(axis=0), pts.max(axis=0)
            win = (np.floor(lo / fine).astype(np.int64) - 1,
                   np.floor(hi / fine).astype(np.int64) + 1)
            ck = _raster(tri_c, fine)
            if not len(ck):
                return False
            b_lo = np.array([box[m][0] for m in members])
            b_hi = np.array([box[m][1] for m in members])
            meet = ((b_lo <= hi).all(axis=1) & (b_hi >= lo).all(axis=1))
            ik = set()
            for j in np.where(meet)[0]:
                ik.update(_raster(placed_tris[members[j]], fine, win).tolist())
            hit = len(ik.intersection(ck.tolist()))
            return hit > min(SMART_MAX_OVERLAP * len(ck), edge / fine) + 2

        import heapq
        island = {}
        rank = {}
        parent = {}
        n_islands = 0
        # Which joins were tried already, as (chart, placed neighbor, bent).
        # A chart refused against one neighbor is tried again when another
        # of its neighbors joins the island, because that edge may fit
        # better.
        tried = set()
        for seed in sorted((int(c) for c in work),
                           key=lambda c: -carea_3d[c]):
            if seed in island:
                continue
            kid = n_islands
            n_islands += 1
            island[seed] = kid
            rank[seed] = len(rank)
            cell_i = min(cell, float(np.sqrt(carea_uv[seed] / SMART_SEED_CELLS)))
            if not (cell_i > 0.0):
                cell_i = cell
            occ = set(_raster(tris_of(seed), cell_i).tolist())
            # The charts of this island and the box round each, for the
            # fine test of a small chart.
            members = [seed]
            sl = uv[loops_of(seed)]
            box = {seed: (sl.min(axis=0), sl.max(axis=0))}
            placed_tris = {seed: tris_of(seed)}
            hull = hulls[seed]
            long_now = _long_side(hull)
            heap = [(-carea_3d[c], c) for c in nbrs.get(seed, [])
                    if c not in island]
            heapq.heapify(heap)
            while heap:
                _neg, c = heapq.heappop(heap)
                if c in island:
                    continue
                placed = [n for n in nbrs[c] if island.get(n) == kid]
                # The longest shared edge first, and every turn before any
                # bend: a chart that fits one of its neighbors exactly is
                # never bent to fit another.
                placed.sort(
                    key=lambda n: -len(pairs[(min(c, n), max(c, n))][0]))
                got = None
                for bent_ok in ((False, True) if frame is not None
                                else (False,)):
                    for n in placed:
                        if (c, n, bent_ok) in tried:
                            continue
                        tried.add((c, n, bent_ok))
                        got = attempt(c, n, bent_ok, occ, hull, long_now,
                                      cell_i)
                        if got is not None:
                            got = got + (n,)
                            break
                    if got is not None:
                        break
                if got is None:
                    continue
                keys, hull, long_now, (mine, theirs), n = got
                island[c] = kid
                rank[c] = len(rank)
                parent[c] = n
                occ.update(keys.tolist())
                members.append(c)
                cl = uv[loops_of(c)]
                box[c] = (cl.min(axis=0), cl.max(axis=0))
                placed_tris[c] = tris_of(c)
                # The join in terms of the CAD charts, for the split.
                tree.append((int(base_chart[pol[mine[0]]]),
                             int(base_chart[pol[theirs[0]]])))
                for m2 in nbrs[c]:
                    if m2 not in island:
                        heapq.heappush(heap, (-carea_3d[m2], m2))

        for c in work:
            took_part.update(np.unique(base_chart[pol[loops_of(c)]]).tolist())

        # Weld each joined edge, so its two sides carry one UV and Blender
        # sees one island. An edge between two charts of one island that the
        # net did not use is welded too when it already fits, and stays a
        # cut when it does not. The welds are joined up first and each group
        # takes the UV of its earliest chart: a corner where three charts
        # meet then closes, where welding edge by edge would leave a gap at
        # it. A chart can hold one vertex twice, on both sides of a cut, so a
        # weld works on each UV copy of a vertex on its own.
        weld_tol = cell * 0.25
        wa, wb = [], []
        for (a, b), (lo, hi) in pairs.items():
            if island.get(a) is None or island.get(a) != island.get(b):
                continue
            is_tree = parent.get(a) == b or parent.get(b) == a
            if not is_tree and np.abs(uv[lo] - uv[hi]).max() > weld_tol:
                continue
            wa.append(lo)
            wb.append(hi)
        if not wa:
            return
        wa = np.concatenate(wa)
        wb = np.concatenate(wb)
        base = int(lv.max()) + 1
        lkey = lchart * base + lv
        near = max(tol * 10.0, weld_tol * 0.01)
        copy = _copies(lkey, uv, near)
        width = int(copy.max()) + 1
        ckey = lkey * width + copy
        ka, kb = ckey[wa], ckey[wb]
        nodes, inv = np.unique(np.concatenate([ka, kb]), return_inverse=True)
        inv = inv.ravel()
        m = len(wa)
        grp = _labels(inv[:m], inv[m:], len(nodes))
        # A group must not hold two copies of one vertex of one chart. That
        # happens where a chart is cut at a vertex and its neighbor meets it
        # on both sides of the cut through one corner: welding would drag
        # one side of the cut across the island. In such a group only the
        # pairs that already lie together stay welded.
        vkey = nodes // width
        o2 = np.lexsort((vkey, grp))
        twice = ((grp[o2][1:] == grp[o2][:-1])
                 & (vkey[o2][1:] == vkey[o2][:-1]))
        if twice.any():
            torn = np.isin(grp[inv[:m]], grp[o2][1:][twice])
            close = np.abs(uv[wa] - uv[wb]).sum(axis=1) <= 4.0 * weld_tol
            keep = ~torn | close
            grp = _labels(inv[:m][keep], inv[m:][keep], len(nodes))
        ref = np.empty(len(nodes), dtype=np.int64)
        ref[inv] = np.concatenate([wa, wb])
        rank_of = np.full(nc, np.iinfo(np.int64).max, dtype=np.int64)
        for c, rk in rank.items():
            rank_of[c] = rk
        node_rank = rank_of[nodes // width // base]
        first_of = {}
        for i in np.lexsort((node_rank, grp)).tolist():
            first_of.setdefault(int(grp[i]), i)
        target = {g: uv[ref[i]].copy() for g, i in first_of.items()}
        # Every loop of the same UV copy moves, not only the loop on the
        # joined edge.
        korder = np.argsort(ckey, kind="stable")
        ksorted = ckey[korder]
        lo_i = np.searchsorted(ksorted, nodes, side="left")
        hi_i = np.searchsorted(ksorted, nodes, side="right")
        for i in range(len(nodes)):
            uv[korder[lo_i[i]:hi_i[i]]] = target[int(grp[i])]

    grow(False)
    if sharp:
        grow(True)

    if not took_part:
        me.edges.foreach_set("use_seam", _seams(ne, man, ~joined(uv)))
        return 0, 0

    # The islands the joins made, over the CAD charts.
    ta = np.array([a for a, _b in tree], dtype=np.int64)
    tb = np.array([b for _a, b in tree], dtype=np.int64)
    comp = _labels(ta, tb, n_base)
    members = {}
    for c in sorted(took_part):
        members.setdefault(int(comp[c]), []).append(c)

    # Cut the islands that grew into shapes that pack badly.
    lbase = base_chart[pol]
    lorder = np.argsort(lbase, kind="stable")
    lcnt = np.bincount(lbase, minlength=n_base)
    lfirst = np.concatenate([[0], np.cumsum(lcnt)[:-1]])
    multi = {i: chs for i, chs in members.items() if len(chs) > 1}
    if split:
        hull_pts = {}
        for chs in multi.values():
            for c in chs:
                hull_pts[c] = _hull(
                    uv[lorder[lfirst[c]:lfirst[c] + lcnt[c]]])
        area_now = np.abs(np.bincount(base_chart[tp], weights=signed(uv),
                                      minlength=n_base))
        margin = SMART_SPLIT_MARGIN * side_3d * density
        pieces = _split_islands(multi, tree, hull_pts, area_now, margin,
                                SMART_SPLIT_GAIN)
    else:
        # Every island stays as it grew: one piece each.
        pieces = {c: k for k, chs in enumerate(multi.values()) for c in chs}
    final = {}
    n_out = 0
    for i, chs in members.items():
        if len(chs) < 2:
            final[chs[0]] = n_out
            n_out += 1
    for c, p in pieces.items():
        final[c] = n_out + p
    n_out += (max(pieces.values()) + 1) if pieces else 0

    # Every island a place of its own, so no two can touch by accident.
    lisland = np.full(nl, -1, dtype=np.int64)
    for c, kid in final.items():
        lisland[lorder[lfirst[c]:lfirst[c] + lcnt[c]]] = kid
    on = lisland >= 0
    step = (lisland[on] + 1).astype(np.float64)[:, None]
    uv[on] += step * np.array([0.8, 0.6]) * SMART_ISLAND_NUDGE

    layer.uv.foreach_set("vector", uv.astype(np.float32).ravel())
    me.edges.foreach_set("use_seam", _seams(ne, man, ~joined(uv)))
    return len(took_part), n_out


def _seams(ne, man, cut):
    """A seam on every edge with two faces where the UVs step."""
    seam = np.zeros(ne, dtype=bool)
    seam[man] = cut
    return seam


classes = ()
