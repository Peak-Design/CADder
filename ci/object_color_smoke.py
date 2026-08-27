# SPDX-License-Identifier: GPL-3.0-or-later
"""The object color must match the CAD color the STEP file states.

    blender -b --factory-startup --python-exit-code 1 -P ci/object_color_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

obj.color is what a Solid viewport shows with Color set to Object. The
importer has always put the CAD color in the material, and nothing outside
Material Preview reads a material, so a solid viewport showed a gray
assembly no matter what the file said.

The colors are compared against the material the importer built, not against
the numbers written into the file. That keeps the test about the feature and
not about color space conversion, which is the importer's business and is
already covered where it belongs.

A refresh has to follow the file here, because the color is now something
the file describes. A color the user picked themselves is theirs and stays.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import main as m

FAILS = []
DEFAULT = (1.0, 1.0, 1.0, 1.0)      # what Blender gives an object with no help


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path, colors):
    """One box per color, each carrying that color in the STEP file."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colours = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    for i, (r, g, b) in enumerate(colors):
        shape = BRepPrimAPI_MakeBox(gp_Pnt(i * 30.0, 0, 0), 10, 10, 10).Shape()
        label = shapes.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString("body%d" % i))
        colours.SetColor(label, Quantity_Color(r, g, b, Quantity_TOC_sRGB),
                         XCAFDoc_ColorType.XCAFDoc_ColorGen)
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
    m._cache_drop(STEP)


def parts():
    return sorted([o for o in bpy.data.objects
                   if o.get("STEP_file") == STEP and o.type == "MESH"
                   and len(o.data.vertices)],
                  key=lambda o: o.name)


def material_color(obj):
    """The material's base color, read the way a shader would."""
    mats = [ms for ms in obj.data.materials if ms is not None]
    if not mats:
        return None
    return m._material_base_color(mats[0])


def same(a, b, eps=1e-5):
    return all(abs(x - y) < eps for x, y in zip(a, b))


tmp = tempfile.mkdtemp(prefix="stepper_objcol_")
STEP = os.path.join(tmp, "colors.step")
FIRST = [(0.9, 0.1, 0.1), (0.1, 0.7, 0.2), (0.15, 0.25, 0.85)]
SECOND = [(0.2, 0.8, 0.8), (0.85, 0.6, 0.05), (0.5, 0.1, 0.6)]
write_step(STEP, FIRST)

# -- the import ---------------------------------------------------------------

print("\n== the object color matches the material")
clean()
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z")
objs = parts()
check(len(objs) == 3, "three parts imported (%d)" % len(objs))

matched = 0
distinct = set()
for obj in objs:
    want = material_color(obj)
    if want is None:
        continue
    distinct.add(tuple(round(c, 4) for c in want))
    if same(obj.color[:3], want):
        matched += 1
    else:
        print("      %s: object %s, material %s"
              % (obj.name, tuple(round(c, 4) for c in obj.color[:3]),
                 tuple(round(c, 4) for c in want)))
check(matched == len(objs), "every part carries its CAD color (%d of %d)"
      % (matched, len(objs)))
check(len(distinct) == 3,
      "the parts really are three different colors (%d)" % len(distinct))
check(not any(same(o.color, DEFAULT) for o in objs),
      "and none was left at the default white")
check(all(o.get("STEP_object_color") for o in objs),
      "the import stamped what it wrote")

# Alpha is untouched: this importer reads no transparency out of STEP.
check(all(abs(o.color[3] - 1.0) < 1e-9 for o in objs), "alpha stays at 1")

# Objects with no material must not be given a color out of nowhere.
print("\n== other hierarchy modes")
for mode in ("FLAT", "EMPTIES", "COLLECTION_INSTANCES"):
    clean()
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    objs = parts()
    good = sum(1 for o in objs if same(o.color[:3], material_color(o) or ()))
    check(objs and good == len(objs),
          "%s: every part carries its CAD color (%d of %d)"
          % (mode, good, len(objs)))
    empties = [o for o in bpy.data.objects
               if o.get("STEP_file") == STEP and o.type == "EMPTY"]
    check(all(same(o.color, DEFAULT) for o in empties),
          "%s: hierarchy empties were left alone (%d)" % (mode, len(empties)))

# -- a refresh ----------------------------------------------------------------

print("\n== a refresh")
clean()
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z")
before = {o.name: tuple(o.color) for o in parts()}

bpy.ops.stepper.refresh_file(filepath=STEP)
check({o.name: tuple(o.color) for o in parts()} == before,
      "an unchanged file keeps the colors exactly")

# The part is recolored in CAD.
write_step(STEP, SECOND)
bpy.ops.stepper.refresh_file(filepath=STEP)
objs = parts()
check(all(same(o.color[:3], material_color(o)) for o in objs),
      "a recolor in CAD comes through")
check(not any(same(tuple(o.color), before.get(o.name, ())) for o in objs),
      "and the colors really did change")

# A color the user picked is theirs.
mine = (0.123, 0.456, 0.789, 1.0)
objs[0].color = mine
name = objs[0].name
write_step(STEP, FIRST)
bpy.ops.stepper.refresh_file(filepath=STEP)
check(same(bpy.data.objects[name].color, mine),
      "a color the user picked survives a refresh (%s)"
      % (tuple(round(c, 4) for c in bpy.data.objects[name].color),))
others = [o for o in parts() if o.name != name]
check(others and all(same(o.color[:3], material_color(o)) for o in others),
      "while the parts they did not touch follow the file")

if FAILS:
    print("\nobject_color_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nobject_color_smoke: OK - the object color matches the CAD color, "
      "a refresh follows the file, and a color the user picked stays")
