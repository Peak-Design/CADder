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
from CADder import main as m, background as B

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


def run_worker(op_kwargs, name):
    """Run the worker as the modal operator does. Return the blend path, or
    None when the worker failed."""
    out_blend = os.path.join(tmp, name + ".blend")
    request = {
        "addon_module": "CADder",
        "filepath": STEP,
        "out_blend": out_blend,
        "op_kwargs": op_kwargs,
        "scene_unit_scale": bpy.context.scene.unit_settings.scale_length,
        "prefs": B.prefs_snapshot(prefs()),
        "parent_pid": os.getpid(),
    }
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

if FAILS:
    print("\nbackground_parity_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nbackground_parity_smoke: OK - a background import matches a direct "
      "one")
