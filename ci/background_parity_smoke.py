# SPDX-License-Identifier: GPL-3.0-or-later
"""A background import must give the same result as a direct import.

    blender -b --factory-startup --python-exit-code 1 -P ci/background_parity_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

The worker is a separate Blender that starts from factory settings. Each
setting that changes the result must travel to it, and each thing that the
append changes must be put back. This smoke runs the worker for real and
appends its blend the way the modal operator does.

  1. A material database in a custom Material Database Folder. The worker
     did not get the folder, so it did not know the database, and the
     operator call failed on the database name.

  2. Collection instances. The import hides the prototype collection in the
     view layer, and a view layer flag does not come across with the
     collection. Every prototype then showed at the origin.

  3. Materials that the scene has already. A direct import uses the
     material of the same name. The append made a ".001" copy of each one,
     so a change to the material reached only the first import.

  4. The 3D cursor. A direct import puts the parts at the cursor. The
     worker's cursor was at the origin, so a large file landed there.
"""
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, background as B, refresh as R

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for i in range(2):
        shape = BRepPrimAPI_MakeBox(gp_Pnt(i * 30.0, 0, 0), 10, 10, 10).Shape()
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString("body%d" % i))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def prefs():
    return bpy.context.preferences.addons["CADder"].preferences


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)


def step_objects():
    return sorted((o for o in bpy.data.objects
                   if o.get("STEP_file") == STEP and o.type == "MESH"),
                  key=lambda o: o.name)


