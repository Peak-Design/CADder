# SPDX-License-Identifier: GPL-3.0-or-later
"""A background import must give the same result as a direct one, and must
leave a refresh able to repeat it.

    blender -b --factory-startup --python-exit-code 1 -P ci/background_refresh_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

This is the path the "refresh shrank everything" report came from. Two
things were wrong with it and both are checked here.

  1. The worker starts from a factory scene, whose unit length is 1.0.
     load_step divides the file unit scale by that, so a file imported in
     the background into a scene set to anything else came out a different
     size than the same file imported directly.

  2. The worker records how it imported the file on ITS scene, and that
     scene is deleted once its content has been appended. The record went
     with it. A refresh then had nothing to repeat and fell back to the
     defaults, which do not know about a custom scale, so the assembly came
     back a thousand times smaller.

The worker is run for real, as a separate Blender, because a stub would not
prove the request reaches it.
"""
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import main as m, refresh as R, background as B

FAILS = []
UNIT = 0.01              # a scene that is NOT the worker's factory 1.0


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
    for i in range(2):
        shape = BRepPrimAPI_MakeBox(gp_Pnt(i * 30.0, 0, 0), 10, 10, 10).Shape()
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString("body%d" % i))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def clean(unit):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
    bpy.context.scene.unit_settings.scale_length = unit
    m._cache_drop(STEP)


def widths():
    """Measured off the mesh and the matrix, so no stale depsgraph value can
    hide a resize."""
    bpy.context.view_layer.update()
    out = []
    for obj in sorted(bpy.data.objects, key=lambda o: o.name):
        if obj.get("STEP_file") != STEP or obj.type != "MESH":
            continue
        if not len(obj.data.vertices):
            continue
        xs = [(obj.matrix_world @ v.co).x for v in obj.data.vertices]
        out.append(round(max(xs) - min(xs), 6))
    return out


tmp = tempfile.mkdtemp(prefix="stepper_bg_")
STEP = os.path.join(tmp, "bg_fixture.step")
write_step(STEP)

# -- what a direct import gives ----------------------------------------------

clean(UNIT)
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z", custom_scale=1.0)
direct = widths()
print("\n== direct import into a scene at %s: %s" % (UNIT, direct))
check(bool(direct), "the direct import produced geometry")

# -- the same import, run in the worker --------------------------------------

out_blend = os.path.join(tmp, "worker.blend")
request = {
    "addon_module": "STEPper_NEXT",
    "filepath": STEP,
    "out_blend": out_blend,
    "op_kwargs": {"hierarchy_types": "TREE",
                  "custom_scale": True, "user_scale": 1.0},
    "prefs": {},
    "parent_pid": os.getpid(),
    "scene_unit_scale": UNIT,
}
req_path = os.path.join(tmp, "request.json")
with open(req_path, "w", encoding="utf-8") as f:
    json.dump(request, f)

worker = os.path.join(_ADDON, "worker.py")
proc = subprocess.run(
    [bpy.app.binary_path, "-b", "--factory-startup", "--python-exit-code", "1",
     "--python", worker, "--", req_path],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
print("\n== worker exit %d" % proc.returncode)
if proc.returncode != 0:
    print(proc.stdout.decode("utf-8", "replace")[-3000:])
check(proc.returncode == 0, "the worker finished")
check(os.path.isfile(out_blend), "the worker wrote its blend")

# -- append it, exactly as the modal operator does ---------------------------

clean(UNIT)


class _Fake:
    pass


B.STEPPER_OT_background_import._append_result(_Fake(), bpy.context, out_blend)

appended = widths()
print("== appended: %s" % appended)
check(appended == direct,
      "the background import is the same size as the direct one (%s vs %s)"
      % (appended, direct))
check(R.settings_for(bpy.context.scene, STEP) is not None,
      "the import record came across with the objects")

# -- and a refresh repeats it ------------------------------------------------

bpy.ops.stepper.refresh_file(filepath=STEP)
refreshed = widths()
print("== after a refresh: %s" % refreshed)
check(refreshed == appended,
      "a refresh keeps the size (%s vs %s)" % (refreshed, appended))

# The same thing with the record thrown away, which is what every blend
# imported in the background before this fix looks like.
R.forget(bpy.context.scene, STEP)
bpy.ops.stepper.refresh_file(filepath=STEP)
check(widths() == appended,
      "and keeps it with no record at all (%s vs %s)" % (widths(), appended))

if FAILS:
    print("\nbackground_refresh_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nbackground_refresh_smoke: OK - a background import matches a direct "
      "one and a refresh repeats it")
