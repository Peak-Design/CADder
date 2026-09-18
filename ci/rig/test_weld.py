# SPDX-License-Identifier: GPL-3.0-or-later
"""The weld that joins the copies of a point SolidWorks sends once for each
face. It must join what one body shares, keep two bodies apart, and say
which of the old face boundaries were sharp or cut in the UV map."""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import weld  # noqa: E402


def _faces(*faces):
    """Joins face patches the way SolidWorks sends them: each face with its
    own points. A face is (points, triangles, normal, uv offset)."""
    pos, tri, nor, uv = [], [], [], []
    for points, triangles, normal, shift in faces:
        base = len(pos)
        for p in points:
            pos.append(p)
            nor.append(normal)
            uv.append((p[0] + shift, p[1] + shift))
        tri.extend([(base + a, base + b, base + c) for a, b, c in triangles])
    return (np.array(pos, dtype=np.float32), np.array(tri),
            np.array(nor, dtype=np.float32), np.array(uv, dtype=np.float32))


def _key(w, p, q):
    """The edge key of the edge between two joined points, found by place."""
    def find(x):
        return int(np.flatnonzero(np.all(np.isclose(w.positions, x), axis=1))[0])
    return int(weld.edge_keys(find(p), find(q), len(w.positions)))


def test_two_faces_that_meet_at_an_edge_share_its_points():
    # A flat square as two CAD faces, each a triangle, with the diagonal
    # sent twice.
    pos, tri, nor, uv = _faces(
        ([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [(0, 1, 2)], (0, 0, 1), 0.0),
        ([(0, 0, 0), (1, 1, 0), (0, 1, 0)], [(0, 1, 2)], (0, 0, 1), 0.0))
    w = weld.weld(pos, tri, nor, uv)
    assert len(w.positions) == 4
    assert len(w.triangles) == 2
    assert len(w.sharp) == 0
    assert len(w.seams) == 0


def test_a_fold_is_sharp_and_a_uv_jump_is_a_seam():
    # Two faces at a right angle along the X axis, and each face has its
    # own UV chart.
    pos, tri, nor, uv = _faces(
        ([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)], (0, 0, 1), 0.0),
        ([(0, 0, 0), (0, 0, 1), (1, 0, 0)], [(0, 1, 2)], (0, 1, 0), 5.0))
    w = weld.weld(pos, tri, nor, uv)
    assert len(w.positions) == 4
    fold = _key(w, (0, 0, 0), (1, 0, 0))
    assert list(w.sharp) == [fold]
    assert list(w.seams) == [fold]


def test_a_smooth_join_is_not_sharp():
    # A bend of one degree: the two faces of a fine tessellation of one
    # curved surface. Both sides carry the same normal on the shared edge.
    pos, tri, nor, uv = _faces(
        ([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)], (0, 0, 1), 0.0),
        ([(0, 0, 0), (1, 0, 0), (0, -1, 0.02)], [(0, 2, 1)], (0, 0, 1), 0.0))
    w = weld.weld(pos, tri, nor, uv)
    assert len(w.sharp) == 0


def test_the_display_mesh_every_triangle_its_own_points_joins_up():
    # A fan of eight triangles round one point, each with three points of
    # its own, as the display-mesh fallback sends them.
    ring = [(np.cos(a), np.sin(a), 0.0)
            for a in np.linspace(0, 2 * np.pi, 9)[:-1]]
    faces = [([(0, 0, 0), ring[k], ring[(k + 1) % 8]], [(0, 1, 2)],
              (0, 0, 1), 0.0) for k in range(8)]
    pos, tri, nor, uv = _faces(*faces)
    assert len(pos) == 24
    w = weld.weld(pos, tri, nor, uv)
    assert len(w.positions) == 9
    assert len(w.sharp) == 0


def test_two_bodies_that_touch_stay_two():
    # Two squares that share an edge, sent as two bodies.
    pos, tri, nor, uv = _faces(
        ([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [(0, 1, 2)], (0, 0, 1), 0.0),
        ([(0, 0, 0), (1, 1, 0), (0, 1, 0)], [(0, 1, 2)], (0, 0, 1), 0.0))
    apart = weld.weld(pos, tri, nor, uv, body_starts=[0, 3])
    assert len(apart.positions) == 6
    # A file from an add-in that did not say where the bodies start joins
    # them. That is harmless here: every edge still has two faces at most.
    assert len(weld.weld(pos, tri, nor, uv, body_starts=None).positions) == 4


def test_bodies_that_share_a_face_are_left_alone_when_nothing_says_so():
    # Two blocks pressed together, reduced to what matters: both bodies
    # have a triangle on the same three points, facing each other. A weld
    # of the whole part would put the face there twice, so a part like this
    # from an add-in with no body starts is left exactly as it came.
    pos, tri, nor, uv = _faces(
        ([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)], (0, 0, 1), 0.0),
        ([(0, 0, 0), (1, 0, 0), (0, 0, 1)], [(0, 1, 2)], (0, 1, 0), 0.0),
        ([(0, 0, 0), (0, 1, 0), (1, 0, 0)], [(0, 1, 2)], (0, 0, -1), 0.0))
    w = weld.weld(pos, tri, nor, uv)
    assert len(w.positions) == 9
    assert w.triangles.tolist() == tri.tolist()
    assert len(w.sharp) == 0 and len(w.seams) == 0
    # With the body starts the first body is joined and the second is not.
    w = weld.weld(pos, tri, nor, uv, body_starts=[0, 6])
    assert len(w.positions) == 4 + 3


def test_a_triangle_the_weld_closes_up_is_dropped():
    # A sliver whose two points are closer than the grid.
    pos = np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 0, 1e-9)],
                   dtype=np.float32)
    tri = np.array([(0, 1, 2), (0, 1, 3)])
    w = weld.weld(pos, tri)
    assert len(w.triangles) == 1
    assert list(w.kept) == [0]
    assert len(w.positions) == 3


def test_corners_point_back_to_the_source_points():
    # The UVs and normals of each corner are read through `corners`, so it
    # must name the point the corner came from, not the joined one.
    pos, tri, nor, uv = _faces(
        ([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [(0, 1, 2)], (0, 0, 1), 0.0),
        ([(0, 0, 0), (1, 1, 0), (0, 1, 0)], [(0, 1, 2)], (0, 0, 1), 3.0))
    w = weld.weld(pos, tri, nor, uv)
    assert list(w.corners) == [0, 1, 2, 3, 4, 5]
    for corner, point in enumerate(w.triangles.reshape(-1)):
        assert np.allclose(w.positions[point], pos[w.corners[corner]])


def test_an_empty_definition_welds_to_nothing():
    w = weld.weld(np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.int64))
    assert len(w.positions) == 0
    assert len(w.triangles) == 0
