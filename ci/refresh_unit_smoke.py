# SPDX-License-Identifier: GPL-3.0-or-later
"""Does a refresh keep the size when the scene unit length changes twice?

    blender -b --factory-startup --python-exit-code 1 -P ci/refresh_unit_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

load_step divides the scale of the file by the scene unit length. When
the unit length changed after the import, a refresh passed a scale that
keeps the size, as a custom scale. load_step recorded it as the user's
custom scale. The next refresh then took that value as the user's own and
divided it by the new unit length: a mm file refreshed at 0.001 and then
at 1.0 came back 1000 times too small.

A refresh must keep the size through both changes, and must not record a
custom scale that the user did not set.
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
from CADder import main as m, refresh as R  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape()
    label = tool.AddShape(shape, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString("cube"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def width(path):
    """Measured off the mesh and the matrix, so no stale depsgraph value can
    hide a resize."""
    bpy.context.view_layer.update()
    for obj in bpy.data.objects:
        if obj.get("STEP_file") == path and obj.type == "MESH":
            xs = [(obj.matrix_world @ v.co).x for v in obj.data.vertices]
            return round(max(xs) - min(xs), 9)
    return None


tmp = tempfile.mkdtemp(prefix="cadder_refresh_unit_")
try:
    step = os.path.join(tmp, "cube.step")
    write_step(step)
    units = bpy.context.scene.unit_settings

    units.scale_length = 1.0
    m.load_step(bpy.context, step, htypes="EMPTIES", up_as="Z")
    size = width(step)
    check(size is not None and size > 0, "the import made the cube (%s)" % size)

    units.scale_length = 0.001
    check(bpy.ops.stepper.refresh_file(filepath=step) == {"FINISHED"},
          "the refresh at 0.001 finished")
    check(width(step) == size,
          "the size stays at 0.001 (%s vs %s)" % (width(step), size))
    record = R.settings_for(bpy.context.scene, step) or {}
    check(record.get("custom_scale") is None,
          "no custom scale is recorded (%r)" % record.get("custom_scale"))

    units.scale_length = 1.0
    check(bpy.ops.stepper.refresh_file(filepath=step) == {"FINISHED"},
          "the refresh back at 1.0 finished")
    check(width(step) == size,
          "the size stays back at 1.0 (%s vs %s)" % (width(step), size))

    # A custom scale the user did set is still recorded and kept.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    R.forget(bpy.context.scene, step)
    m._cache_drop(step)
    m.load_step(bpy.context, step, htypes="EMPTIES", up_as="Z",
                custom_scale=0.01)
    record = R.settings_for(bpy.context.scene, step) or {}
    check(record.get("custom_scale") == 0.01,
          "the user's custom scale is recorded (%r)"
          % record.get("custom_scale"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print("\nrefresh_unit_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nrefresh_unit_smoke: OK")
