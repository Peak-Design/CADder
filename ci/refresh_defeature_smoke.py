# SPDX-License-Identifier: GPL-3.0-or-later
"""Refresh from disk keeps a part defeatured when its switch is on.

    blender -b --factory-startup --python-exit-code 1 -P ci/refresh_defeature_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Defeature for a STEP part is done on the shape, in Regenerate. A refresh
reads the file again through the importer, which knows nothing about the
switch, so the bolt holes came back while the switch and the panel still
said the part was defeatured. The live link does not have this problem: it
sends the switch with every request. A refresh has to keep the same
decision.

The fixture is fixtures/holes.step, copied to a temp folder: a plate with
four 6 mm bolt holes and one 30 mm bore.
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, refresh as R

FAILS = []

# Where the holes are in the fixture and how wide, in meters. The same
# table as defeature_step_smoke.py.
SMALL = [((0.012, 0.012), 0.006), ((0.088, 0.012), 0.006),
         ((0.012, 0.048), 0.006), ((0.088, 0.048), 0.006)]
BIG = ((0.050, 0.030), 0.030)


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def has_hole(obj, where, diameter):
    (x, y), radius = where, diameter * 0.5
    matrix = obj.matrix_world
    for vertex in obj.data.vertices:
        point = matrix @ vertex.co
        if (point.x - x) ** 2 + (point.y - y) ** 2 <= (radius * 1.2) ** 2:
            return True
    return False


def holes(obj):
    """(small holes left, the big bore is left). The view layer is updated
    first, so no stale matrix can hide a hole."""
    bpy.context.view_layer.update()
    return (sum(1 for where, size in SMALL if has_hole(obj, where, size)),
            has_hole(obj, BIG[0], BIG[1]))


tmp = tempfile.mkdtemp(prefix="stepper_refresh_defeature_")
STEP = os.path.join(tmp, "holes.step")
shutil.copyfile(os.path.join(_HERE, "fixtures", "holes.step"), STEP)


def load():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z")
    parts = [o for o in R.file_objects(STEP)
             if o.type == "MESH" and "STEP_tag" in o]
    assert parts, "the fixture did not import"
    return parts[0]


def only_select(obj):
    for other in bpy.context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


print("\n== the part's own switch")
plate = load()
check(holes(plate) == (4, True), "the fixture has its holes (%s)"
      % (holes(plate),))
plain_faces = len(plate.data.polygons)
plate.cad_defeature.enabled = True
plate.cad_defeature.size = 0.012
only_select(plate)
bpy.ops.stepper.apply_defeature()
lighter = len(plate.data.polygons)
check(holes(plate) == (0, True) and lighter < plain_faces,
      "Apply Defeature took the bolt holes (%s, %d faces)"
      % (holes(plate), lighter))

# The user's own work on the defeatured part. A refresh without defeature
# keeps all of it, so one with defeature must too.
mine = bpy.data.materials.new("My Material")
plate.data.materials[0] = mine
group = plate.vertex_groups.new(name="My Group")
group.add([0, 1, 2], 0.5, "REPLACE")
weighted = sorted(v.index for v in plate.data.vertices
                  for g in v.groups if g.group == group.index)

name = plate.name
plate.select_set(False)
result = bpy.ops.stepper.refresh_file(filepath=STEP)
check(result == {"FINISHED"}, "the refresh finished (%s)" % result)
same = bpy.data.objects.get(name)
check(same is plate, "the object is the same one")
check(same.cad_defeature.enabled, "the switch is still on")
check(holes(same) == (0, True),
      "the bolt holes stay out after the refresh (%s)" % (holes(same),))
check(len(same.data.polygons) == lighter,
      "the same light mesh (%d faces, was %d)"
      % (len(same.data.polygons), lighter))
check(not same.select_get() and not bpy.context.selected_objects,
      "the refresh left the selection alone (%s)"
      % [o.name for o in bpy.context.selected_objects])
check([ms.name if ms else None for ms in same.data.materials][:1]
      == ["My Material"], "the user's material stays (%s)"
      % [ms.name if ms else None for ms in same.data.materials])
kept_group = same.vertex_groups.get("My Group")
check(kept_group is not None, "the user's vertex group stays")
if kept_group is not None:
    now = sorted(v.index for v in same.data.vertices
                 for g in v.groups if g.group == kept_group.index)
    check(now == weighted, "with its weights, as the part did not change "
          "(%s, was %s)" % (now, weighted))

print("\n== a collection's switch")
plate = load()
group = [c for c in plate.users_collection
         if c.get("STEP_file") == STEP][0]
group.cad_defeature.enabled = True
group.cad_defeature.size = 0.012
only_select(plate)
bpy.ops.stepper.apply_defeature()
check(holes(plate) == (0, True), "Apply Defeature took the bolt holes")
bpy.ops.stepper.refresh_file(filepath=STEP)
check(holes(plate) == (0, True),
      "the bolt holes stay out after the refresh (%s)" % (holes(plate),))

print("\n== a part nobody marked")
plate = load()
bpy.ops.stepper.refresh_file(filepath=STEP)
check(holes(plate) == (4, True) and len(plate.data.polygons) == plain_faces,
      "comes back with every hole (%s, %d faces)"
      % (holes(plate), len(plate.data.polygons)))
check(not plate.cad_defeature.enabled, "and its switch stays off")

if FAILS:
    print("\nrefresh_defeature_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nrefresh_defeature_smoke: OK: a refresh keeps the defeature switch "
      "and what it asks for")
