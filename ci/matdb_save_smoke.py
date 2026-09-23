# SPDX-License-Identifier: GPL-3.0-or-later
"""Does Save write the mapping list only to the database it came from?

    blender -b --factory-startup --python-exit-code 1 -P ci/matdb_save_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

The mapping list is kept in the scene, and so in the .blend. The database
that Save writes is the one selected in the preferences. A user who loaded
'metals', then selected 'plastics' and pressed Save, replaced plastics.blend
with the metals list. A .blend whose list came from another database did
the same.

Save must now refuse a list that did not come from the selected database,
and must still work for a list that did (Load, New and Duplicate).
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def save():
    """Press Save. False when the button is not available."""
    if not bpy.ops.stepper.mat_db_save.poll():
        return False
    return bpy.ops.stepper.mat_db_save() == {"FINISHED"}


tmp = tempfile.mkdtemp(prefix="cadder_matdb_save_")
try:
    prefs = m._get_addon_prefs()
    prefs.matdb_dir = tmp
    stepper = bpy.context.scene.stepper

    bpy.data.materials.new("Brass")
    bpy.data.materials.new("ABS")
    metals = os.path.join(tmp, "metals.blend")
    plastics = os.path.join(tmp, "plastics.blend")
    m._write_material_database(metals, {"steel": "Brass"})
    m._write_material_database(plastics, {"pla": "ABS"})

    # 1. Load metals, select plastics, press Save.
    prefs.active_matdb = "metals"
    bpy.ops.stepper.mat_db_refresh()
    check([i.original_name for i in stepper.mat_db_mappings] == ["steel"],
          "Load filled the list from metals")
    prefs.active_matdb = "plastics"
    check(not save(), "Save refuses the metals list while plastics is selected")
    check(m._read_matdb_mappings(plastics) == {"pla": "ABS"},
          "plastics.blend still holds its own list (%s)"
          % m._read_matdb_mappings(plastics))

    # 2. A list from the selected database still saves.
    bpy.ops.stepper.mat_db_refresh()
    stepper.mat_db_mappings[0].replacement_name = "Brass"
    check(save(), "Save writes the plastics list to plastics")
    check(m._read_matdb_mappings(plastics) == {"pla": "Brass"},
          "the change reached plastics.blend (%s)"
          % m._read_matdb_mappings(plastics))
    check(m._read_matdb_mappings(metals) == {"steel": "Brass"},
          "metals.blend did not change")

    # 3. A list whose source is not known (a .blend saved by an older
    # version) is not saved over whatever database is selected.
    if hasattr(stepper, "mat_db_source"):
        stepper.mat_db_source = ""
    check(not save(), "Save refuses a list from an unknown database")

    # 4. New and Duplicate make the list of the database they make.
    obj = bpy.data.objects.new("part", bpy.data.meshes.new("part"))
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(bpy.data.materials["Brass"])
    obj["STEP_materials"] = '["bronze"]'
    bpy.ops.stepper.mat_db_create(db_name="alloys")
    check(prefs.active_matdb == "alloys", "New selected the new database")
    check(save(), "Save works on the list that New made")
    bpy.ops.stepper.mat_db_duplicate(db_name="alloys_copy")
    check(prefs.active_matdb == "alloys_copy",
          "Duplicate selected the copy")
    check(save(), "Save works on the list that Duplicate loaded")

    # 5. Delete clears the list, so nothing is left to save anywhere.
    prefs.active_matdb = "metals"
    bpy.ops.stepper.mat_db_refresh()
    bpy.ops.stepper.mat_db_delete()
    prefs.active_matdb = "plastics"
    check(not save(), "nothing to save after Delete")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print("\nmatdb_save_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nmatdb_save_smoke: OK")
