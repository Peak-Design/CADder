# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the material databases live, and what survives an upgrade.

    blender -b --factory-startup --python-exit-code 1 -P ci/matdb_store_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

Blender deletes the folder of an extension on every upgrade, and the update
notice sends the user straight into one. A database in the MaterialDB folder
inside the addon went with it. The default folder is now the one Blender
keeps for the extension's own files, and the databases an older version
left in the addon folder move there when the addon starts.

A test run loads the package as a legacy add-on, which has no such folder.
So the test stands in a temporary folder for it, and never lets the move
touch the MaterialDB folder of the checkout.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


TMP = tempfile.mkdtemp(prefix="cadder_matdb_store_")
PREFS = bpy.context.preferences.addons["CADder"].preferences
REAL_PATH_USER = bpy.utils.extension_path_user
USER_ROOT = os.path.join(TMP, "user")


def fake_path_user(package, path="", create=False):
    """The folder an extension install would get, under the temp folder."""
    d = os.path.join(USER_ROOT, path)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def as_extension(fn):
    """Run `fn` as if the addon were an extension install."""
    bpy.utils.extension_path_user = fake_path_user
    try:
        return fn()
    finally:
        bpy.utils.extension_path_user = REAL_PATH_USER


def norm(p):
    return os.path.normcase(os.path.normpath(p or ""))


# ---- the default folder survives an upgrade ------------------------------
print("\n== the default folder is outside the addon")
PREFS.matdb_dir = ""
got = as_extension(m._get_matdb_dir)
want = os.path.join(USER_ROOT, "MaterialDB")
check(norm(got) == norm(want),
      "an extension keeps its databases in its user folder (%s)" % got)
check(os.path.isdir(want), "and the folder is made")
# A legacy install has no user folder, so the addon folder is all it has.
got = m._get_matdb_dir()
check(norm(got) == norm(os.path.join(m._ADDON_DIR, "MaterialDB")),
      "a legacy install keeps the addon folder (%s)" % got)
# A folder set in preferences still wins.
custom = os.path.join(TMP, "shared")
PREFS.matdb_dir = custom
got = as_extension(m._get_matdb_dir)
check(norm(got) == norm(custom), "a folder set in preferences wins (%s)" % got)
PREFS.matdb_dir = ""

# ---- the old databases move out of the addon folder ----------------------
print("\n== databases in the addon folder move to the user folder")
old = os.path.join(TMP, "addon", "MaterialDB")
new = os.path.join(TMP, "moved", "MaterialDB")
os.makedirs(old)
os.makedirs(new)
for name, body in (("metal.blend", b"metal"), ("wood.BLEND", b"wood"),
                   ("same.blend", b"old copy"), ("notes.txt", b"notes")):
    with open(os.path.join(old, name), "wb") as fh:
        fh.write(body)
# A database of that name is already in the user folder. It is newer than
# anything an older version left behind, so it stays as it is.
with open(os.path.join(new, "same.blend"), "wb") as fh:
    fh.write(b"new copy")

moved = m._move_addon_matdbs(old, new)
check(moved == 2, "two databases moved (%r)" % moved)
check(sorted(os.listdir(new)) == ["metal.blend", "same.blend", "wood.BLEND"],
      "the user folder holds them (%s)" % sorted(os.listdir(new)))
with open(os.path.join(new, "metal.blend"), "rb") as fh:
    check(fh.read() == b"metal", "a moved database keeps its content")
with open(os.path.join(new, "same.blend"), "rb") as fh:
    check(fh.read() == b"new copy",
          "a database already in the user folder is not overwritten")
check(sorted(os.listdir(old)) == ["notes.txt", "same.blend"],
      "what could not move stays where it was (%s)" % sorted(os.listdir(old)))
check(m._move_addon_matdbs(old, new) == 0, "a second pass moves nothing")
check(m._move_addon_matdbs(new, new) == 0, "a folder never moves into itself")
check(m._move_addon_matdbs(os.path.join(TMP, "none"), new) == 0,
      "a missing addon folder is not an error")

# The move runs when the addon starts. A spy stands in for it here, so the
# real one never runs on the checkout's own folder.
calls = []
real_move = m._move_addon_matdbs
m._move_addon_matdbs = lambda *a, **k: calls.append(a) or 0
try:
    bpy.ops.preferences.addon_disable(module="CADder")
    bpy.ops.preferences.addon_enable(module="CADder")
finally:
    m._move_addon_matdbs = real_move
check(len(calls) == 1, "register moves the old databases (%d call(s))"
      % len(calls))

PREFS = bpy.context.preferences.addons["CADder"].preferences

# ---- the active database stays the same database -------------------------
# Blender stores the number of an enum item, not its name. Numbered by
# position in the folder, the selection moved to another database when a
# teammate added one to a shared folder, and Save then wrote over it.
print("\n== the selection follows the name, not the place in the folder")
shared = os.path.join(TMP, "team")
os.makedirs(shared)