def run_worker(op_kwargs, name, filepath=None):
    """Run the worker as the modal operator does. Return the blend path, or
    None when the worker failed."""
    out_blend = os.path.join(tmp, name + ".blend")
    request = B.build_request(bpy.context, filepath or STEP, out_blend,
                              op_kwargs, B.prefs_snapshot(prefs()))
    req_path = os.path.join(tmp, name + ".json")
    with open(req_path, "w", encoding="utf-8") as f:
        json.dump(request, f)
    worker = os.path.join(_ADDON, "worker.py")
    proc = subprocess.run(
        [bpy.app.binary_path, "-b", "--factory-startup",
         "--python-exit-code", "1", "--python", worker, "--", req_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0 or not os.path.isfile(out_blend):
        print(proc.stdout.decode("utf-8", "replace")[-3000:])
        return None
    return out_blend


class _Fake:
    pass


def append(out_blend):
    B.STEPPER_OT_background_import._append_result(
        _Fake(), bpy.context, out_blend)


tmp = tempfile.mkdtemp(prefix="cadder_bgparity_")
STEP = os.path.join(tmp, "bg_parity.step")
write_step(STEP)

# -- 1. a material database in a custom folder --------------------------------
# The database goes in a temp folder. The MaterialDB folder of a worktree is
# a link to the real one, so a test must never write there.
print("\n== material database in a custom folder")
clean()
db_dir = os.path.join(tmp, "custom MaterialDB")
os.makedirs(db_dir)
prefs().matdb_dir = db_dir

m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z", custom_scale=1.0)
originals = sorted({n for o in step_objects()
                    for n in json.loads(o.get("STEP_materials", "[]"))})
check(bool(originals), "a direct import names its materials (%s)" % originals)
authored = bpy.data.materials.new("Workshop authored")
db_path = os.path.join(db_dir, "workshop.blend")
m._write_material_database(db_path, {n: authored.name for n in originals})
check(os.path.isfile(db_path), "the database is in the custom folder")

clean()
prefs().matdb_dir = db_dir
prefs().active_matdb = "workshop"
check(prefs().active_matdb == "workshop",
      "the parent session sees the database in the custom folder")
out = run_worker({"hierarchy_types": "TREE", "up_as": "ZPOS",
                  "custom_scale": True, "user_scale": 1.0,
                  "material_database": "workshop"}, "matdb")
check(out is not None, "the worker accepts a database from the custom folder")
if out is not None:
    append(out)
    used = sorted({s.material.name for o in step_objects()
                   for s in o.material_slots if s.material})
    check(used == ["Workshop authored"],
          "the appended parts carry the database material (%s)" % used)
prefs().active_matdb = "NONE"
prefs().matdb_dir = ""

# -- 2. collection instances ---------------------------------------------------
print("\n== collection instances")
ASSY = os.path.join(_HERE, "fixtures", "assembly.step")


def layer_state():
    """{collection name: excluded} for the collections of the import, and
    the names of the mesh objects that show."""
    excluded = {}

    def walk(lc):
        for child in lc.children:
            if child.collection.get("STEP_file") == ASSY:
                excluded[child.collection.get("STEP_role", "")
                         + ":" + child.name.split(".")[-1]] = child.exclude
            walk(child)
    walk(bpy.context.view_layer.layer_collection)
    bpy.context.view_layer.update()
    shown = sorted(o.name for o in bpy.context.view_layer.objects
                   if o.type == "MESH" and o.visible_get())
    return excluded, shown


clean()
m._cache_drop(ASSY)
m.load_step(bpy.context, ASSY, htypes="COLLECTION_INSTANCES", up_as="Z")
direct = layer_state()
print("    direct:", direct)
check(any(direct[0].values()),
      "a direct import hides the prototype collection")

clean()
out = run_worker({"hierarchy_types": "COLLECTION_INSTANCES", "up_as": "ZPOS"},
                 "instances", filepath=ASSY)
check(out is not None, "the worker imports with collection instances")
if out is not None:
    append(out)
    appended = layer_state()
    print("    appended:", appended)
    check(appended[0] == direct[0],
          "the append hides the same collections as a direct import")
    check(appended[1] == direct[1],
          "the same mesh objects show as after a direct import (%d vs %d)"
          % (len(appended[1]), len(direct[1])))

# -- 3. materials the scene has already ---------------------------------------
print("\n== materials the scene has already")
clean()
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z", custom_scale=1.0)
first = {s.material for o in step_objects() for s in o.material_slots
         if s.material}
check(len(first) == 1, "the first import made one material (%s)"
      % sorted(mat.name for mat in first))
for mat in first:
    mat["edited_by_user"] = True
names_before = sorted(mat.name for mat in bpy.data.materials)
out = run_worker({"hierarchy_types": "TREE", "up_as": "ZPOS",
                  "custom_scale": True, "user_scale": 1.0}, "materials")
check(out is not None, "the worker imports the file a second time")
if out is not None:
    append(out)
    names_after = sorted(mat.name for mat in bpy.data.materials)
    check(names_after == names_before,
          "the append adds no copy of a material (%s, was %s)"
          % (names_after, names_before))
    used = {s.material for o in step_objects() for s in o.material_slots
            if s.material}
    check(len(step_objects()) == 4 and used == first,
          "both imports use the one material (%s)"
          % sorted(mat.name for mat in used))

# -- 4. the 3D cursor ----------------------------------------------------------
print("\n== the 3D cursor")
CURSOR = (1.0, 2.0, 0.5)


def placement():
    """Each part's lowest corner, and the refresh basis stamped on it."""
    bpy.context.view_layer.update()
    out = []
    for obj in step_objects():
        pts = [obj.matrix_world @ v.co for v in obj.data.vertices]
        out.append(tuple(round(min(p[k] for p in pts), 5) for k in range(3)))
    return sorted(out)


def basis():
    return sorted(tuple(round(v, 5) for v in o.get(R.BASIS_PROP, ()))
                  for o in step_objects())


clean()
bpy.context.scene.cursor.location = CURSOR
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z", custom_scale=1.0)
direct = placement()
direct_basis = basis()
print("    direct:", direct)
check(bool(direct) and direct[0][2] == CURSOR[2],
      "a direct import lands at the 3D cursor")

clean()
bpy.context.scene.cursor.location = CURSOR
out = run_worker({"hierarchy_types": "TREE", "up_as": "ZPOS",
                  "custom_scale": True, "user_scale": 1.0}, "cursor")
check(out is not None, "the worker imports with the cursor moved")
if out is not None:
    append(out)
    appended = placement()
    print("    appended:", appended)
    check(appended == direct,
          "a background import lands where a direct import does")
    check(basis() == direct_basis,
          "and records the same placement for a later refresh")

if FAILS:
    print("\nbackground_parity_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nbackground_parity_smoke: OK - a background import matches a direct "
      "one")
