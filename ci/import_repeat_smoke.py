# SPDX-License-Identifier: GPL-3.0-or-later
"""The same file imported again: same place, same report, fresh content.

    blender -b --factory-startup --python-exit-code 1 -P ci/import_repeat_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

A refresh runs the import again with the settings it recorded. The import
put every part at the 3D cursor, and the cursor was not in the record, so
a refresh after a click in the viewport put the parts where the cursor was
NOW. A refresh reads a part it did not put back where it was as a move in
CAD.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
STEP = os.path.join(_HERE, "fixtures", "multisolid.step")

import bpy
from mathutils import Vector

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m
from CADder import refresh as refresh_mod

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def imported(**kwargs):
    """The mesh objects one import makes."""
    before = set(bpy.data.objects)
    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, **kwargs)
    return [o for o in bpy.data.objects
            if o not in before and o.type == "MESH"]


def low_corner(objs):
    pts = [o.matrix_world @ v.co for o in objs for v in o.data.vertices]
    return Vector([min(p[i] for p in pts) for i in range(3)])


# ---- a refresh puts the parts where the import did -----------------------
print("\n== the cursor of the import")
scene = bpy.context.scene
scene.cursor.location = (0.0, 0.0, 0.0)
first = imported(htypes="FLAT", up_as="Z")
at_first = low_corner(first)

scene.cursor.location = (5.0, 0.0, 0.0)
kwargs, _record, source = refresh_mod.import_settings(scene, STEP)
check(source is not None, "the import was recorded (%s)" % source)
again = imported(**kwargs)
shift = (low_corner(again) - at_first).length
check(shift < 1e-6, "a refresh puts the parts where the import did "
      "(moved %.4f)" % shift)

fresh = imported(htypes="FLAT", up_as="Z")
shift = (low_corner(fresh) - at_first - Vector((5.0, 0.0, 0.0))).length
check(shift < 1e-6, "a new import still lands at the cursor (off by %.4f)"
      % shift)

empty = bpy.data.objects.new("probe", None)
scene.collection.objects.link(empty)
m.transform_to_up("Z", [empty], 1.0, to_cursor=False)
check(empty.matrix_world.translation.length < 1e-9,
      "to_cursor=False leaves the cursor out (%s)"
      % (tuple(empty.matrix_world.translation),))

if FAILS:
    print("\nimport_repeat_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nimport_repeat_smoke: OK")
