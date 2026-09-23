# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the problems the native mesh extraction reports.

    blender -b --factory-startup --python-exit-code 1 -P ci/native_normals_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

The native module gives placeholder normals, and the importer calculates
the real ones from the CAD surface after it. The module once marked every
face as a face with undefined normals because of those placeholders. So
every import with the native module reported "Undefined normals" for each
face, also for a plate that has only planes and cylinders.
"""

import os
import sys

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

STEP = os.path.join(_HERE, "fixtures", "holes.step")

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
importer = sys.modules["CADder.importer"]
m = sys.modules["CADder.main"]
check(importer._HAS_NATIVE, "the native module is loaded (ABI %d)"
      % importer._NATIVE_ABI)
m._cache_drop(STEP)
m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z")
reader = m._cache_get(STEP)
problems = dict(reader.import_problems)
print("   problems:", problems)
check(problems.get("Undefined normals", 0) == 0,
      "no undefined normals on a plate of planes and cylinders (%d)"
      % problems.get("Undefined normals", 0))
check(problems.get("Triangulation", 0) == 0, "and no failed faces")
part = next(o for o in bpy.data.objects
            if o.type == "MESH" and "STEP_tag" in o)
check(len(part.data.polygons) > 0, "the part has a mesh")

if FAILS:
    print("\nnative_normals_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nnative_normals_smoke: OK: the native module reports only real "
      "problems")
