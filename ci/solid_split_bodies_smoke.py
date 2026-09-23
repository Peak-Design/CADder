# SPDX-License-Identifier: GPL-3.0-or-later
"""Separate Solids on a colored multibody part: does each body keep its colors?

    blender -b --factory-startup --python-exit-code 1 -P ci/solid_split_bodies_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

The fixture is one part with no assembly structure: two boxes, each with a
body color and its faces in a color of their own, and a surface body next
to them, a free square face in a red face label. A SOLIDWORKS multibody
part with face colors exports like this.

The face labels are held against the part, not against a body. A split
body looked them up under its own key, found nothing, and came in all in
the body color.
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

# The nearest named colors to the fixture's colors.
BODY = "LIGHTGOLDENROD2"
FACE_A, FACE_B, SURFACE = "YELLOW", "BLUE", "RED"


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shell
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.gp import gp_Pnt, gp_Pln, gp_Dir
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorSurf
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    box_a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape()
    box_b = BRepPrimAPI_MakeBox(gp_Pnt(20, 0, 0), 10, 10, 10).Shape()
    square = BRepBuilderAPI_MakeFace(
        gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 40, 50, 0, 10).Face()
    builder = BRep_Builder()
    surface = TopoDS_Shell()
    builder.MakeShell(surface)
    builder.Add(surface, square)

    part = TopoDS_Compound()
    builder.MakeCompound(part)
    for shape in (box_a, box_b, surface):
        builder.Add(part, shape)

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    label = tool.AddShape(part, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString("multibody part"))

    def rgb(r, g, b):
        return Quantity_Color(r, g, b, Quantity_TOC_RGB)

    def color_faces(shape, color):
        ex = TopExp_Explorer(shape, TopAbs_FACE)
        while ex.More():
            sub = tool.AddSubShape(label, TopoDS.Face_s(ex.Current()))
            if not sub.IsNull():
                colors.SetColor(sub, color, XCAFDoc_ColorSurf)
            ex.Next()

    for solid, face_color in ((box_a, rgb(1.0, 1.0, 0.0)),
                              (box_b, rgb(0.0, 0.0, 1.0))):
        sub = tool.AddSubShape(label, solid)
        colors.SetColor(sub, rgb(0.85, 0.65, 0.13), XCAFDoc_ColorSurf)
        color_faces(solid, face_color)
    color_faces(surface, rgb(1.0, 0.0, 0.0))

    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="cadder_split_bodies_")
STEP = os.path.join(tmp, "multibody.step")
write_step(STEP)


def load(split):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    live = sys.modules["CADder.main"]
    live._cache_drop(STEP)
    live.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z",
                   tris_to_quads=False, uv_mode="SURFACE",
                   separate_solids=split)
    return sorted((o for o in bpy.data.objects
                   if o.type == "MESH" and len(o.data.polygons)),
                  key=lambda o: o.name)


def used(obj):
    """The material names the faces of `obj` use."""
    me = obj.data
    names = [s.material.name if s.material else "" for s in obj.material_slots]
    mi = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("material_index", mi)
    return sorted(set(names[i] for i in mi))


print("\n== the whole part")
whole = load(False)
check(len(whole) == 1, "one object with the option off (%d)" % len(whole))
colors_whole = used(whole[0]) if whole else []
check(colors_whole == sorted([FACE_A, FACE_B, SURFACE]),
      "every face in its face color (%s)" % colors_whole)

print("\n== the part split into bodies")
bodies = load(True)
per_body = [used(o) for o in bodies]
print("   bodies:", [(o.name, c) for o, c in zip(bodies, per_body)])
check([FACE_A] in per_body, "the first box keeps its face color")
check([FACE_B] in per_body, "the second box keeps its face color")
check(not any(BODY in c for c in per_body),
      "no face falls back to the body color")

# A second import takes the reader from the cache, and the labels the
# first one gave the bodies are still on it.
live = sys.modules["CADder.main"]
reader = live._cache_get(STEP)
before = sum(len(v) for v in reader.sub_shapes.values())
live.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z",
               tris_to_quads=False, uv_mode="SURFACE", separate_solids=True)
after = sum(len(v) for v in reader.sub_shapes.values())
check(after == before, "a cached import adds no label twice (%d, then %d)"
      % (before, after))

if FAILS:
    print("\nsolid_split_bodies_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nsolid_split_bodies_smoke: OK")