def put(name):
    with open(os.path.join(shared, name + ".blend"), "wb") as fh:
        fh.write(b"db")


put("metal")
put("wood")
PREFS.matdb_dir = shared
PREFS.active_matdb = "wood"
check(PREFS.active_matdb == "wood", "wood is active (%r)" % PREFS.active_matdb)
put("aluminium")
check(PREFS.active_matdb == "wood",
      "a database added in front keeps wood active (%r)" % PREFS.active_matdb)
os.remove(os.path.join(shared, "aluminium.blend"))
os.remove(os.path.join(shared, "metal.blend"))
check(PREFS.active_matdb == "wood",
      "databases taken away keep wood active (%r)" % PREFS.active_matdb)
check(norm(m._get_active_matdb_path()) == norm(os.path.join(shared, "wood.blend")),
      "and the import uses wood.blend")
PREFS.active_matdb = "NONE"
put("metal")
check(PREFS.active_matdb == "NONE", "None stays None (%r)" % PREFS.active_matdb)
PREFS.matdb_dir = ""

# ---- a relative texture path still points at the texture -----------------
# Blender gives a new image a path relative to the blend file. The database
# is a file in another folder, and Blender reads a relative path in it from
# that folder, so the path has to be rebased when the database is written.
print("\n== a texture keeps its file through the database")
proj_a = os.path.join(TMP, "projA")
os.makedirs(os.path.join(proj_a, "tex"))
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(proj_a, "a.blend"))
texture = os.path.join(proj_a, "tex", "t.png")
img = bpy.data.images.new("t", 4, 4)
img.filepath_raw = texture
img.file_format = "PNG"
img.save()
img.filepath = "//tex/t.png"
mat = bpy.data.materials.new("Textured")
node = mat.node_tree.nodes.new("ShaderNodeTexImage")
node.image = img
db_path = os.path.join(TMP, "db", "db.blend")
os.makedirs(os.path.dirname(db_path))
m._write_material_database(db_path, {"STEP_part": "Textured"})
check(img.filepath == "//tex/t.png",
      "the image in the open file keeps its path (%r)" % img.filepath)
bpy.data.materials.remove(mat)
bpy.data.images.remove(img)

proj_c = os.path.join(TMP, "projC")
os.makedirs(proj_c)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(proj_c, "c.blend"))
m._append_matdb_materials(db_path)
got = bpy.data.images.get("t")
where = bpy.path.abspath(got.filepath) if got else ""
check(got is not None and os.path.isfile(where),
      "the appended image finds its file (%r reads as %r)"
      % (got.filepath if got else None, where))

# ---- a damaged database does not stop the import --------------------------
# The database was read after every object was built and linked, but before
# the parts were scaled and turned. A file that Blender could not read (a
# copy to a shared folder that stopped half way) raised out of the import
# and left the parts in file units, on the wrong up axis, with no record.
print("\n== a damaged database file")
STEP = os.path.join(_HERE, "fixtures", "multisolid.step")
broken = os.path.join(TMP, "broken")
os.makedirs(broken)
with open(os.path.join(broken, "half.blend"), "wb") as fh:
    fh.write(b"BLENDER-v5")
PREFS.matdb_dir = broken
m._cache_drop(STEP)
try:
    result = m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z",
                         material_database="half")
except Exception as exc:
    result = exc
check(isinstance(result, tuple), "the import finishes (%r)" % (result,))
from CADder import refresh as refresh_mod
check(refresh_mod.settings_for(bpy.context.scene, STEP) is not None,
      "and records itself for a refresh")
PREFS.matdb_dir = ""

# ---- a failed write leaves the open file as it was -----------------------
# A linked material is written through a local copy that takes the linked
# material's name. The copy and the text block were removed only after a
# write that worked, so a database folder that was read-only or gone left
# them in the user's file.
print("\n== a write that fails cleans up")
lib_path = os.path.join(TMP, "lib", "lib.blend")
os.makedirs(os.path.dirname(lib_path))
lib_mat = bpy.data.materials.new("LibMat")
bpy.data.libraries.write(lib_path, {lib_mat}, fake_user=True)
bpy.data.materials.remove(lib_mat)
with bpy.data.libraries.load(lib_path, link=True) as (src, dst):
    dst.materials = ["LibMat"]
linked = [mt for mt in bpy.data.materials if mt.name == "LibMat"]
check(len(linked) == 1 and linked[0].library is not None,
      "LibMat is linked from the library")
nowhere = os.path.join(TMP, "gone", "deeper", "db.blend")
try:
    m._write_material_database(nowhere, {"STEP_part": "LibMat"})
    raised = False
except Exception:
    raised = True
check(raised, "the write fails")
left = [mt.name for mt in bpy.data.materials
        if mt.name == "LibMat" and mt.library is None]
check(not left, "no local copy of LibMat is left behind (%s)" % left)
check(bpy.data.texts.get(m._MATDB_TEXT_NAME) is None,
      "no mapping text block is left behind")

if FAILS:
    print("\nmatdb_store_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nmatdb_store_smoke: OK")
