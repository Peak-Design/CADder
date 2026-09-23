# SPDX-License-Identifier: GPL-3.0-or-later
"""The Select button of the STEP File panel works in every hierarchy mode.

    blender -b --factory-startup --python-exit-code 1 -P ci/refresh_select_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

In COLLECTION_INSTANCES mode the part meshes sit in a collection that is
excluded from the view layer, and a mesh comes before its instancing empty
in name order ('alpha' before 'alpha.001'). The button skipped the objects
it could not select, but then made the first object of the file active
anyway, and Blender refuses an object that is not in the view layer. The
button failed with a Python error.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, refresh as R

FAILS = []
MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """Two parts in one assembly."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    top = tool.NewShape()
    TDataStd_Name.Set_s(top, TCollection_ExtendedString("top"))
    for i, name in enumerate(("alpha", "beta")):
        leaf = tool.AddShape(BRepPrimAPI_MakeBox(
            gp_Pnt(0, 0, 0), 10.0 + i, 10, 10).Shape(), False)
        TDataStd_Name.Set_s(leaf, TCollection_ExtendedString(name))
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(30.0 * i, 0, 0))
        tool.AddComponent(top, leaf, TopLoc_Location(trsf))
    tool.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_select_")
STEP = os.path.join(tmp, "select_fixture.step")
write_step(STEP)

for mode in MODES:
    print("\n== Select: %s" % mode)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    in_layer = set(bpy.context.view_layer.objects)
    try:
        result = bpy.ops.stepper.select_file_objects(filepath=STEP)
    except RuntimeError as exc:
        result = {"RAISED: %s" % str(exc).strip().splitlines()[-1]}
    check(result == {"FINISHED"}, "%s: the button finished (%s)"
          % (mode, result))
    selected = set(bpy.context.selected_objects)
    want = {o for o in R.file_objects(STEP) if o in in_layer}
    check(selected == want,
          "%s: every object of the file in the view layer is selected "
          "(%d of %d)" % (mode, len(selected), len(want)))
    active = bpy.context.view_layer.objects.active
    check(active is not None and active in want,
          "%s: the active object is one of them (%s)"
          % (mode, active.name if active else None))


if FAILS:
    print("\nrefresh_select_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nrefresh_select_smoke: OK: Select works in every hierarchy mode")
