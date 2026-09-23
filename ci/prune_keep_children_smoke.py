# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Prune Hierarchy next to objects of the user.

    blender -b --factory-startup --python-exit-code 1 -P ci/prune_keep_children_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

Prune removes an empty of the import that holds one part. A user can
parent a light, a camera or a helper to that empty. Such an empty is not a
level with one child, and removing it would drop those objects from their
parent and move them.
"""

import os
import sys

import bpy
from mathutils import Vector

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


def load():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m = sys.modules["CADder.main"]
    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, htypes="EMPTIES", up_as="Z")
    bpy.context.view_layer.update()
    part = next(o for o in bpy.data.objects
                if o.type == "MESH" and "STEP_tag" in o)
    return part


def prune(part):
    for o in bpy.context.view_layer.objects:
        o.select_set(o == part)
    bpy.context.view_layer.objects.active = part
    return bpy.ops.stepper.prune_hierarchy()


# ---- an empty with only the part goes ------------------------------------
print("\n== an empty that holds one part is pruned")
part = load()
holder = part.parent
check(holder is not None and holder.type == "EMPTY",
      "the part sits under an empty of the import")
holder_name = holder.name if holder is not None else ""
where = part.matrix_world.translation.copy()
prune(part)
check(holder_name not in bpy.data.objects, "the empty is gone")
check((part.matrix_world.translation - where).length < 1e-6,
      "and the part did not move")

# ---- an empty that also holds an object of the user stays ----------------
print("\n== an empty that also holds an object of the user stays")
part = load()
holder = part.parent
holder.location = (1.0, 2.0, 3.0)
bpy.context.view_layer.update()
light = bpy.data.objects.new("user light", bpy.data.lights.new("user light",
                                                               "POINT"))
bpy.context.scene.collection.objects.link(light)
light.parent = holder
light.location = (0.5, 0.0, 0.0)
bpy.context.view_layer.update()
light_at = light.matrix_world.translation.copy()
check((light_at - Vector((1.5, 2.0, 3.0))).length < 1e-6,
      "the light starts at (1.5, 2, 3)")
holder_name = holder.name
prune(part)
bpy.context.view_layer.update()
kept = bpy.data.objects.get(holder_name)
check(kept is not None, "the empty is kept")
check(kept is not None and light.parent == kept,
      "the light keeps its parent")
check((light.matrix_world.translation - light_at).length < 1e-6,
      "and the light did not move (%s)"
      % (tuple(round(v, 4) for v in light.matrix_world.translation),))
check(kept is not None and part.parent == kept,
      "the part keeps its parent too")

if FAILS:
    print("\nprune_keep_children_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nprune_keep_children_smoke: OK: prune leaves the objects of the "
      "user where they are")
