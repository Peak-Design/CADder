# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the colors of a defeatured STEP part, and for the
file cache that holds them.

    blender -b --factory-startup --python-exit-code 1 -P ci/defeature_colors_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

A defeatured part is a new shape with new faces. The colors of the old
faces go on to the new ones for the tessellation. The reader that holds the
colors stays in the file cache, so those entries must go when the part is
done. They once stayed, and each Regenerate of a defeatured part kept one
more copy of its shape, with its triangulation, alive in the cache.

The fixture is written here: a plate with four small holes, red as a part,
with a blue top face.
"""

import os
import sys
import tempfile

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

bpy.ops.preferences.addon_enable(module="CADder")

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 100.0, 60.0, 10.0).Shape()
    for x, y in ((12.0, 12.0), (88.0, 12.0), (12.0, 48.0), (88.0, 48.0)):
        axis = gp_Ax2(gp_Pnt(x, y, -1.0), gp_Dir(0.0, 0.0, 1.0))
        shape = BRepAlgoAPI_Cut(
            shape, BRepPrimAPI_MakeCylinder(axis, 3.0, 12.0).Shape()).Shape()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    label = shapes.AddShape(shape, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString("plate"))
    colors.SetColor(label, Quantity_Color(1.0, 0.0, 0.0, Quantity_TOC_RGB),
                    XCAFDoc_ColorType.XCAFDoc_ColorSurf)
    walker = TopExp_Explorer(shape, TopAbs_FACE)
    while walker.More():
        face = TopoDS.Face_s(walker.Current())
        surface = BRepAdaptor_Surface(face)
        if (surface.GetType().name == "GeomAbs_Plane"
                and abs(surface.Plane().Location().Z() - 10.0) < 1e-6):
            top = shapes.AddSubShape(label, face)
            colors.SetColor(top,
                            Quantity_Color(0.0, 0.0, 1.0, Quantity_TOC_RGB),
                            XCAFDoc_ColorType.XCAFDoc_ColorSurf)
        walker.Next()
    writer = STEPCAFControl_Writer()
    writer.Transfer(doc)
    writer.Write(path)


def tint(obj):
    """The colors on the mesh, as a set of rounded (r, g, b)."""
    layer = obj.data.color_attributes.get("Colors")
    if layer is None:
        return set()
    found = set()
    for item in layer.data:
        found.add(tuple(round(c, 1) for c in tuple(item.color)[:3]))
    return found


STEP = os.path.join(tempfile.mkdtemp(prefix="stepper_defeature_colors_"),
                    "plate.step")
write_step(STEP)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
m = sys.modules["CADder.main"]
m._cache_drop(STEP)
m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z")
part = next(o for o in bpy.data.objects
            if o.type == "MESH" and "STEP_tag" in o)
plain = tint(part)
print("   colors after the import:", sorted(plain))
faces = len(part.data.polygons)

part.cad_defeature.enabled = True
part.cad_defeature.size = 0.012
for o in bpy.context.view_layer.objects:
    o.select_set(o == part)
bpy.context.view_layer.objects.active = part

print("\n== the colors follow the defeatured faces")
bpy.ops.stepper.regenerate(use_scene_settings=False)
check(len(part.data.polygons) < faces,
      "the holes are gone (%d faces against %d)"
      % (len(part.data.polygons), faces))
check(tint(part) == plain,
      "the part keeps its colors (%s)" % sorted(tint(part)))

print("\n== the cached reader does not grow")
reader = m._cache_get(STEP)
sizes = []
for _ in range(4):
    bpy.ops.stepper.regenerate(use_scene_settings=False)
    sizes.append((len(reader.face_colors), len(reader.face_color_priority),
                  len(reader.sub_shapes)))
print("   (colors, priorities, sub shapes) after each:", sizes)
check(len(set(sizes)) == 1, "the same size after every Regenerate")
check(tint(part) == plain, "and the colors are still there")

if FAILS:
    print("\ndefeature_colors_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\ndefeature_colors_smoke: OK: the colors follow the defeatured part "
      "and the cache stays the same size")
