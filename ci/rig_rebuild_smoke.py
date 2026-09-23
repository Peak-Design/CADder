# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a rig rebuilt inside its own armature (rig mode
APPEND): what the user added or keyed must still be there after it.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_rebuild_smoke.py

Each case sends an assembly, changes the rig the way a user does, sends
an update with rig_mode APPEND and checks what came back:

  * a limited ball keeps the name of the handle the user keys, update
    after update;
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
from CADder.rig import swmesh, ui as rig_ui  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("   FAIL:", msg)
    return cond


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


def manifest(stem, parts, joints):
    """parts: (component id, path, persistent id, x, group name). The
    first part is the ground."""
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": stem + ".step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": cid, "sw_path": path, "step_name": group,
             "step_occurrence_path": None, "sw_persistent_id": pid,
             "transform": _t(x)}
            for cid, path, pid, x, group in parts],
        "rigid_groups": [
            {"id": "g%03d" % i, "name": group, "components": [cid],
             "grounded": i == 0, "frame": None, "bbox_diag": 0.2}
            for i, (cid, path, pid, x, group) in enumerate(parts)],
        "joints": joints,
        "loops": [], "warnings": [],
    }


def hinge(jid, parent, child, x):
    return {"id": jid, "type": "revolute", "parent_group": parent,
            "child_group": child, "origin": [x, 0, 0], "axis": [0, 0, 1],
            "secondary_axis": [1, 0, 0], "limits": None}


def cone_ball(jid, parent, child, x):
    """A ball held in a 45 degree cone about +Z, resting on the axis."""
    return {"id": jid, "type": "ball", "parent_group": parent,
            "child_group": child, "origin": [x, 0, 0], "axis": [0, 0, 1],
            "secondary_axis": [0, 0, 1],
            "limits": {"rotation": {"min": 0.0, "max": 0.7853981633974483,
                                    "value_at_rest": 0.0},
                       "translation": None}}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, parts):
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, 1, len(parts), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.1, 0, 0, 0, 0.1, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, path_, pid, x, group in parts:
        body += (struct.pack("<i", 1) + _text(cid) + _text(group)
                 + _text(path_) + struct.pack("<16d", *_flat(_t(x)))
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def send(stem, parts, joints, update=False):
    """One send of the assembly `stem`: a first send, or an update that
    rebuilds the rig inside its armature."""
    tmp = tempfile.gettempdir()
    manifest_path = os.path.join(tmp, stem + ".rig.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest(stem, parts, joints), fh)
    mesh = write_mesh(os.path.join(tmp, stem + ".swmesh"), parts)
    payload = {
        "step": None, "mesh": mesh, "manifest": manifest_path,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": "FLAT", "up_as": "ZPOS"},
    }
    if update:
        payload["rig_mode"] = "APPEND"
    result = bridge._run_job(payload)
    check(result.get("ok"), "%s: the send failed: %s"
          % (stem, result.get("error")))
    return result


def fcurves(obj):
    animation = obj.animation_data
    action = animation.action if animation is not None else None
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(animation.action_slot)
            if bag is not None:
                out.extend(bag.fcurves)
    return out


def rig_object():
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and obj.get("RIG_rig"):
            return obj
    return None


def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()


# ── A limited ball keeps its handle's name ──────────────────────────────

def ball_handle_keeps_its_name():
    print("-- a limited ball, keyed, then updated twice")
    fresh()
    parts = [("c001", "base-1", "pbase", 0.0, "base"),
             ("c002", "stud-1", "pstud", 0.2, "stud")]
    joints = [cone_ball("j001", "g000", "g001", 0.2)]
    send("ballrig", parts, joints)
    arm = rig_object()
    if not check(arm is not None, "no rig was built"):
        return
    build = rig_ui._STATE["build"]
    handle = build.ball_ctrl_names.get("g001")
    if not check(handle, "the ball has no handle bone"):
        return

    pb = arm.pose.bones[handle]
    for frame, angle in ((1, 0.0), (20, 0.3)):
        pb.rotation_euler[0] = angle
        pb.keyframe_insert("rotation_euler", index=0, frame=frame)
    path = 'pose.bones["%s"].rotation_euler' % handle
    # The first update is of a rig built before the handle said which
    # manifest it came from.
    if "RIG_source" in pb.keys():
        del pb["RIG_source"]

    for n in (1, 2):
        send("ballrig", parts, joints, update=True)
        now = rig_object()
        names = sorted(b.name for b in now.data.bones)
        build = rig_ui._STATE["build"]
        check(build.ball_ctrl_names.get("g001") == handle,
              "update %d: the handle is %r, was %r (bones %s)"
              % (n, build.ball_ctrl_names.get("g001"), handle, names))
        check(not any(name.startswith("DEF_DEF_") for name in names),
              "update %d: a DEF bone was named after a DEF bone: %s"
              % (n, names))
        check(any(fc.data_path == path for fc in fcurves(now))
              and handle in now.pose.bones,
              "update %d: the keys no longer name the handle" % n)
    print("   bones after two updates:",
          sorted(b.name for b in rig_object().data.bones))


def main():
    ball_handle_keeps_its_name()

    print()
    if FAILS:
        print("rig_rebuild_smoke: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("rig_rebuild_smoke: OK")


main()
