# SPDX-License-Identifier: GPL-3.0-or-later
"""Does the selected material database stay selected when the folder changes?

    blender -b --factory-startup --python-exit-code 1 -P ci/matdb_choice_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

The database dropdown is an enum whose items come from the folder
listing. Blender stores the NUMBER of the selected item, not its name, and
the number was the position in the sorted listing. A teammate who added
brass.blend to a shared folder moved plastics from 2 to 3, and the stored
2 then read as metals: imports used the wrong database, and Save wrote over
it.

Each database now has a number made from its name. The test adds and
removes files around a selection and checks that the name stays. It also
checks that a position stored by an older version is read once, as that
version read it, and is then kept by name.
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


def touch(folder, name):
    open(os.path.join(folder, name + ".blend"), "wb").close()


def value_of(name):
    """The number Blender stores for this database in the dropdown."""
    for pos, item in enumerate(m._matdb_enum_items(None, bpy.context)):
        if item[0] == name:
            return item[3] if len(item) > 3 else pos
    return None


tmp = tempfile.mkdtemp(prefix="cadder_matdb_choice_")
try:
    prefs = m._get_addon_prefs()
    prefs.matdb_dir = tmp
    touch(tmp, "metals")
    touch(tmp, "plastics")

    prefs.active_matdb = "plastics"
    before = value_of("plastics")

    # 1. A teammate adds a database that sorts first.
    touch(tmp, "brass")
    check(prefs.active_matdb == "plastics",
          "plastics stays selected after brass is added (reads %r)"
          % prefs.active_matdb)
    check(value_of("plastics") == before,
          "the import dialog keeps the same number for plastics")
    check(m._get_active_matdb_path().endswith("plastics.blend"),
          "imports use plastics.blend")

    # 2. The selected file goes away: no other database takes its place.
    os.remove(os.path.join(tmp, "plastics.blend"))
    check(prefs.active_matdb not in ("metals", "brass"),
          "no other database takes the place of a removed one (reads %r)"
          % prefs.active_matdb)
    check(m._get_active_matdb_path() == "",
          "no database applies while plastics is missing")
    touch(tmp, "plastics")
    check(prefs.active_matdb == "plastics",
          "plastics is selected again when its file comes back")

    # 3. A position stored by an older version: [None, brass, metals,
    # plastics], so 2 was metals when that version saved it.
    stored = prefs.bl_system_properties_get(do_create=True)
    stored["active_matdb"] = 2
    migrate = getattr(m, "_migrate_active_matdb", None)
    check(migrate is not None, "the addon carries an older choice over")
    if migrate is not None:
        migrate()
    check(prefs.active_matdb == "metals",
          "the older choice reads as metals (reads %r)" % prefs.active_matdb)
    touch(tmp, "aluminium")
    check(prefs.active_matdb == "metals",
          "metals stays selected after aluminium is added (reads %r)"
          % prefs.active_matdb)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print("\nmatdb_choice_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nmatdb_choice_smoke: OK")
