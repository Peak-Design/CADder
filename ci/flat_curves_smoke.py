# SPDX-License-Identifier: GPL-3.0-or-later
"""Does a Flat collection import with Import Curves leave no empty groups?

    blender -b --factory-startup --python-exit-code 1 -P ci/flat_curves_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Flat collection makes one group collection for each object it creates,
named after the object. A curve object is named "<part>.curves", so it
got a group of its own, and was then put in Cad Curves. Every part with a
sketch left an empty "<part>.curves" collection in the ".flat" one.

The fixture holds a box and a part with only a sketch line.
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """A box and a sketch line, as two parts of one assembly."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    from OCP.gp import gp_Pnt
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer

    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    builder.Add(comp, BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape())
    builder.Add(comp, BRepBuilderAPI_MakeEdge(
        gp_Pnt(0, 20, 0), gp_Pnt(30, 20, 0)).Edge())

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    tool.AddShape(comp, True)  # True: one part per shape
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="cadder_flat_curves_")
try:
    path = os.path.join(tmp, "sketch.step")
    write_step(path)
    m.load_step(bpy.context, path, htypes="FLAT", import_curves=True)

    curves = [o for o in bpy.data.objects if o.type == "CURVE"]
    check(len(curves) == 1, "the sketch came in as one curve object")
    cad_curves = bpy.data.collections.get("Cad Curves")
    check(cad_curves is not None and all(
        c.name in cad_curves.objects for c in curves),
          "the curve is in Cad Curves")

    flat = next((c for c in bpy.data.collections
                 if c.name.endswith(".flat")), None)
    check(flat is not None, "the import made a .flat collection")
    if flat is not None:
        empty = [c.name for c in flat.children if not c.all_objects]
        check(not empty, "no empty group in .flat (%s)" % empty)
        check(len(flat.children) >= 1 and all(
            c.all_objects for c in flat.children),
              "every group in .flat holds a part")
finally:
    m._cache_drop(os.path.join(tmp, "sketch.step"))
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print("\nflat_curves_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nflat_curves_smoke: OK")
