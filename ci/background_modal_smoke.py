# SPDX-License-Identifier: GPL-3.0-or-later
"""The modal part of a background import, as far as a headless Blender
can run it.

    blender -b --factory-startup --python-exit-code 1 -P ci/background_modal_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

A headless Blender has no window, so the modal loop and the undo stack
cannot run here. What can be checked is checked:

  1. Each import is one undo step. An operator without the UNDO option
     pushes no undo step when it finishes, so Ctrl+Z after an import went
     back past it, and the step before it was lost too.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import background as B  # noqa: F401

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


print("\n== undo")
for name, op in (("background import", bpy.ops.stepper.background_import),
                 ("import", bpy.ops.import_scene.occ_import_step)):
    check("UNDO" in op.bl_options,
          "the %s is an undo step (%s)" % (name, sorted(op.bl_options)))

if FAILS:
    print("\nbackground_modal_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nbackground_modal_smoke: OK - each import is an undo step")
