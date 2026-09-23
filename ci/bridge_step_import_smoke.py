# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a direct send stops at a STEP import of its assembly.

    blender -b --factory-startup --python-exit-code 1 -P ci/bridge_step_import_smoke.py

A send from SolidWorks deleted a STEP import of the same assembly and put
its own parts in its place. The work done on the STEP import went with it,
and Lock Materials and Lock Geometry could not find its parts again. Left
standing next to the send, every part was in the scene twice. Oscar,
2026-09-23: "I never intended to support replacing STEP files with direct
send." So the send now stops before it changes anything, and says why.

Every send runs through bridge._run_job, the path SolidWorks drives.
"""

import os
import struct
import sys
import tempfile

import addon_utils
import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge  # noqa: E402
from CADder.rig import swmesh  # noqa: E402


def fail(where, msg):
    raise SystemExit("bridge_step_import_smoke: FAIL: %s: %s" % (where, msg))


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def _t(x):
    return [1, 0, 0, x, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


PARTS = [("c001", "base-1", 0.0, "base"), ("c002", "arm-1", 0.2, "arm")]


def write_mesh(path, parts=PARTS, size=0.05):
    """One triangle per part, as bridge_no_manifest_smoke writes it."""
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, len(parts), len(parts), 0)
    body += _text("gray") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    for i, (_cid, _path, _x, name) in enumerate(parts):
        s = size * (i + 1)
        body += struct.pack("<i", i + 1) + _text(name)
        body += struct.pack("<II", 3, 1)
        body += struct.pack("<9f", 0, 0, 0, s, 0, 0, 0, s, 0)
        body += struct.pack("<3i", 0, 1, 2)
        body += struct.pack("<i", 0)
    for i, (cid, path_, x, name) in enumerate(parts):
        body += (struct.pack("<i", i + 1) + _text(cid) + _text(name)
                 + _text(path_) + struct.pack("<16d", *_t(x))
                 + struct.pack("<B", 0))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def send(mesh, quads=None):
    options = {"hierarchy_types": "FLAT", "up_as": "ZPOS"}
    if quads is not None:
        options["tris_to_quads"] = quads
    return bridge._run_job({
        "step": None, "mesh": mesh, "manifest": None,
        "steps": {"import": False, "replace": True, "update": False,
                  "match": False, "sync_poses": False, "build_rig": False,
                  "relink": False, "cleanup": False},
        "import_options": options,
    })


def step_import(step_file, names, rigged=False):
    """What a STEP import leaves: tagged objects in a tagged collection."""
    col = bpy.data.collections.new(os.path.basename(step_file) + ".flat")
    col["STEP_file"] = step_file
    bpy.context.scene.collection.children.link(col)
    out = []
    for name in names:
        me = bpy.data.meshes.new(name)
        me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        obj = bpy.data.objects.new(name, me)
        obj["STEP_file"] = step_file
        if rigged:
            obj["RIG_group"] = "g001"
        col.objects.link(obj)
        out.append(obj)
    return out


def sent_parts():
    return sorted(o.name for o in bpy.data.objects
                  if o.get("SWMESH_file") == "gearbox"
                  and o.get("RIG_component_id"))


def run(tmp):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not addon_utils.enable("CADder", default_set=True, handle_error=None):
        fail("setup", "CADder did not enable")
    export = os.path.join(tmp, "exports", "gearbox")
    mesh = write_mesh(os.path.join(export, "gearbox.swmesh"))

    # A STEP import that the add-in wrote, in the folder of the send, and
    # one of a vendor's file of the same name, which is not this assembly.
    ours = step_import(os.path.join(export, "gearbox.step"), ["gear", "shaft"])
    vendor = step_import(os.path.join(tmp, "Downloads", "gearbox.step"),
                         ["vendor gear"])
    prg = bpy.context.scene.stepper
    quads = prg.tris_to_quads
    result = send(mesh, quads=not quads)
    where = "send with a STEP import of the assembly"
    if result.get("ok"):
        fail(where, "the send went through")
    error = result.get("error") or ""
    for words in ("STEP import of gearbox", "2 objects", "Delete that import"):
        if words not in error:
            fail(where, "the message %r does not say %r" % (error, words))
    for obj in ours + vendor:
        if bpy.data.objects.get(obj.name) is None:
            fail(where, "%s was removed" % obj.name)
    if sent_parts():
        fail(where, "parts arrived: %s" % sent_parts())
    # It stopped before it changed anything, the settings of the send too.
    if prg.tris_to_quads != quads:
        fail(where, "the send changed Triangles to Quads")
    if "replace" in (result.get("stages") or {}):
        fail(where, "a replace stage ran: %s" % result["stages"])

    # A STEP import that was matched to a rig is this assembly's, from any
    # folder: exported by hand and rigged in Blender.
    for obj in ours:
        bpy.data.objects.remove(obj)
    hand = step_import(os.path.join(tmp, "Desktop", "gearbox.step"),
                       ["rigged gear"], rigged=True)
    where = "send with a rigged STEP import of the assembly"
    if send(mesh).get("ok"):
        fail(where, "the send went through")
    if bpy.data.objects.get(hand[0].name) is None:
        fail(where, "the rigged import was removed")

    # With only the vendor's file left, the send goes through and leaves it.
    bpy.data.objects.remove(hand[0])
    where = "send with only a file of the same name"
    result = send(mesh)
    if not result.get("ok"):
        fail(where, "the send failed: %s" % result.get("error"))
    if sent_parts() != ["arm", "base"]:
        fail(where, "the parts are %s" % sent_parts())
    if bpy.data.objects.get("vendor gear") is None:
        fail(where, "the vendor's import was removed")

    # A second send replaces the first one, as before.
    where = "second send"
    result = send(mesh)
    if not result.get("ok"):
        fail(where, "the send failed: %s" % result.get("error"))
    if sent_parts() != ["arm", "base"]:
        fail(where, "the parts are %s" % sent_parts())


def main():
    tmp = os.path.join(tempfile.gettempdir(), "bridge_step_import_smoke")
    run(tmp)
    print("bridge_step_import_smoke: OK: a direct send stops at a STEP import "
          "of its assembly and changes nothing, and goes through next to a "
          "file that only has the same name")


main()
