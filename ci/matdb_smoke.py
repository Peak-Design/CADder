# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the material database on a direct send.

    blender -b --factory-startup -P ci/matdb_smoke.py

Writes a database that maps the SolidWorks appearance name "SW polished
gold" to an authored material, selects it, and imports a .swmesh whose
material is that appearance. The imported object must end up with the
authored material, and must keep the original name in STEP_materials so
the database still matches on a later pass.
"""

import json
import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from STEPper_NEXT import main  # noqa: E402
from STEPper_NEXT.rig import matdb, native_import, swmesh  # noqa: E402


def _check(cond, msg):
    if not cond:
        raise SystemExit("matdb_smoke: FAIL: " + msg)


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, appearance_json):
    body = struct.pack("<III", swmesh.MAGIC, 2, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<III", 1, 1, 1)
    body += _text("polished gold") + struct.pack("<6f", 0.97, 0.88, 0.6, 1.0, 0.12, 1.0) + _text("")
    raw = appearance_json.encode("utf-8")
    body += struct.pack("<I", len(raw)) + raw
    body += struct.pack("<i", 1) + _text("pin")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.05, 0, 0, 0, 0.05, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    rows = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    body += struct.pack("<i", 1) + _text("c001") + _text("pin-1") + struct.pack("<16d", *rows)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def main_():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
    tmp = tempfile.mkdtemp(prefix="cadlink_matdb_")
    db_dir = os.path.join(tmp, "MaterialDB")
    os.makedirs(db_dir)
    prefs = bpy.context.preferences.addons["STEPper_NEXT"].preferences
    prefs.matdb_dir = db_dir

    authored = bpy.data.materials.new("Brass authored")
    authored.use_nodes = True
    authored.diffuse_color = (0.6, 0.4, 0.05, 1.0)
    db_path = os.path.join(db_dir, "workshop.blend")
    main._write_material_database(db_path, {"SW polished gold": "Brass authored"})
    _check(os.path.isfile(db_path), "the database file was not written")
    bpy.data.materials.remove(authored)

    prefs.active_matdb = "workshop"
    _check(matdb.active_path() == db_path, "the active database is %r" % matdb.active_path())

    spec = {"name": "polished gold", "category": "metal/gold", "colour": [0.97, 0.88, 0.6],
            "blender": {"roughness": 0.12, "metallic": 1.0, "glass": False},
            "library": {"sw_shader": "polishedgold"}, "decals": []}
    mesh = write_mesh(os.path.join(tmp, "pin.swmesh"), json.dumps(spec))
    objs, _report = native_import.build(bpy.context, mesh, manifest=None, hierarchy="FLAT")
    _check(len(objs) == 1, "imported %d objects" % len(objs))
    obj = objs[0]
    slots = [m.name for m in obj.data.materials]
    _check(slots == ["Brass authored"], "the database did not replace the material: %s" % slots)
    _check(json.loads(obj["STEP_materials"]) == ["SW polished gold"],
           "the original name was lost: %s" % obj.get("STEP_materials"))

    # Scanning the scene finds the same pairing back, which is how a user
    # builds a database from a send they have re-materialled by hand.
    scanned = main._scan_scene_materials()
    _check(scanned.get("SW polished gold") == "Brass authored",
           "a scene scan does not see the direct send: %s" % scanned)

    # With no database selected the appearance material is built as before.
    prefs.active_matdb = "NONE"
    bpy.data.objects.remove(obj, do_unlink=True)
    objs, _r = native_import.build(bpy.context, mesh, manifest=None, hierarchy="FLAT")
    slots = [m.name for m in objs[0].data.materials]
    _check(slots == ["SW polished gold"], "without a database the appearance is not used: %s" % slots)

    print("matdb_smoke: OK: a direct send takes the authored material from the "
          "database, keeps the appearance name for a later pass, is found by a "
          "scene scan, and is untouched when no database is selected")


main_()
