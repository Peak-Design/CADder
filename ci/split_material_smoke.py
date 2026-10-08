# SPDX-License-Identifier: GPL-3.0-or-later
"""Split by Material gives each material of a STEP part its own object.

    blender -b --factory-startup --python-exit-code 1 -P ci/split_material_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

A user splits faces in the CAD application to put graphics on a product,
and gives those faces another color. In Blender they separated each part
by material by hand, and did it again after each refresh of the file. The
import now does it: a part with faces in two or more materials comes in as
that many objects.

  1. With the option, a part of two colors is two objects of one material
     each, and together they hold all the faces of the part. A part of one
     color stays one object. Without the option nothing changes.
  2. In each hierarchy mode.
  3. A refresh of the file keeps the pieces: the same objects, and a
     material that the user gave one of them with its materials locked.
  4. A color that changed in the file: the piece of the old color goes,
     a piece of the new color comes, and the other piece is not touched.
  5. Rebuild from STEP of one piece gives that piece again, not the part.
"""

import os
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

# The add-on first: it puts its OpenCASCADE on the path, which the fixture
# below is written with.
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as _main  # noqa: E402,F401

FAILS = []
MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path, accent=(1.0, 0.0, 0.0)):
    """A yellow housing with two faces in the accent color, and a plain
    blue block."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopoDS import TopoDS
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.gp import gp_Pnt
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorSurf
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    def rgb(r, g, b):
        return Quantity_Color(r, g, b, Quantity_TOC_RGB)

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    housing = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 20, 30).Shape()
    label = tool.AddShape(housing, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString("housing"))
    colors.SetColor(label, rgb(1.0, 1.0, 0.0), XCAFDoc_ColorSurf)
    ex = TopExp_Explorer(housing, TopAbs_FACE)
    n = 0
    while ex.More():
        if n < 2:
            sub = tool.AddSubShape(label, TopoDS.Face_s(ex.Current()))
            colors.SetColor(sub, rgb(*accent), XCAFDoc_ColorSurf)
        n += 1
        ex.Next()

    block = BRepPrimAPI_MakeBox(gp_Pnt(50, 0, 0), 5, 5, 5).Shape()
    plain = tool.AddShape(block, False)
    TDataStd_Name.Set_s(plain, TCollection_ExtendedString("block"))
    colors.SetColor(plain, rgb(0.0, 0.0, 1.0), XCAFDoc_ColorSurf)

    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="cadder_split_material_")
STEP = os.path.join(tmp, "product.step")
write_step(STEP)


def live():
    return sys.modules["CADder.main"]


def load(split, mode="FLAT"):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    live()._cache_drop(STEP)
    live().load_step(bpy.context, STEP, htypes=mode, up_as="Z",
                     tris_to_quads=False, uv_mode="SURFACE",
                     split_by_material=split)
    return meshes()


def meshes():
    """STEP name -> the mesh object, for the parts with faces. A part that
    is a collection instance is read from its prototype."""
    out = {}
    for obj in bpy.data.objects:
        if obj.type == "MESH" and len(obj.data.polygons) \
                and obj.get("STEP_name"):
            out[obj["STEP_name"]] = obj
    return out


def used(obj):
    """The material names that the faces of `obj` use."""
    me = obj.data
    names = [s.material.name if s.material else "" for s in obj.material_slots]
    mi = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("material_index", mi)
    return sorted(set(names[i] for i in mi))


def faces(objs):
    return sum(len(o.data.polygons) for o in objs)


print("1. a part of two colors is two objects")
whole = load(False)
check(sorted(whole) == ["block", "housing"],
      "without the option: one object for each part (%s)" % sorted(whole))
check(len(used(whole["housing"])) == 2,
      "the housing has two materials (%s)" % used(whole["housing"]))
total = len(whole["housing"].data.polygons)
box = sorted(tuple(round(c, 4) for c in v.co) for v in whole["housing"].data.vertices)
YELLOW, RED = used(whole["housing"])[::-1] if used(whole["housing"])[0] == "RED" \
    else used(whole["housing"])
if "RED" not in (YELLOW, RED):
    FAILS.append("the fixture colors are %s" % ((YELLOW, RED),))

cut = load(True)
pieces = {n: o for n, o in cut.items() if n.startswith("housing")}
check(sorted(pieces) == sorted(["housing." + YELLOW, "housing." + RED]),
      "with the option: one object for each material (%s)" % sorted(cut))
check("block" in cut and len(cut) == 3,
      "a part of one material stays one object (%s)" % sorted(cut))
for name, obj in pieces.items():
    check(used(obj) == [name.partition(".")[2]]
          and len(obj.material_slots) == 1,
          "%s holds its one material (%s)" % (name, used(obj)))
    check(obj.get("STEP_piece") == name.partition(".")[2],
          "%s says which piece it is (%s)" % (name, obj.get("STEP_piece")))
check(faces(pieces.values()) == total,
      "the pieces hold all the faces of the part (%d of %d)"
      % (faces(pieces.values()), total))
corners = sorted(set(tuple(round(c, 4) for c in v.co)
                     for o in pieces.values() for v in o.data.vertices))
check(corners == sorted(set(box)),
      "the pieces have the corners of the part, with no gap")

print("2. in each hierarchy mode")
for mode in MODES:
    got = load(True, mode)
    names = sorted(n for n in got if n.startswith("housing"))
    check(names == sorted(["housing." + YELLOW, "housing." + RED])
          and faces(got[n] for n in names) == total,
          "%s: the two pieces, with all the faces (%s)" % (mode, sorted(got)))

print("3. a refresh keeps the pieces")
for mode in MODES:
    got = load(True, mode)
    was = {n: got[n] for n in got}
    # The mesh of a collection instance is on a prototype that is not in
    # the view layer, so it cannot be selected for the lock here.
    lock = mode != "COLLECTION_INSTANCES"
    if lock:
        graphic = bpy.data.materials.new("Graphic")
        red = got["housing." + RED]
        red.data.materials[0] = graphic
        red.select_set(True)
        bpy.context.view_layer.objects.active = red
        bpy.ops.stepper.material_lock()
    bpy.ops.stepper.refresh_file(filepath=STEP)
    now = meshes()
    check(sorted(now) == sorted(was),
          "%s: the refresh keeps the same parts (%s)" % (mode, sorted(now)))
    check(all(now.get(n) is was[n] for n in was),
          "%s: the pieces are the same objects" % mode)
    if lock:
        check(used(now["housing." + RED]) == ["Graphic"],
              "%s: the piece keeps the material of the user (%s)"
              % (mode, used(now["housing." + RED])))
    check(faces(now[n] for n in now if n.startswith("housing")) == total,
          "%s: the pieces still hold all the faces" % mode)

print("4. a color that changed in the file")
got = load(True)
yellow = got["housing." + YELLOW]
write_step(STEP, accent=(0.0, 1.0, 0.0))
live()._cache_drop(STEP)
bpy.ops.stepper.refresh_file(filepath=STEP)
now = meshes()
new = sorted(n for n in now if n.startswith("housing"))
check(len(new) == 2 and "housing." + YELLOW in new
      and "housing." + RED not in new,
      "the piece of the old color went, and one of the new color came (%s)"
      % new)
check(now.get("housing." + YELLOW) is yellow,
      "the piece whose color did not change is the same object")
check(faces(now[n] for n in new) == total,
      "the pieces hold all the faces after the change")
write_step(STEP)

print("5. Rebuild from STEP of one piece")
got = load(True)
red = got["housing." + RED]
count = len(red.data.polygons)
for obj in bpy.data.objects:
    obj.select_set(obj is red)
bpy.context.view_layer.objects.active = red
bpy.ops.object.occ_rebuild_selected()
check(len(red.data.polygons) == count and used(red) == [RED],
      "the piece is the same piece after a rebuild (%d faces of %d, %s)"
      % (len(red.data.polygons), count, used(red)))

if FAILS:
    print("\nsplit_material_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nsplit_material_smoke: OK")
