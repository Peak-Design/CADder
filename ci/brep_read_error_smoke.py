# SPDX-License-Identifier: GPL-3.0-or-later
"""A damaged BREP file must give an import error, not a traceback.

    blender -b --factory-startup --python-exit-code 1 -P ci/brep_read_error_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

load_step catches only AssertionError from the reader. The BREP reader
passed on the OCCT exception of a bad file, so a direct import showed a
traceback, and Batch Import Folder stopped at the bad file and did not
import the files after it.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_box_brep(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), path)


def write_box_step(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    w = STEPControl_Writer()
    w.Transfer(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(),
               STEPControl_AsIs)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="cadder_brep_err_")

good_brep = os.path.join(tmp, "good.brep")
write_box_brep(good_brep)
text = open(good_brep, "rb").read()

random_brep = os.path.join(tmp, "random.brep")
with open(random_brep, "wb") as f:
    f.write(bytes((i * 131 + 7) % 256 for i in range(4096)))

cut_brep = os.path.join(tmp, "cut.brep")
with open(cut_brep, "wb") as f:
    f.write(text[:len(text) // 2])

for label, path in (("random bytes", random_brep),
                    ("a text file cut in half", cut_brep)):
    print("\n== %s" % label)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    try:
        result = m.load_step(bpy.context, path, htypes="FLAT", up_as="Z")
        raised = None
    except Exception as exc:
        result = None
        raised = "%s: %s" % (type(exc).__name__, exc)
    check(raised is None, "%s: load_step does not raise (%s)"
          % (label, raised or "no exception"))
    check(result is False, "%s: load_step reports a file it cannot open "
          "(%r)" % (label, result))

print("\n== the good file still imports")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
result = m.load_step(bpy.context, good_brep, htypes="FLAT", up_as="Z")
check(result is not False and any(o.type == "MESH" for o in bpy.data.objects),
      "an undamaged BREP file imports")

print("\n== Batch Import Folder goes past a damaged file")
folder = os.path.join(tmp, "batch")
os.makedirs(folder)
with open(os.path.join(folder, "a_damaged.brep"), "wb") as f:
    f.write(text[:len(text) // 2])
write_box_step(os.path.join(folder, "b_good.step"))
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
try:
    status = bpy.ops.stepper.batch_import_folder(directory=folder)
    raised = None
except Exception as exc:
    status = None
    raised = "%s: %s" % (type(exc).__name__, exc)
check(raised is None, "the batch does not stop on the damaged file (%s)"
      % (raised or "no exception"))
good = [o for o in bpy.data.objects
        if o.type == "MESH" and "b_good" in (o.get("STEP_file") or "")]
check(bool(good), "the file after the damaged one is imported")

if FAILS:
    print("\nbrep_read_error_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nbrep_read_error_smoke: OK: a damaged BREP file is an import error")
