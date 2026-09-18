# SPDX-License-Identifier: GPL-3.0-or-later
"""Joins the points a SolidWorks mesh carries more than once.

SolidWorks tessellates a body face by face, and every face keeps its own
copy of the points along its edges. That is what lets each point carry the
surface coordinates of its own face, but it also means no two faces touch.
The mesh then has two to six times the vertices it needs, every CAD edge is
a cut, and a tool that walks from face to face finds nothing to walk to.
The display mesh, the fallback for a body the tessellator refuses, is worse:
every triangle has three points of its own.

This joins the copies by position, and only inside one body, so two bodies
that touch stay two. The shape does not change and neither does the
shading: the normals and UVs of each face travel per corner, so each face
keeps what it had. What was a CAD edge is kept as marks on the edge. It is
sharp where the two faces do not meet smoothly, and a UV seam where their
UVs do not agree.

Pure numpy and no bpy, so it can be tested without Blender.
"""

from dataclasses import dataclass

import numpy as np

# Two copies of one point are the same float32 almost every time. The grid
# catches the few that are not. A tenth of a micron is far finer than any
# tessellation tolerance, and it is the grid the add-in itself uses to check
# that a body came out closed.
GRID = 1e-7

# Two faces meet smoothly across an edge when their normals at both ends
# are within about two degrees of each other.
SMOOTH = 0.9995

# The UVs of two faces agree at a point when they are this close.
UV_SAME = 1e-6


@dataclass
class Welded:
    positions: np.ndarray   # (V, 3) float32, one row for each joined point
    triangles: np.ndarray   # (T, 3) int64, into positions
    kept: np.ndarray        # (T,) the source triangle of each triangle
    corners: np.ndarray     # (T * 3,) the source point of each corner
    sharp: np.ndarray       # edge keys, see edge_keys()
    seams: np.ndarray       # edge keys


def edge_keys(a, b, count):
    """One number for each edge, the same whichever way it is walked."""
    a = np.asarray(a, dtype=np.int64)
    b = np.asarray(b, dtype=np.int64)
    return np.minimum(a, b) * count + np.maximum(a, b)


def weld(positions, triangles, normals=None, uvs=None, body_starts=None):
    """Joins the points of one mesh definition.

    `positions` holds three numbers for each point, `triangles` three
    point indices for each triangle, `normals` three numbers and `uvs` two
    numbers for each point. `body_starts` is the first point of each body.
    None means that the mesh is one body.

    A triangle that the weld closes up (two of its corners become one
    point) is dropped. It had no area to begin with.

    Without `body_starts`, two bodies that touch would be joined where they
    touch, and a face they share would then be there twice. When the weld
    would put more than two faces on one edge, the mesh is left as it came.
    A file from Bridge 1.0.0 has no body starts, and that is the only case
    this covers.
    """
    pos = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
    tri = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    known = body_starts is not None
    welded = _weld(pos, tri, normals, uvs, body_starts)
    if not known and _crowded(welded.triangles, len(welded.positions)):
        return Welded(positions=pos, triangles=tri,
                      kept=np.arange(len(tri)), corners=tri.reshape(-1),
                      sharp=np.zeros(0, dtype=np.int64),
                      seams=np.zeros(0, dtype=np.int64))
    return welded


def _crowded(wt, count):
    """True when an edge of the mesh has more than two faces on it."""
    if not len(wt):
        return False
    ca = wt.reshape(-1)
    cb = wt[:, [1, 2, 0]].reshape(-1)
    _, size = np.unique(edge_keys(ca, cb, count), return_counts=True)
    return bool((size > 2).any())


