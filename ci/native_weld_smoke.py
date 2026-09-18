# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the weld of a SolidWorks mesh.

SolidWorks sends each face of a body with its own copy of the points on its
edges. This sends a part of two cubes, face by face the same way, and with
the BODY section that says where the second cube starts. The cubes touch
along one face. The part must arrive as two closed cubes of 8 points each,
with every cube edge sharp and a UV seam, the flat shading of each face
kept, and the two cubes still apart where they touch.

Run:  blender -b --factory-startup -P native_weld_smoke.py
"""

import os
import struct
import sys
import tempfile

import bmesh
import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import native_import, swmesh  # noqa: E402

# One face of a unit cube: its normal, and its four corners in turn.
_FACES = (
    ((0, 0, -1), ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0))),
    ((0, 0, 1), ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))),
    ((0, -1, 0), ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1))),
    ((0, 1, 0), ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0))),
    ((-1, 0, 0), ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0))),
    ((1, 0, 0), ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1))),
)


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def _cube(shift, pos, nor, uv, tri):
    """Appends one cube, each face with points of its own and its own UV
    square, as the add-in sends it."""
    for normal, corners in _FACES:
        base = len(pos)
        for k, c in enumerate(corners):
            pos.append((c[0] + shift, c[1], c[2]))
            nor.append(normal)
            uv.append(((0, 0), (1, 0), (1, 1), (0, 1))[k])
        tri.extend([(base, base + 1, base + 2), (base, base + 2, base + 3)])


def write_scene(path):
    pos, nor, uv, tri = [], [], [], []
    _cube(0.0, pos, nor, uv, tri)
    second = len(pos)
    _cube(1.0, pos, nor, uv, tri)      # touches the first along x = 1

    data = struct.pack("<III", swmesh.MAGIC, swmesh.VERSION,
                       swmesh.FLAG_NORMALS | swmesh.FLAG_UVS)
    data += struct.pack("<d", 0.0005)
    data += struct.pack("<IIII", 1, 1, 1, 0)   # materials, defs, instances, nodes
    data += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    data += _text("") + struct.pack("<I", 0)

    data += struct.pack("<i", 1) + _text("blocks")
    data += struct.pack("<II", len(pos), len(tri))
    data += struct.pack("<%df" % (len(pos) * 3), *[c for p in pos for c in p])
    data += struct.pack("<%df" % (len(nor) * 3), *[c for n in nor for c in n])
    data += struct.pack("<%df" % (len(uv) * 2), *[c for t in uv for c in t])
    data += struct.pack("<%di" % (len(tri) * 3), *[i for t in tri for i in t])
    data += struct.pack("<%di" % len(tri), *([0] * len(tri)))

    rows = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    data += struct.pack("<i", 1) + _text("c001") + _text("blocks-1")
    data += _text("blocks-1") + struct.pack("<16d", *rows) + struct.pack("<B", 0)

    data += b"BODY" + struct.pack("<IIii", 12, 2, 0, second)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    path = write_scene(os.path.join(tempfile.gettempdir(), "native_weld_smoke.swmesh"))
    objects, _ = native_import.build(bpy.context, path)
    assert len(objects) == 1, [o.name for o in objects]
    me = objects[0].data

    # 1. Each cube is 8 points, not 24. The two cubes do not share the
    #    four points where they touch.
    assert len(me.vertices) == 16, len(me.vertices)
    assert len(me.polygons) == 24

    # 2. Both cubes are closed: every edge has exactly two faces.
    bm = bmesh.new()
    bm.from_mesh(me)
    faces_on_edge = sorted({len(e.link_faces) for e in bm.edges})
    pieces = len(_islands(bm))
    bm.free()
    assert faces_on_edge == [2], faces_on_edge
    assert pieces == 2, pieces

    # 3. The 24 cube edges are sharp and cut in the UV map. The diagonals
    #    inside the faces are neither.
    sharp = sum(1 for e in me.edges if e.use_edge_sharp)
    seams = sum(1 for e in me.edges if e.use_seam)
    assert sharp == 24, sharp
    assert seams == 24, seams

    # 4. The shading did not change: every corner has its face's normal.
    corner = np.empty(len(me.loops) * 3, dtype=np.float32)
    me.corner_normals.foreach_get("vector", corner)
    corner = corner.reshape(-1, 3)
    for poly in me.polygons:
        for i in poly.loop_indices:
            assert np.allclose(corner[i], poly.normal, atol=1e-4), (
                poly.index, corner[i], tuple(poly.normal))

    # 5. The UVs of each face came through as its own square.
    layer = me.uv_layers[0].data
    square = {(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)}
    for poly in me.polygons:
        got = {tuple(round(v, 4) for v in layer[i].uv) for i in poly.loop_indices}
        assert got <= square, got

    print("native_weld_smoke: OK: two cubes of 48 points arrive as two closed "
          "cubes of 8, sharp and seamed on the cube edges, shading and UVs kept, "
          "and still apart where they touch")


def _islands(bm):
    seen, found = set(), []
    for v in bm.verts:
        if v.index in seen:
            continue
        stack, piece = [v], set()
        while stack:
            x = stack.pop()
            if x.index in piece:
                continue
            piece.add(x.index)
            stack.extend(e.other_vert(x) for e in x.link_edges)
        seen |= piece
        found.append(piece)
    return found


main()
