# SPDX-License-Identifier: GPL-3.0-or-later
"""Do the import options really come back in the next session?

    blender -b --factory-startup --python-exit-code 1 -P ci/import_settings_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Two things are checked, and the first is the one that rots
--------------------------------------------------------
1. Every option the import dialog shows is in PERSISTED_PROPS. A new option
   added to the dialog and forgotten here is the way this breaks, and it
   breaks quietly.
2. The values survive a round trip: save them, throw the operator away,
   seed a new one from preferences, and read them back.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import import_ui, main as m

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


# Blender's own file-browser plumbing, and options that are not the user's.
NOT_SETTINGS = {
    "filepath", "directory", "files", "filter_glob", "filename",
    "rna_type", "check_existing",
    # The material database list changes between sessions, so its remembered
    # value could name a file that is gone.
    "material_database",
    # Written by a caller, never by the dialog.
    "override_file",
    # Superseded by the quality preset. Remembering them would make the
    # legacy path in make_deflection_spec look deliberately chosen.
    "lin_deflection", "ang_deflection",
    # Declared but never drawn and never read. See main.py.
    "fw_as",
}

print("\n== every option the dialog offers is on the list")
rna = bpy.ops.import_scene.occ_import_step.get_rna_type()
props = [p.identifier for p in rna.properties
         if not p.is_readonly and p.identifier not in NOT_SETTINGS]
missing = sorted(p for p in props if p not in import_ui.PERSISTED_PROPS)
check(not missing,
      "no option is left out of PERSISTED_PROPS (%d checked, missing %s)"
      % (len(props), missing or "none"))
stale = sorted(p for p in import_ui.PERSISTED_PROPS if p not in props)
check(not stale,
      "the list names no option that no longer exists (%s)"
      % (stale or "none"))

print("\n== the defaults the addon ships with")
# These are a deliberate choice, not whatever each property happened to be
# given. A change here should be a decision, so it has to break this test
# first.
SHIPPED = {
    "uv_mode": "SURFACE",
    "uv_normalize": False,
    "uv_closed_seams": "SINGLE",
    "uv_pack": "NONE",
    "tris_to_quads": True,
}
by_id = {p.identifier: p for p in rna.properties}
wrong_default = []
for key, value in SHIPPED.items():
    p = by_id.get(key)
    got = getattr(p, "default", None) if p is not None else None
    if got != value:
        wrong_default.append("%s=%r wanted %r" % (key, got, value))
check(not wrong_default,
      "the shipped defaults are unchanged (%s)"
      % ("; ".join(wrong_default) if wrong_default
         else ", ".join("%s=%r" % (k, v) for k, v in sorted(SHIPPED.items()))))

print("\n== the values survive a round trip")
prefs = m._get_addon_prefs()
prefs.remember_import_settings = True
prefs.last_import_settings = ""


class FakeOp:
    """Stands in for the operator: the round trip only touches attributes."""
    bl_idname = "import_scene.occ_import_step_test"


# Something clearly not the default for each option.
WANT = {
    "up_as": "Y",
    "hierarchy_types": "EMPTIES",
    "custom_scale": True,
    "user_scale": 0.25,
    "apply_scale": False,
    "tessellation_relative": True,
    "quality_preset": "CUSTOM",
    "lin_deflection_len": 0.123,
    "ang_deflection_rot": 0.456,
    "lin_deflection_rel": 0.0123,
    "detail_level": 42,
    "eng_materials": False,
    "uv_mode": "UNWRAP",
    "uv_normalize": True,
    "uv_closed_seams": "SPLIT",
    "box_uv_scale": 2.5,
    "uv_unwrap_method": "ANGLE_BASED",
    "uv_pack": "UDIM",
    "uv_pack_tiles": 7,
    "uv_pack_margin": 0.0123,
    "tris_to_quads": True,
    "skip_construction": True,
    "import_curves": True,
    "group_in_collection": True,
    "separate_solids": True,
}

covered = sorted(set(import_ui.PERSISTED_PROPS) - set(WANT))
check(not covered,
      "this test sets a value for every persisted option (%s)"
      % (covered or "none"))

source = FakeOp()
for key, value in WANT.items():
    setattr(source, key, value)
import_ui.save_last_used(source, prefs)
check(bool(prefs.last_import_settings),
      "the settings were written to preferences (%d chars)"
      % len(prefs.last_import_settings))


def round_trip():
    """Save, throw the operator away, and seed a new one as a new session."""
    import_ui._session_seeded.clear()
    restored = FakeOp()
    restored.bl_idname = "import_scene.occ_import_step"
    for key in WANT:
        setattr(restored, key, None)
    import_ui.seed_from_prefs(restored, prefs)
    return restored


def compare(restored, skip=()):
    wrong = []
    for key, value in WANT.items():
        if key in skip:
            continue
        got = getattr(restored, key, None)
        if isinstance(value, float):
            if got is None or abs(float(got) - value) > 1e-6:
                wrong.append("%s=%r wanted %r" % (key, got, value))
        elif got != value:
            wrong.append("%s=%r wanted %r" % (key, got, value))
    return wrong


# With the full parameter set on screen, everything must come back.
prefs.simpler_parameters = False
wrong = compare(round_trip())
check(not wrong,
      "every option came back with the value it was saved with (%s)"
      % ("; ".join(wrong) if wrong else "all %d" % len(WANT)))

# With the simple detail slider on screen, quality_preset is deliberately
# left alone. Assigning it marks it as chosen for the whole session, which
# would override the slider the user is actually looking at. Everything
# else still has to come back.
prefs.simpler_parameters = True
restored = round_trip()
wrong = compare(restored, skip=("quality_preset",))
check(not wrong,
      "and again with the simple detail slider on screen (%s)"
      % ("; ".join(wrong) if wrong else "all but quality_preset"))
check(getattr(restored, "quality_preset", None) is None,
      "quality_preset stays untouched in simple mode, so the detail slider "
      "still wins")
prefs.simpler_parameters = False

print("\n== turning the option off stops the saving")
prefs.remember_import_settings = False
prefs.last_import_settings = ""
import_ui.save_last_used(source, prefs)
check(prefs.last_import_settings == "",
      "nothing is written when Remember import settings is off")

if FAILS:
    print("\nimport_settings_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nimport_settings_smoke: OK - every dialog option is saved and comes "
      "back")
