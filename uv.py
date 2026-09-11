# UV layer helpers for STEPper NEXT.
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
# CAD Surface gives every CAD face a flat chart of its own, in length units.
# On a plane that chart is exact, and on a cylinder it is the surface rolled
# out, circumference by height. So joining a face to an island needs no
# solver: turn its chart until the shared edge lies on the island's edge.
# That is how a paper model is unfolded, and the checks below decide which
# folds the net may make.

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

# How badly a shared edge may fit, as a share of its length. A plane and a
# cylinder fit exactly. A sphere or a torus chart is only close to its true
# shape, and past this the join would visibly bend the face to close it.
SMART_MAX_MISFIT = 0.02

# How much of a face may land on the island before the join counts as an
# overlap, as a share of the face. A little is allowed for the grid itself,
# which is coarse where two faces meet.
SMART_MAX_OVERLAP = 0.02

# How far apart two islands are set. The net turns charts into new places,
# and a chart that is refused can start an island where a neighbor's edge
# now lies. Blender joins two faces into one island when their shared edge
# carries the same UV, so without this the two islands would read as one,
# on top of each other. It matches the nudge each CAD face already gets.
SMART_ISLAND_NUDGE = 0.001

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


def _hull(pts):
    """Convex hull of 2D points, counter-clockwise (monotone chain)."""
    pts = np.unique(np.asarray(pts, dtype=np.float64), axis=0)
    if len(pts) < 3:
        return pts
    p = [tuple(x) for x in pts]

    def turn(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for q in p:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    upper = []
    for q in reversed(p):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return np.array(lower[:-1] + upper[:-1], dtype=np.float64)


def _long_side(hull):
    """Long side of the smallest rectangle round a convex hull.

    The packer turns islands to fit them, so what has to fit the tile is
    this rectangle, not the box the island happens to sit in.
    """
    if len(hull) < 2:
        return 0.0
    if len(hull) == 2:
        return float(np.linalg.norm(hull[1] - hull[0]))
    e = np.roll(hull, -1, axis=0) - hull
    ln = np.linalg.norm(e, axis=1)
    keep = ln > 1e-15
    if not keep.any():
        return 0.0
    d = e[keep] / ln[keep, None]
    n = np.stack([-d[:, 1], d[:, 0]], axis=1)
    pu = hull @ d.T
    pv = hull @ n.T
    w = pu.max(axis=0) - pu.min(axis=0)
    h = pv.max(axis=0) - pv.min(axis=0)
    k = int(np.argmin(w * h))
    return float(max(w[k], h[k]))


def _raster(tris, cell):
    """The grid cells whose centers fall inside the triangles, as keys.

    Testing cell centers, not cell areas, is what lets two faces share an
    edge without counting as an overlap: a center lies on one side of the
    edge or the other, never on both.
    """
    if not len(tris):
        return np.zeros(0, dtype=np.int64)
    g = tris / cell
    mn = np.floor(g.min(axis=1)).astype(np.int64)
    mx = np.floor(g.max(axis=1)).astype(np.int64)
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
    # No duplicates to remove: the triangles of one chart do not overlap,
    # and a center on the edge between two of them counts for neither.
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


def smart_merge(me, side_3d=None):
    """Grow UV islands out of the CAD charts of one mesh.

    Starts from the largest chart and joins its tangent neighbors one at a
    time, largest first. A neighbor is turned so its shared edge lies on
    the island, and it joins only if it then fits that edge, does not land
    on the island, and does not make the island too long for the tile. A
    neighbor that fails waits for a later island. The next island starts
    from the largest chart still left, until none is left.

    `side_3d` is the side of the UV tile, in the mesh's own length units.
    None means one tile for this part alone, which is the side of the square
    its surface would pack into. The tile is never smaller than the longest
    chart, because a CAD face is never split.

    Writes the UVs and puts the seams on the island boundaries. Returns
    (charts, islands) for the charts that had a tangent neighbor.
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
    sharp = np.zeros(ne, dtype=bool)
    me.edges.foreach_get("use_edge_sharp", sharp)

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

    span = float(np.ptp(uv, axis=0).max()) if nl else 1.0
    tol = max(span, 1e-12) * 1e-7

    def joined(u):
        return ((np.abs(u[a1] - u[a2]).sum(axis=1) <= tol)
                & (np.abs(u[b1] - u[b2]).sum(axis=1) <= tol))

    # Charts: the faces the parametric UVs already hold together.
    cont = joined(uv)
    p1, p2 = pol[l1], pol[l2]
    _ids, chart = np.unique(_labels(p1[cont], p2[cont], npoly),
                            return_inverse=True)
    nc = int(chart.max()) + 1

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

    # Every chart the same way up. A face whose surface runs the other way
    # arrives mirrored, and a mirrored chart cannot be turned into place.
    tchart = chart[tp]
    csign = np.bincount(tchart, weights=signed(uv), minlength=nc)
    flip = csign < 0
    lchart = chart[pol]
    uv[flip[lchart], 0] *= -1.0
    carea_uv = np.abs(np.bincount(tchart, weights=signed(uv), minlength=nc))
    carea_3d = np.bincount(chart, weights=parea, minlength=nc)

    # Tangent joins: an edge between two charts that is not sharp.
    between = (chart[p1] != chart[p2]) & ~sharp[man]
    if not between.any():
        me.edges.foreach_set("use_seam", _seams(ne, man, ~cont))
        return 0, 0
    ja, jb = chart[p1][between], chart[p2][between]
    ja1, jb1, ja2, jb2 = a1[between], b1[between], a2[between], b2[between]
    comp = _labels(ja, jb, nc)
    sizes = np.bincount(comp, minlength=nc)
    busy = sizes[comp] > 1
    work = np.where(busy)[0]
    if not len(work):
        me.edges.foreach_set("use_seam", _seams(ne, man, ~cont))
        return 0, 0

    # For each pair of charts, the loops that meet along their shared edge:
    # (loop in the lower chart, loop in the higher chart), both ends.
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
    # Both arrays hold the first ends of the shared edges, then the second
    # ends in the same order, so an edge is item i and item i + half.
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

    def tris_of(c):
        idx = torder[tfirst[c]:tfirst[c] + tcnt[c]]
        return np.stack([uv[t0[idx]], uv[t1[idx]], uv[t2[idx]]], axis=1)

    hulls = {int(c): _hull(uv[loops_of(c)]) for c in work}

    # The tile. It is never smaller than the longest chart of the part,
    # because a chart is one CAD face and is never split.
    density = np.sqrt(carea_uv.sum() / max(carea_3d.sum(), 1e-30))
    if side_3d is None:
        side_3d = np.sqrt(carea_3d.sum() / SMART_FILL)
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
        me.edges.foreach_set("use_seam", _seams(ne, man, ~cont))
        return 0, 0

    import heapq
    island = {}
    rank = {}
    moved = {}
    parent = {}
    n_islands = 0
    for seed in sorted((int(c) for c in work), key=lambda c: -carea_3d[c]):
        if seed in island:
            continue
        kid = n_islands
        n_islands += 1
        island[seed] = kid
        rank[seed] = len(rank)
        occ = set(_raster(tris_of(seed), cell).tolist())
        hull = hulls[seed]
        long_now = _long_side(hull)
        heap = [(-carea_3d[c], c) for c in nbrs.get(seed, [])
                if c not in island]
        heapq.heapify(heap)
        tried = set()
        while heap:
            _neg, c = heapq.heappop(heap)
            if c in island or c in tried:
                continue
            tried.add(c)
            placed = [n for n in nbrs[c] if island.get(n) == kid]
            if not placed:
                continue

            def shared(n):
                lo, hi = pairs[(min(c, n), max(c, n))]
                return (lo, hi) if c < n else (hi, lo)

            best = max(placed, key=lambda n: len(shared(n)[0]))
            mine, theirs = shared(best)
            src = uv[mine]
            dst = uv[theirs]
            if best in moved:
                r0, s0 = moved[best]
                dst = dst @ r0.T + s0
            half = len(src) // 2
            edge = float(np.linalg.norm(src[:half] - src[half:],
                                        axis=1).sum())
            r, s, err = _fit(src, dst)
            if edge <= 0.0 or err > SMART_MAX_MISFIT * edge:
                continue
            tri = tris_of(c) @ r.T + s
            keys = _raster(tri, cell)
            hit = len(occ.intersection(keys.tolist()))
            if hit > SMART_MAX_OVERLAP * len(keys) + 2:
                continue
            grown = _hull(np.vstack([hull, hulls[c] @ r.T + s]))
            ls = _long_side(grown)
            if ls > max(limit, long_now) * (1.0 + 1e-9):
                continue
            island[c] = kid
            rank[c] = len(rank)
            moved[c] = (r, s)
            parent[c] = best
            occ.update(keys.tolist())
            hull = grown
            long_now = ls
            for n in nbrs[c]:
                if n not in island:
                    heapq.heappush(heap, (-carea_3d[n], n))

    # Put every chart where the net placed it.
    for c, (r, s) in moved.items():
        idx = loops_of(c)
        uv[idx] = uv[idx] @ r.T + s

    # Weld each joined edge, so its two sides carry one UV and Blender sees
    # one island. An edge between two charts of one island that the net did
    # not use is welded too when it already fits, and stays a cut when it
    # does not. The welds are joined up first and each group takes the UV of
    # its earliest chart: a corner where three charts meet then closes, where
    # welding edge by edge would leave a gap at it.
    weld_tol = cell * 0.25
    wa, wb = [], []
    for (a, b), (lo, hi) in pairs.items():
        if island.get(a) is None or island.get(a) != island.get(b):
            continue
        tree = parent.get(a) == b or parent.get(b) == a
        if not tree and np.abs(uv[lo] - uv[hi]).max() > weld_tol:
            continue
        wa.append(lo)
        wb.append(hi)
    if wa:
        wa = np.concatenate(wa)
        wb = np.concatenate(wb)
        base = int(lv.max()) + 1
        ka = lchart[wa] * base + lv[wa]
        kb = lchart[wb] * base + lv[wb]
        nodes, inv = np.unique(np.concatenate([ka, kb]), return_inverse=True)
        m = len(wa)
        grp = _labels(inv[:m], inv[m:], len(nodes))
        ref = np.empty(len(nodes), dtype=np.int64)
        ref[inv] = np.concatenate([wa, wb])
        rank_of = np.full(nc, np.iinfo(np.int64).max, dtype=np.int64)
        for c, rk in rank.items():
            rank_of[c] = rk
        node_rank = rank_of[nodes // base]
        first_of = {}
        for i in np.lexsort((node_rank, grp)).tolist():
            first_of.setdefault(int(grp[i]), i)
        target = {g: uv[ref[i]].copy() for g, i in first_of.items()}
        # Every loop of a chart at a welded vertex moves, not only the loop
        # on the joined edge. A loop on the chart's own closure seam has a
        # different UV at the same vertex, and it stays where it is.
        lkey = lchart * base + lv
        korder = np.argsort(lkey, kind="stable")
        ksorted = lkey[korder]
        lo_i = np.searchsorted(ksorted, nodes, side="left")
        hi_i = np.searchsorted(ksorted, nodes, side="right")
        near = max(tol * 10.0, weld_tol * 0.01)
        old = uv[ref].copy()
        for i in range(len(nodes)):
            idx = korder[lo_i[i]:hi_i[i]]
            sel = idx[np.abs(uv[idx] - old[i]).sum(axis=1) <= near]
            uv[sel] = target[int(grp[i])]

    # Every island a place of its own, so no two can touch by accident.
    if island:
        lisland = np.full(nl, -1, dtype=np.int64)
        for c, kid in island.items():
            lisland[loops_of(c)] = kid
        on = lisland >= 0
        step = (lisland[on] + 1).astype(np.float64)[:, None]
        uv[on] += step * np.array([0.8, 0.6]) * SMART_ISLAND_NUDGE

    layer.uv.foreach_set("vector", uv.astype(np.float32).ravel())
    me.edges.foreach_set("use_seam", _seams(ne, man, ~joined(uv)))
    return int(len(work)), n_islands


def _seams(ne, man, cut):
    """A seam on every edge with two faces where the UVs step."""
    seam = np.zeros(ne, dtype=bool)
    seam[man] = cut
    return seam


classes = ()
