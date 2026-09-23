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

  2. Only an Esc press cancels. The modal handler gets key releases too.
     When the user pressed Esc to cancel a move or to close a menu, that
     tool used the press, and the release then canceled the import.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import background as B

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


class _Event:
    def __init__(self, type_, value):
        self.type = type_
        self.value = value


class _Op:
    """Stands in for the running operator. It records what modal() does."""

    def __init__(self):
        self.calls = []

    def _kill(self):
        self.calls.append("kill")

    def _cleanup(self, context):
        self.calls.append("cleanup")

    def report(self, level, message):
        self.calls.append("report: %s" % message)


print("\n== Esc")
modal = B.STEPPER_OT_background_import.modal
for value, cancels in (("RELEASE", False), ("PRESS", True)):
    op = _Op()
    result = modal(op, bpy.context, _Event("ESC", value))
    if cancels:
        check(result == {"CANCELLED"} and "kill" in op.calls,
              "an Esc press cancels the import (%s, %s)" % (result, op.calls))
    else:
        check(result == {"PASS_THROUGH"} and not op.calls,
              "an Esc release does not cancel the import (%s, %s)"
              % (result, op.calls))

if FAILS:
    print("\nbackground_modal_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nbackground_modal_smoke: OK: each import is an undo step, and only "
      "an Esc press cancels")
