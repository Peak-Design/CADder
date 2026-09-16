# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for an update that keeps the animation: rig mode APPEND.

    blender -b --factory-startup -P ci/rig_append_smoke.py

A part added or deleted in SolidWorks changes the rig, and rebuilding it
takes any animation with it: a keyframe names its bone, and a bone built
again under a new name is a bone nothing is keyed to. APPEND rebuilds the
rig INSIDE the armature that is already there, and a body made of the same
parts as before comes back under the same name, so the keys still find it
(Oscar, 2026-09-16).

The assembly here gains a part and loses one, which also shifts every
component and group id, which is exactly the case a name built from those
ids cannot survive.
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


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


def manifest(parts, joints):
    """parts: (component id, path, persistent id, x, group name)."""
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "app.step", "ap": "AP214",
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


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, instances):
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, 1, len(instances), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.1, 0, 0, 0, 0.1, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, name, where, x in instances:
        body += (struct.pack("<i", 1) + _text(cid) + _text(name)
                 + _text(where) + struct.pack("<16d", *_flat(_t(x)))
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def _check(cond, msg):
    if not cond:
        raise SystemExit("rig_append_smoke: FAIL: " + msg)


def send(mesh, manifest_path, update=False, rig_mode=None):
    payload = {
        "step": None, "mesh": mesh, "manifest": manifest_path,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": "FLAT", "up_as": "ZPOS"},
    }
    if rig_mode:
        payload["rig_mode"] = rig_mode
    return bridge._run_job(payload)


def fcurves(obj):
    """The action's curves, whichever way this Blender holds them: 5.x
    puts them in a channelbag under a layer and a strip, older ones on the
    action itself."""
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


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    tmp = tempfile.gettempdir()

    # ── The assembly: a base, an arm on a hinge, and a clip on the arm ──
    before = [("c001", "base-1", "pbase", 0.0, "base"),
              ("c002", "arm-1", "parm", 0.2, "arm"),
              ("c003", "clip-1", "pclip", 0.4, "clip")]
    first = os.path.join(tmp, "app.rig.json")
    with open(first, "w", encoding="utf-8") as fh:
        json.dump(manifest(before, [hinge("j001", "g000", "g001", 0.2),
                                    hinge("j002", "g001", "g002", 0.4)]), fh)
    mesh = write_mesh(os.path.join(tmp, "app.swmesh"),
                      [("c001", "base", "base-1", 0.0),
                       ("c002", "arm", "arm-1", 0.2),
                       ("c003", "clip", "clip-1", 0.4)])
    result = send(mesh, first)
    _check(result.get("ok"), "the first send failed: %s" % result.get("error"))

    arm_obj = rig_object()
    _check(arm_obj is not None, "no rig was built")
    build = rig_ui._STATE["build"]
    arm_bone = build.bone_names["g001"]
    clip_bone = build.bone_names["g002"]

    # ── An animation on the arm, and a bone of the user's own ───────────
    bpy.context.view_layer.objects.active = arm_obj
    pb = arm_obj.pose.bones[arm_bone]
    pb.rotation_mode = "XYZ"
    for frame, angle in ((1, 0.0), (20, 0.6)):
        pb.rotation_euler[2] = angle
        pb.keyframe_insert("rotation_euler", index=2, frame=frame)
    bpy.ops.object.mode_set(mode="EDIT")
    mine = arm_obj.data.edit_bones.new("mine")
    mine.head = (0.0, 0.0, 0.5)
    mine.tail = (0.0, 0.0, 0.6)
    bpy.ops.object.mode_set(mode="OBJECT")
    action = arm_obj.animation_data.action.name
    keys_before = len(fcurves(arm_obj))
    armature_data = arm_obj.data.name

    # ── The edit in CAD: the clip is gone and a guard is new. Every id
    #    shifts, which is what a rig keyed to ids cannot survive. ────────
    after = [("c001", "base-1", "pbase", 0.0, "base"),
             ("c002", "arm-1", "parm", 0.2, "arm"),
             ("c003", "guard-1", "pguard", 0.7, "guard")]
    second = os.path.join(tmp, "app.rig.json")
    with open(second, "w", encoding="utf-8") as fh:
        json.dump(manifest(after, [hinge("j001", "g000", "g001", 0.2),
                                   hinge("j002", "g001", "g002", 0.7)]), fh)
    mesh = write_mesh(os.path.join(tmp, "app.swmesh"),
                      [("c001", "base", "base-1", 0.0),
                       ("c002", "arm", "arm-1", 0.2),
                       ("c003", "guard", "guard-1", 0.7)])
    result = send(mesh, second, update=True, rig_mode="APPEND")
    _check(result.get("ok"), "the update failed: %s" % result.get("error"))

    # ── The structure followed the CAD ──────────────────────────────────
    changed = result["stages"]["update"]
    _check(changed["added"] and changed["removed"],
           "the update saw no change: %s" % changed)
    _check(bpy.data.objects.get("clip") is None, "the deleted part is still here")

    # ── And the rig is the same rig ─────────────────────────────────────
    now = rig_object()
    _check(now is arm_obj, "the armature object was replaced")
    _check(now.data.name == armature_data, "the armature data was replaced")
    stage = result["stages"]["rig"]
    _check(stage.get("mode") == "APPEND", "the rig stage says %s" % stage)

    _check(arm_bone in now.data.bones,
           "the arm's bone lost its name: %s" % sorted(b.name for b in now.data.bones))
    _check(clip_bone not in now.data.bones,
           "the deleted part kept its bone")
    _check("mine" in now.data.bones, "the bone the user added was removed")

    # The animation still drives the arm.
    _check(now.animation_data is not None
           and now.animation_data.action is not None
           and now.animation_data.action.name == action,
           "the action was lost")
    curves = fcurves(now)
    _check(len(curves) == keys_before,
           "the curves changed: %d, was %d" % (len(curves), keys_before))
    path = 'pose.bones["%s"].rotation_euler' % arm_bone
    _check(any(fc.data_path == path for fc in curves),
           "no curve still names the arm's bone: %s"
           % [fc.data_path for fc in curves])

    # And it still poses the part it is keyed to.
    bpy.context.scene.frame_set(20)
    bpy.context.view_layer.update()
    pb = now.pose.bones[arm_bone]
    _check(abs(pb.rotation_euler[2] - 0.6) < 1e-5,
           "the keyed pose did not come back: %.4f" % pb.rotation_euler[2])

    # ── REGENERATE is the other answer: a new rig, and the animation is
    #    left behind with the old one. ────────────────────────────────────
    result = send(mesh, second, update=True, rig_mode="REGENERATE")
    _check(result.get("ok"), "the regenerate failed: %s" % result.get("error"))
    fresh = rig_object()
    _check(fresh is not arm_obj, "REGENERATE reused the standing rig")
    _check("mine" not in fresh.data.bones,
           "REGENERATE kept a bone from the old rig")

    print("rig_append_smoke: OK: an assembly edit that shifts every id "
          "leaves the arm's bone, its keyframes and the user's own bone "
          "alone, drops the bone of the deleted part, and REGENERATE still "
          "builds a new rig")


main()
