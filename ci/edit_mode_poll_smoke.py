# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the mesh tools in edit mode.

    blender -b --factory-startup --python-exit-code 1 -P ci/edit_mode_poll_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

Clean Up Selected Meshes and Box Project UVs work on the mesh data. In edit
mode that data is empty until the mode ends, so the tools failed with a
traceback, and Clean Up left a helper attribute on the mesh. They must
refuse edit mode, as Regenerate and Apply UVs do.
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
m = sys.modules["CADder.main"]
m._cache_drop(STEP)
m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z")
part = next(o for o in bpy.data.objects
            if o.type == "MESH" and "STEP_tag" in o)
for o in bpy.context.view_layer.objects:
    o.select_set(o == part)
bpy.context.view_layer.objects.active = part

print("\n== object mode")
check(bpy.ops.stepper.mesh_cleanup.poll(), "Clean Up is available")
check(bpy.ops.stepper.add_box_uv.poll(), "Box Project is available")

print("\n== edit mode")
bpy.ops.object.mode_set(mode="EDIT")
check(not bpy.ops.stepper.mesh_cleanup.poll(), "Clean Up refuses edit mode")
check(not bpy.ops.stepper.add_box_uv.poll(), "Box Project refuses edit mode")
for op in (bpy.ops.stepper.mesh_cleanup, bpy.ops.stepper.add_box_uv):
    try:
        op()
    except RuntimeError as exc:
        check("Traceback" not in str(exc),
              "%s stops without a traceback" % op.idname_py())
bpy.ops.object.mode_set(mode="OBJECT")
check(part.data.attributes.get("stepper_normal") is None,
      "the mesh carries no helper attribute after edit mode")

if FAILS:
    print("\nedit_mode_poll_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nedit_mode_poll_smoke: OK: the mesh tools wait for object mode")