def _weld(pos, tri, normals, uvs, body_starts):
    count = len(pos)

    # The key of a point is its place on the grid, and its body.
    key = np.round(pos.astype(np.float64) / GRID).astype(np.int64)
    if body_starts is not None and len(body_starts) > 1:
        starts = np.asarray(body_starts, dtype=np.int64)
        body = np.searchsorted(starts, np.arange(count), side="right") - 1
        key = np.column_stack([body, key])
    if count:
        _, first, inverse = np.unique(
            key, axis=0, return_index=True, return_inverse=True)
        inverse = np.asarray(inverse).reshape(-1)
    else:
        first = np.zeros(0, dtype=np.int64)
        inverse = np.zeros(0, dtype=np.int64)

    # Number the joined points in the order they first came, so the mesh
    # keeps the order SolidWorks gave it and not the order of the grid.
    order = np.argsort(first, kind="stable")
    rank = np.empty(len(order), dtype=np.int64)
    rank[order] = np.arange(len(order))
    new_of_old = rank[inverse]
    new_pos = pos[first[order]]

    wt = new_of_old[tri] if len(tri) else np.zeros((0, 3), dtype=np.int64)
    closed = ((wt[:, 0] == wt[:, 1]) | (wt[:, 1] == wt[:, 2])
              | (wt[:, 0] == wt[:, 2]))
    kept = np.flatnonzero(~closed)
    wt = wt[kept]
    corners = tri[kept].reshape(-1)

    # A point only a closed-up triangle used is left over. Take it out, so
    # the mesh has no loose points.
    used = np.zeros(len(new_pos), dtype=bool)
    used[wt.reshape(-1)] = True
    if not used.all():
        renumber = np.cumsum(used) - 1
        wt = renumber[wt]
        new_pos = new_pos[used]

    sharp, seams = _edge_marks(wt, corners, len(new_pos), normals, uvs)
    return Welded(positions=new_pos, triangles=wt, kept=kept,
                  corners=corners, sharp=sharp, seams=seams)


def _edge_marks(wt, corners, count, normals, uvs):
    """The edges where the two faces do not meet smoothly (sharp) and the
    edges where their UVs do not agree (seams), as edge keys."""
    empty = np.zeros(0, dtype=np.int64)
    if not len(wt) or (normals is None and uvs is None):
        return empty, empty

    # Corner c runs from its point a to the next corner's point b.
    loops = np.arange(len(wt) * 3).reshape(-1, 3)
    nxt = loops[:, [1, 2, 0]].reshape(-1)
    ca = wt.reshape(-1)
    cb = ca[nxt]
    keys = edge_keys(ca, cb, count)

    # The edges two corners share: an edge between two faces. An open edge
    # has one, and an edge where more than two faces meet is left alone.
    order = np.argsort(keys, kind="stable")
    sk = keys[order]
    start = np.flatnonzero(np.r_[True, sk[1:] != sk[:-1]])
    size = np.diff(np.r_[start, len(sk)])
    pair = start[size == 2]
    if not len(pair):
        return empty, empty
    i = order[pair]
    j = order[pair + 1]
    edge = sk[pair]

    # The corner of each side at each end of the edge. The two faces
    # usually walk the edge in opposite directions, so j's own corner is at
    # the far end. A face turned over walks it the same way.
    same = ca[j] == ca[i]
    i_at_a, i_at_b = i, nxt[i]
    j_at_a = np.where(same, j, nxt[j])
    j_at_b = np.where(same, nxt[j], j)

    def split(values, width, apart):
        """True for each edge whose two sides disagree at either end."""
        v = np.asarray(values, dtype=np.float64).reshape(-1, width)
        return (apart(v[corners[i_at_a]], v[corners[j_at_a]])
                | apart(v[corners[i_at_b]], v[corners[j_at_b]]))

    sharp = empty
    if normals is not None:
        sharp = edge[split(normals, 3, _bent)]
    seams = empty
    if uvs is not None:
        seams = edge[split(uvs, 2, _apart)]
    return sharp, seams


def _bent(p, q):
    return np.einsum("ij,ij->i", p, q) < SMOOTH


def _apart(p, q):
    return np.abs(p - q).max(axis=1) > UV_SAME
