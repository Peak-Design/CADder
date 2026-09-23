# SPDX-License-Identifier: GPL-3.0-or-later
"""Is an import one step in the undo history?

    blender -b --factory-startup --python-exit-code 1 -P ci/import_undo_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Blender pushes an undo step after an operator only when the operator has
the UNDO option. Without it the import joined the NEXT step: a user who
imported a file, moved one part and pressed Ctrl+Z to put the part back
lost the whole import with it.

The test does what the file browser does when it runs the import (an
operator call that may push an undo step), makes one edit of its own, and
undoes that edit. The import must stay.

The background import cannot run here (it needs a window for its timer),
so only its option is checked. It is the operator that adds the parts on
that route, so it is the one that must push the step.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m  # noqa: E402,F401

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


STEP = os.path.join(_HERE, "fixtures", "assembly.step")

bpy.ops.ed.undo_push(message="before the import")
before = {o.name for o in bpy.data.objects}

# 'EXEC_DEFAULT', True: run it as the file browser does, with an undo push
# when the operator asks for one.
result = bpy.ops.import_scene.occ_import_step(
    "EXEC_DEFAULT", True, filepath=STEP)
check(result == {"FINISHED"}, "the import finished (%s)" % result)
imported = {o.name for o in bpy.data.objects} - before
check(len(imported) > 0, "the import made %d object(s)" % len(imported))

edit = bpy.data.objects.new("user edit", None)
bpy.context.scene.collection.objects.link(edit)
bpy.ops.ed.undo_push(message="the user's edit")

bpy.ops.ed.undo()
now = {o.name for o in bpy.data.objects}
check("user edit" not in now, "Ctrl+Z took back the user's edit")
check(imported <= now,
      "Ctrl+Z kept the import (%d of %d objects left)"
      % (len(imported & now), len(imported)))

bg_options = getattr(bpy.types.STEPPER_OT_background_import,
                     "bl_options", set())
check("UNDO" in bg_options,
      "the background import pushes its own undo step (%s)"
      % sorted(bg_options))

if FAILS:
    print("\nimport_undo_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nimport_undo_smoke: OK")
