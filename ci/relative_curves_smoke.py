# SPDX-License-Identifier: GPL-3.0-or-later
"""Does Import Curves sample a sketch the same way with Relative
Tessellation on?

    blender -b --factory-startup --python-exit-code 1 -P ci/relative_curves_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

With Relative Tessellation the linear value is a share of each edge, not
a distance. The curve sampler has no relative mode, and it took the share
(0.005) as a distance in file units: 0.005 mm on a mm file. A sketch circle
came in with about seven times the points of a Balanced import.

The fixture is a box and a sketch circle of 10 mm radius. A relative
import must sample the circle as a Balanced import does.
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
from CADder import main as m, quality as quality_mod  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """A box and a sketch circle, as two parts of one assembly."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir, gp_Circ
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer

    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    builder.Add(comp, BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape())
    circle = gp_Circ(gp_Ax2(gp_Pnt(0, 40, 0), gp_Dir(0, 0, 1)), 10.0)
    builder.Add(comp, BRepBuilderAPI_MakeEdge(circle).Edge())

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    tool.AddShape(comp, True)  # True: one part per shape
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def curve_points(path, spec):
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for cu in list(bpy.data.curves):
        bpy.data.curves.remove(cu)
    m.load_step(bpy.context, path, htypes="EMPTIES", import_curves=True,
                deflection_spec=spec)
    return sum(len(s.points) for cu in bpy.data.curves for s in cu.splines)


tmp = tempfile.mkdtemp(prefix="cadder_relative_curves_")
try:
    path = os.path.join(tmp, "circle.step")
    write_step(path)

    balanced = curve_points(path, quality_mod.spec("BALANCED"))
    relative = curve_points(path, quality_mod.spec(
        None, relative=True, relative_distance=0.005))
    check(balanced > 2, "a Balanced import samples the circle (%d points)"
          % balanced)
    check(relative == balanced,
          "a relative import samples it the same (%d vs %d points)"
          % (relative, balanced))
finally:
    m._cache_drop(os.path.join(tmp, "circle.step"))
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print("\nrelative_curves_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nrelative_curves_smoke: OK")
