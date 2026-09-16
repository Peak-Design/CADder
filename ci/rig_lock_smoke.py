# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a LOCKED rig.

    blender -b --factory-startup -P ci/rig_lock_smoke.py

A generated rig is a starting point. A user who renames a bone, adds a
control or changes a constraint has made something the CAD application
cannot send again, and the next send rebuilt the rig and took it away
(Oscar, 2026-09-16). Locking the rig keeps it: the geometry is still
replaced and still attached to the bones, and only the rig is left alone.

The whole send runs through bridge._run_job, the same path SolidWorks
drives, so what is tested is what a send does.
"""

import json
import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import rig_build, swmesh, ui as rig_ui  # noqa: E402

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "lock.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "base-1", "step_name": "base",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
        {"id": "c002", "sw_path": "arm-1", "step_name": "arm",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0.2], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.3},
        {"id": "g001", "name": "arm", "components": ["c002"], "grounded": False,
         "frame": None, "bbox_diag": 0.2},
    ],
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [0.2, 0, 0],
         "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None},
    ],
    "loops": [],
    "warnings": [],
}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path):
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, 1, 2, 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.1, 0, 0, 0, 0.1, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, name, tx in (("c001", "base", 0.0), ("c002", "arm", 0.2)):
        rows = [1, 0, 0, tx, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        body += (struct.pack("<i", 1) + _text(cid) + _text(name)
                 + _text(name + "-1") + struct.pack("<16d", *rows)
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def _check(cond, msg):
    if not cond:
        raise SystemExit("rig_lock_smoke: FAIL: " + msg)


def send(mesh_path, manifest_path):
    """One send, exactly as the CAD application asks for it."""
    return bridge._run_job({
        "step": None, "mesh": mesh_path, "manifest": manifest_path,
        "steps": {"import": False, "replace": True, "match": True,
                  "sync_poses": True, "build_rig": True, "relink": True,
                  "cleanup": True},
        "import_options": {"hierarchy_types": "FLAT", "up_as": "ZPOS"},
    })


def rigs():
    return [o for o in bpy.data.objects
            if o.type == "ARMATURE" and o.get("RIG_rig")]


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()

    tmp = tempfile.gettempdir()
    manifest_path = os.path.join(tmp, "lock.rig.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(MANIFEST, fh)
    mesh_path = write_mesh(os.path.join(tmp, "lock.swmesh"))

    result = send(mesh_path, manifest_path)
    _check(result.get("ok"), "the first send failed: %s" % result.get("error"))
    _check(len(rigs()) == 1, "the first send built %d rig(s)" % len(rigs()))
    arm = rigs()[0]
    bones = len(arm.data.bones)

    # What a user does to a generated rig: a control of their own, and a
    # constraint they changed. Neither is in the manifest, so neither can
    # be sent again.
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    added = arm.data.edit_bones.new("hand_made")
    added.head = (0.0, 0.0, 0.5)
    added.tail = (0.0, 0.0, 0.6)
    bpy.ops.object.mode_set(mode="OBJECT")
    arm.pose.bones["hand_made"]["mine"] = True
    _check(len(arm.data.bones) == bones + 1, "the test bone was not added")

    # Unlocked, a send takes it away. That is the behaviour the lock
    # exists to stop, so it is pinned here as well.
    result = send(mesh_path, manifest_path)
    _check(result.get("ok"), "the second send failed: %s" % result.get("error"))
    _check("hand_made" not in rigs()[0].data.bones,
           "an unlocked rig kept a hand-made bone, so this test proves nothing")

    # Lock it, through the operator the panel draws.
    arm = rigs()[0]
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    added = arm.data.edit_bones.new("hand_made")
    added.head = (0.0, 0.0, 0.5)
    added.tail = (0.0, 0.0, 0.6)
    bpy.ops.object.mode_set(mode="OBJECT")
    _check("FINISHED" in bpy.ops.cadlink.lock_rig(), "the lock operator failed")
    _check(rig_build.is_locked(arm), "the rig is not locked")
    locked_name = arm.name
    armature_data = arm.data.name

    # The send that would have taken it away.
    result = send(mesh_path, manifest_path)
    _check(result.get("ok"), "the send over a locked rig failed: %s"
           % result.get("error"))
    _check(result["stages"].get("rig") == {"locked": locked_name},
           "the send did not report the rig as locked: %s"
           % result["stages"].get("rig"))
    _check(len(rigs()) == 1, "the send left %d rig(s)" % len(rigs()))
    kept = rigs()[0]
    _check(kept.name == locked_name and kept.data.name == armature_data,
           "the rig was replaced: %s / %s" % (kept.name, kept.data.name))
    _check("hand_made" in kept.data.bones, "the hand-made bone was lost")

    # The geometry still arrived, and still hangs from the rig: a locked
    # rig is not a stopped send.
    parts = [o for o in bpy.data.objects if o.get("RIG_component_id")]
    _check(len(parts) == 2, "the send brought %d part(s)" % len(parts))
    for obj in parts:
        _check(obj.parent is kept and obj.parent_type == "BONE",
               "%s is not on a bone of the locked rig" % obj.name)

    # The operator that rebuilds is refused while it is locked, with a
    # reason rather than a failure.
    _check(not bpy.ops.cadlink.build_rig.poll(),
           "Build Rig is still offered over a locked rig")

    # And unlocking gives the old behaviour back.
    bpy.context.view_layer.objects.active = kept
    _check("FINISHED" in bpy.ops.cadlink.lock_rig(), "the unlock failed")
    _check(not rig_build.is_locked(kept), "the rig is still locked")
    result = send(mesh_path, manifest_path)
    _check(result.get("ok"), "the send after unlocking failed: %s"
           % result.get("error"))
    _check("bones" in (result["stages"].get("rig") or {}),
           "the rig was not built again after unlocking: %s"
           % result["stages"].get("rig"))
    _check("hand_made" not in rigs()[0].data.bones,
           "the rebuilt rig still holds the hand-made bone")

    print("rig_lock_smoke: OK: a locked rig survives a send with its own "
          "bones, the parts arrive and attach to it, Build Rig is refused "
          "while it is locked, and unlocking rebuilds as before")


main()
