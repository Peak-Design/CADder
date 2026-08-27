# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the direct link: a .swmesh must become real Blender
objects (instanced, placed, colored and tagged) without a STEP file or
a matching pass anywhere in sight.

The scene is written here rather than by the add-in, so the test runs with
no SolidWorks: two occurrences of one definition (which is what proves
instancing) plus a second definition, two materials, and a transform that
is not the identity.

Run:  blender -b --factory-startup -P native_smoke.py
"""

import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from STEPper_NEXT.rig import native_import, swmesh  # noqa: E402


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_scene(path):
    """A .swmesh built by hand: byte for byte what MeshWriter.cs emits."""
    # One unit square (2 triangles) and one triangle, so the two definitions
    # are told apart by counts alone.
    square_v = [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0]
    square_t = [0, 1, 2, 0, 2, 3]
    square_m = [1, 0]
    tri_v = [0, 0, 0, 2, 0, 0, 0, 2, 0]
    tri_t = [0, 1, 2]
    tri_m = [1]

    body = struct.pack("<III", swmesh.MAGIC, swmesh.VERSION,
                       swmesh.FLAG_NORMALS | swmesh.FLAG_UVS)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<III", 2, 2, 3)          # materials, defs, instances

    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0) + _text("")
    body += _text("blue") + struct.pack("<6f", 0.1, 0.2, 0.9, 1.0, 0.3, 0.0) + _text("")

    for did, name, verts, tris, mats in (
            (10, "plate", square_v, square_t, square_m),
            (11, "wedge", tri_v, tri_t, tri_m)):
        nv = len(verts) // 3
        nt = len(tris) // 3
        body += struct.pack("<i", did) + _text(name)
        body += struct.pack("<II", nv, nt)
        body += struct.pack("<%df" % len(verts), *verts)
        body += struct.pack("<%df" % (nv * 3), *([0.0, 0.0, 1.0] * nv))
        body += struct.pack("<%df" % (nv * 2), *([0.25, 0.75] * nv))
        body += struct.pack("<%di" % len(tris), *tris)
        body += struct.pack("<%di" % nt, *mats)

    def instance(did, cid, name, tx, ty, tz):
        rows = [1, 0, 0, tx, 0, 1, 0, ty, 0, 0, 1, tz, 0, 0, 0, 1]
        return struct.pack("<i", did) + _text(cid) + _text(name) \
            + struct.pack("<16d", *rows)

    # c001 and c002 SHARE definition 10: the instancing case.
    body += instance(10, "c001", "plate-1", 0.0, 0.0, 0.0)
    body += instance(10, "c002", "plate-2", 5.0, 0.0, 0.0)
    body += instance(11, "c003", "wedge-1", 0.0, 3.0, 1.0)

    with open(path, "wb") as fh:
        fh.write(body)
    return path


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    path = write_scene(os.path.join(tempfile.gettempdir(), "native_smoke.swmesh"))

    objects, report = native_import.build(bpy.context, path)

    assert len(objects) == 3, [o.name for o in objects]
    assert len(report.matched) == 3
    assert not report.unmatched
    assert all(e.confidence == "exact" for e in report.matched)

    by_component = {o["RIG_component_id"]: o for o in objects}
    assert set(by_component) == {"c001", "c002", "c003"}

    # 1. Instancing: two objects, one mesh datablock, shared not copied.
    a, b = by_component["c001"], by_component["c002"]
    assert a.data is b.data, "the two plates should share one mesh"
    assert a.data.users >= 2
    assert by_component["c003"].data is not a.data

    # 2. Geometry survived the wire.
    assert len(a.data.vertices) == 4 and len(a.data.polygons) == 2
    assert len(by_component["c003"].data.vertices) == 3

    # 3. Placement: the transform is row-major, so the translation is the
    #    fourth COLUMN. Reading it as a row puts the second plate at y=5.
    assert tuple(round(v, 6) for v in b.matrix_world.translation) == (5.0, 0.0, 0.0)
    assert tuple(round(v, 6) for v in by_component["c003"].matrix_world.translation) \
        == (0.0, 3.0, 1.0)

    # 4. Materials are per TRIANGLE, and the plate's two triangles differ.
    assert len(a.data.materials) == 2
    indices = [p.material_index for p in a.data.polygons]
    assert indices == [1, 0], indices
    assert a.data.materials[1].name.endswith("blue")

    # 5. UVs came through, per loop.
    assert a.data.uv_layers, "no UV layer"
    assert len(a.data.uv_layers[0].data) == len(a.data.loops)

    # 6. A re-send REPLACES rather than accumulating, and takes its orphaned
    #    meshes with it: an unreferenced million-triangle datablock is
    #    invisible in the outliner and very much present in the .blend.
    meshes_before = len(bpy.data.meshes)
    objects2, _ = native_import.build(bpy.context, path)
    assert len(objects2) == 3
    assert len(bpy.data.meshes) == meshes_before, (
        "re-import leaked %d mesh datablock(s)"
        % (len(bpy.data.meshes) - meshes_before))
    assert len([o for o in bpy.data.objects if o.get("RIG_component_id")]) == 3

    print("native_smoke: OK: %d objects from %d mesh datablocks, instanced, "
          "placed, per-triangle materials, replaceable"
          % (len(objects2), len({o.data.name for o in objects2})))


main()
