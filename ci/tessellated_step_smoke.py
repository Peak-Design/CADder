# SPDX-License-Identifier: GPL-3.0-or-later
"""A STEP face that holds only a mesh must import, not crash Blender.

    blender -b --factory-startup --python-exit-code 1 -P ci/tessellated_step_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

AP242 can carry a part as triangles only (TESSELLATED_SHELL, no
ADVANCED_FACE). OCCT reads such a face with its triangulation and no
surface. The UV chart pass and the normal pass read the surface of every
face, and on a face with no surface OCCT reads a null pointer. That is an
access violation, which no try can catch, so Blender closed with no message.
A crash here is the failure: the script does not get to its last line.

Both mesh paths are run: the native module, and the Python fallback that
runs when the native module is missing or fails.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, importer

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_mesh_only_step(path):
    """A 10 mm box written as triangles only, with no B-rep surface."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.Poly import Poly_Triangle, Poly_Triangulation
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Shell

    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    BRepMesh_IncrementalMesh(box, 0.5)
    builder = BRep_Builder()
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    ex = TopExp_Explorer(box, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        tri = BRep_Tool.Triangulation_s(face, TopLoc_Location())
        # The mesher winds the triangles along the surface. A face that is
        # turned the other way in the box gets its triangles turned here,
        # so that every triangle of the fixture faces out.
        flip = face.Orientation() == TopAbs_REVERSED
        out = Poly_Triangulation(tri.NbNodes(), tri.NbTriangles(), False)
        for i in range(1, tri.NbNodes() + 1):
            out.SetNode(i, tri.Node(i))
        for i in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(i).Get()
            out.SetTriangle(i, Poly_Triangle(b, a, c) if flip
                            else Poly_Triangle(a, b, c))
        bare = TopoDS_Face()
        builder.MakeFace(bare, out)
        builder.Add(shell, bare)
        ex.Next()

    # The writer registers the write settings, so make it first.
    w = STEPControl_Writer()
    schema = Interface_Static.CVal_s("write.step.schema")
    tess = Interface_Static.IVal_s("write.step.tessellated")
    try:
        Interface_Static.SetCVal_s("write.step.schema", "AP242DIS")
        Interface_Static.SetIVal_s("write.step.tessellated", 1)
        w.Transfer(shell, STEPControl_AsIs)
        w.Write(path)
    finally:
        Interface_Static.SetCVal_s("write.step.schema", schema)
        Interface_Static.SetIVal_s("write.step.tessellated", tess)
    text = open(path, encoding="utf-8", errors="replace").read()
    return "TRIANGULATED_FACE" in text and "ADVANCED_FACE" not in text


def import_and_measure(label, **kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)
    print("\n== %s" % label)
    kw.setdefault("tris_to_quads", False)
    result = m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z", **kw)
    check(result is not False, "%s: the file opens" % label)
    meshes = [o for o in bpy.data.objects
              if o.type == "MESH" and len(o.data.polygons)]
    check(len(meshes) == 1, "%s: one part with geometry (%d)"
          % (label, len(meshes)))
    if not meshes:
        return
    me = meshes[0].data
    if not kw["tris_to_quads"]:
        check(len(me.polygons) == 12, "%s: the box has its 12 triangles "
              "(%d)" % (label, len(me.polygons)))

    # Each corner normal must point the same way as its triangle, and each
    # triangle must face out of the box.
    center = sum((v.co for v in me.vertices), me.vertices[0].co * 0.0)
    center /= len(me.vertices)
    wrong = inward = 0
    for poly in me.polygons:
        if poly.normal.dot(poly.center - center) <= 0.0:
            inward += 1
        for li in poly.loop_indices:
            if me.corner_normals[li].vector.dot(poly.normal) < 0.9:
                wrong += 1
                break
    check(inward == 0, "%s: every triangle faces out (%d inward)"
          % (label, inward))
    check(wrong == 0, "%s: the shading normals follow the triangles "
          "(%d wrong)" % (label, wrong))

    uv = me.uv_layers.active
    finite = uv is not None and all(
        abs(d.uv[0]) < 1e6 and abs(d.uv[1]) < 1e6 and
        d.uv[0] == d.uv[0] and d.uv[1] == d.uv[1] for d in uv.data)
    check(finite, "%s: the UV map holds only finite values" % label)


tmp = tempfile.mkdtemp(prefix="cadder_tess_")
STEP = os.path.join(tmp, "mesh_only_box.step")
check(write_mesh_only_step(STEP),
      "the fixture holds triangles only and no B-rep face")

import_and_measure("native module")

native = importer._HAS_NATIVE
importer._HAS_NATIVE = False
try:
    import_and_measure("Python fallback")
finally:
    importer._HAS_NATIVE = native

# The passes after the mesh build must take the zero UVs of such a face.
for mode, pack in (("SURFACE", "ALL"), ("SMART", "OBJECT"),
                   ("CONFORMAL", "NONE"), ("BOX", "UDIM")):
    import_and_measure("UV %s, pack %s, quads" % (mode, pack),
                       uv_mode=mode, uv_pack=pack, tris_to_quads=True)

if FAILS:
    print("\ntessellated_step_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\ntessellated_step_smoke: OK - a mesh-only STEP imports on both mesh "
      "paths")
