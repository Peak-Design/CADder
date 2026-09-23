# SPDX-License-Identifier: GPL-3.0-or-later
"""An import preset must keep the Quality it was saved with.

    blender -b --factory-startup --python-exit-code 1 -P ci/import_preset_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Blender writes every operator property into a preset file unless the
property has SKIP_PRESET. The old lin_deflection and ang_deflection
properties had no such flag, so each preset held them. When the preset was
applied, they were set, and a set legacy property puts the import in legacy
mode: 0.8 and 0.5 in file units, whatever Quality said.

The preset file lines come from Blender's own preset operator, and the
preset is then applied the way Blender applies it: each line sets a
property. The import that follows must use the Quality of the preset.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import import_ui

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    w = STEPControl_Writer()
    w.Transfer(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(),
               STEPControl_AsIs)
    w.Write(path)


class _PresetOp:
    operator = "IMPORT_SCENE_OT_occ_import_step"


from bl_operators.presets import AddPresetOperator  # noqa: E402

lines = AddPresetOperator.preset_values.fget(_PresetOp())
written = {line.split(".", 1)[1] for line in lines}
print("\n== what a preset file holds")
print("   ", sorted(written))
check("quality_preset" in written, "a preset keeps the Quality")
check("lin_deflection" not in written and "ang_deflection" not in written,
      "a preset does not hold the legacy deflection values")

print("\n== an import with the preset applied")
tmp = tempfile.mkdtemp(prefix="cadder_preset_")
step = os.path.join(tmp, "preset_box.step")
write_step(step)

rna = bpy.ops.import_scene.occ_import_step.get_rna_type()
applied = {"quality_preset": "FINE"}
for name in ("lin_deflection", "ang_deflection"):
    if name in written:
        # The preset file holds the value the operator had: the default.
        applied[name] = rna.properties[name].default

specs = []
real = import_ui.make_deflection_spec


def spy(op, prefs):
    spec = real(op, prefs)
    specs.append(spec)
    return spec


import_ui.make_deflection_spec = spy
try:
    bpy.ops.import_scene.occ_import_step(
        filepath=step, override_file=os.path.basename(step), **applied)
finally:
    import_ui.make_deflection_spec = real

check(len(specs) == 1, "the import ran once (%d)" % len(specs))
if specs:
    spec = specs[0]
    print("    spec:", spec)
    check(spec.get("mode") != "legacy",
          "the import does not fall back to legacy deflection")
    check(spec.get("quality") == "FINE",
          "the import uses the Quality of the preset")

print("\n== a script that names the legacy values still gets them")
specs.clear()
import_ui.make_deflection_spec = spy
try:
    bpy.ops.import_scene.occ_import_step(
        filepath=step, override_file=os.path.basename(step),
        lin_deflection=0.3, ang_deflection=0.2)
finally:
    import_ui.make_deflection_spec = real
legacy = specs[0] if specs else {}
check(legacy.get("mode") == "legacy"
      and abs(legacy.get("lin", 0.0) - 0.3) < 1e-6
      and abs(legacy.get("ang", 0.0) - 0.2) < 1e-6,
      "explicit legacy values select legacy mode (%s)" % legacy)

if FAILS:
    print("\nimport_preset_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nimport_preset_smoke: OK: a preset keeps its Quality")
