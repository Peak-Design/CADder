# SPDX-License-Identifier: GPL-3.0-or-later
"""Tris to Quads at import: does it pair the triangles, and does it leave
the shape and the shading alone?

    blender -b --factory-startup --python-exit-code 1 -P ci/tris_to_quads_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

The point of the shape checks
-----------------------------
Joining triangles is not a remesh. No vertex may move, no vertex may go,
and the custom split normals the importer wrote from the CAD surface must
come through. A quad mesh that shades flat is worse than the triangles.

Blender stores a custom normal as an offset from the normal it calculates,
and a quad has a different calculated normal than its two triangles. So a
join through bmesh turns every custom normal near it, by up to 22 degrees
on a real part. The check compares each corner with the triangle import,
because a check that only looks for "some curved shading" passed on that.
Clean Up Meshes does the same kind of join, so it gets the same check.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy
import numpy as np

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """A block with a hole and rounded edges.

    The flat faces and the hole have no vertex inside a curved face, and
    that is where a join turns a custom normal. The rounded corners have
    them, so the shading check has something to catch.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    block = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 30, 10).Shape()
    fillet = BRepFilletAPI_MakeFillet(block)
    edges = TopExp_Explorer(block, TopAbs_EDGE)
    while edges.More():
        fillet.Add(3.0, TopoDS.Edge_s(edges.Current()))
        edges.Next()
    block = fillet.Shape()
    hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(20, 15, -1), gp_Dir(0, 0, 1)), 6, 12).Shape()
    shape = BRepAlgoAPI_Cut(block, hole).Shape()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    label = tool.AddShape(shape, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString("drilled block"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_quads_")
STEP = os.path.join(tmp, "block.step")
write_step(STEP)


def load(**kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)
    opts = dict(htypes="FLAT", up_as="Z")
    opts.update(kw)
    m.load_step(bpy.context, STEP, **opts)
    return sorted([o for o in bpy.data.objects
                   if o.type == "MESH" and len(o.data.polygons)],
                  key=lambda o: -len(o.data.polygons))[0]


def verts_of(obj):
    co = np.empty(len(obj.data.vertices) * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def corner_normals(obj):
    me = obj.data
    out = np.empty(len(me.loops) * 3, dtype=np.float64)
    me.corner_normals.foreach_get("vector", out)
    return out.reshape(-1, 3)


def sides(obj):
    counts = {}
    for p in obj.data.polygons:
        counts[len(p.vertices)] = counts.get(len(p.vertices), 0) + 1
    return counts


print("\n== the option can be turned off")
plain = load(tris_to_quads=False)
# Read everything now. The next load resets the file, and an object held
# across that reset is already dead.
base_v = verts_of(plain)
base_n = corner_normals(plain)
base_cv = np.empty(len(plain.data.loops), dtype=np.int64)
plain.data.loops.foreach_get("vertex_index", base_cv)
base_sides = sides(plain)
base_faces = len(plain.data.polygons)
check(set(base_sides) == {3},
      "with it off the import is all triangles (%s)" % base_sides)

print("\n== it is on by default, and pairs the triangles")
quad = load()
got = sides(quad)
check(4 in got and got[4] > 0, "quads come out (%s)" % got)
check(len(quad.data.polygons) < base_faces,
      "the face count drops (%d -> %d)"
      % (base_faces, len(quad.data.polygons)))
share = got.get(4, 0) / float(sum(got.values()))
check(share > 0.5, "most faces are quads (%.0f%%)" % (share * 100.0))

print("\n== it is not a remesh")
new_v = verts_of(quad)
check(len(new_v) == len(base_v),
      "no vertex is added or lost (%d vs %d)" % (len(base_v), len(new_v)))
if len(new_v) == len(base_v):
    check(np.allclose(np.sort(new_v, axis=0), np.sort(base_v, axis=0),
                      atol=1e-9),
          "no vertex moves")

print("\n== the CAD shading survives")
check(quad.data.has_custom_normals or len(quad.data.corner_normals),
      "the mesh still carries corner normals")
flat_face = 0
for poly in quad.data.polygons:
    face = poly.normal
    if all(quad.data.corner_normals[i].vector.dot(face) > 0.0
           for i in poly.loop_indices):
        continue
    flat_face += 1
check(flat_face == 0,
      "every corner normal stays on the side of its own face (%d bad)"
      % flat_face)
# The hole is a cylinder. Its corner normals must still lean off the face,
# which is what makes it shade round rather than faceted.
curved = [p for p in quad.data.polygons
          if any(quad.data.corner_normals[i].vector.dot(p.normal) < 0.999
                 for i in p.loop_indices)]
check(len(curved) > 0,
      "the curved faces keep a normal off the CAD surface (%d faces)"
      % len(curved))


def worst_turn(obj):
    """The largest angle between a corner normal and the triangle import.

    Each corner is matched to the triangle corners at the vertex in the
    same place, and the closest of those counts. Matching by place, not by
    index, lets a check follow a mesh that lost merged vertices.
    """
    pos = verts_of(obj)
    idx = np.empty(len(obj.data.loops), dtype=np.int64)
    obj.data.loops.foreach_get("vertex_index", idx)
    normals = corner_normals(obj)
    key = lambda p: tuple(np.round(p, 5))
    at = {}
    for c, v in enumerate(base_cv):
        at.setdefault(key(base_v[v]), []).append(base_n[c])
    worst = 0.0
    for c, v in enumerate(idx):
        near = at.get(key(pos[v]))
        if not near:
            continue
        best = max(float(np.dot(n, normals[c])) for n in near)
        worst = max(worst, float(np.degrees(np.arccos(min(1.0, best)))))
    return worst


turn = worst_turn(quad)
check(turn < 0.5,
      "every corner shades as it did on the triangles (worst %.2f deg)"
      % turn)

print("\n== Clean Up Meshes keeps the CAD shading")
for o in bpy.data.objects:
    o.select_set(o == quad)
bpy.context.view_layer.objects.active = quad
faces = len(quad.data.polygons)
bpy.ops.stepper.mesh_cleanup()
check(len(quad.data.polygons) < faces,
      "the cleanup dissolves faces (%d -> %d)"
      % (faces, len(quad.data.polygons)))
turn = worst_turn(quad)
check(turn < 0.5,
      "every corner shades as it did on the triangles (worst %.2f deg)"
      % turn)
check(quad.data.attributes.get("stepper_normal") is None,
      "the working copy of the normals is gone")

print("\n== a refresh reproduces it")
rec = quad.get("STEP_import_settings") or quad.get("import_record_json")
check(rec is not None and "tris_to_quads" in str(rec),
      "the setting is stamped on the object for a refresh")

if FAILS:
    print("\ntris_to_quads_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\ntris_to_quads_smoke: OK - quads, same shape, same CAD shading")
