# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Refresh Model on a posed rig.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_refresh_pose_smoke.py

Oscar, 2026-09-22: "If I move some bones in pose mode, then refresh the
model, the rig updates, but the parts that were parented to the old bones
don't reset to the SW pose before being re-parented, meaning everything
gets offset from where it should be."

The rig is posed (by hand, and by a keyframe), a part is moved in CAD, and
the scene is refreshed in every rig mode and every hierarchy mode. After
each refresh every part has to sit on its SolidWorks pose, and posing a bone
afterwards has to carry its parts with it exactly: a part bound while the
rig was posed carries that pose as an offset for ever after. The limit
dial of the arm is there too, because a rebuild in place used to leave the
old dial behind and make a second one beside it.
"""

import json
import os
import struct
import sys
import tempfile

import bpy
from mathutils import Matrix

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import native_import, swmesh, ui as rig_ui  # noqa: E402

TOL = 1e-5


def _t(x, y=0.0):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


# component id, path, persistent id, x, group, step name
BEFORE = [
    ("c001", "base-1", "pbase", 0.0, 0, "base"),
    ("c002", "armsub-1/arm-1", "parm", 0.2, 1, "arm"),
    ("c003", "armsub-1/pin-1", "ppin", 0.3, 1, "pin"),
    ("c004", "clip-1", "pclip", 0.4, 2, "clip"),
]
# The clip moved in CAD, and its hinge with it.
AFTER = [
    ("c001", "base-1", "pbase", 0.0, 0, "base"),
    ("c002", "armsub-1/arm-1", "parm", 0.2, 1, "arm"),
    ("c003", "armsub-1/pin-1", "ppin", 0.3, 1, "pin"),
    ("c004", "clip-1", "pclip", 0.45, 2, "clip"),
]
GROUP_NAMES = ("base", "arm", "clip")


def manifest(parts, clip_x, name="app"):
    groups = {}
    for cid, _path, _pid, _x, g, _name in parts:
        groups.setdefault(g, []).append(cid)
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": name + ".step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": cid, "sw_path": path, "step_name": name,
             "step_occurrence_path": None, "sw_persistent_id": pid,
             "transform": _t(x)}
            for cid, path, pid, x, _g, name in parts],
        "rigid_groups": [
            {"id": "g%03d" % g, "name": GROUP_NAMES[g], "components": cids,
             "grounded": g == 0, "frame": None, "bbox_diag": 0.2}
            for g, cids in sorted(groups.items())],
        "joints": [
            {"id": "j001", "type": "revolute", "parent_group": "g000",
             "child_group": "g001", "origin": [0.2, 0, 0], "axis": [0, 0, 1],
             "secondary_axis": [1, 0, 0],
             "limits": {"rotation": {"min": -1.0, "max": 1.0,
                                     "value_at_rest": 0.0},
                        "translation": None}},
            {"id": "j002", "type": "revolute", "parent_group": "g001",
             "child_group": "g002", "origin": [clip_x, 0, 0],
             "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None},
        ],
        "loops": [], "warnings": [],
    }


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
    body += struct.pack("<9f", 0, 0, 0, 0.05, 0, 0, 0, 0.05, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, path_, _pid, x, _g, name in parts:
        body += (struct.pack("<i", 1) + _text(cid) + _text(name)
                 + _text(path_) + struct.pack("<16d", *_flat(_t(x)))
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def export(tmp, parts, clip_x, name="app"):
    man = os.path.join(tmp, name + ".rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(manifest(parts, clip_x, name), fh)
    return write_mesh(os.path.join(tmp, name + ".swmesh"), parts), man


def send(mesh, man, hierarchy, up_as, update=False, rig_mode=None, steps=None):
    payload = {
        "step": None, "mesh": mesh, "manifest": man,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": hierarchy, "up_as": up_as},
    }
    payload["steps"].update(steps or {})
    if rig_mode:
        payload["rig_mode"] = rig_mode
    return bridge._run_job(payload)


def rig_object():
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and obj.get("RIG_rig"):
            return obj
    return None


def parts():
    """component id -> the object that carries it."""
    out = {}
    for obj in bpy.data.objects:
        cid = obj.get("RIG_component_id")
        if cid:
            out[cid] = obj
    return out


def sw_world(obj, frame):
    rows = [list(obj["SWMESH_transform"][i * 4:(i + 1) * 4]) for i in range(4)]
    return frame @ Matrix(rows)


def off(a, b):
    return max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


def fail(where, msg):
    raise SystemExit("rig_refresh_pose_smoke: FAIL: %s: %s" % (where, msg))


def bone_of(arm_obj, group):
    for pb in arm_obj.pose.bones:
        if pb.get("RIG_group") == group:
            return pb
    return None


def check_on_sw_pose(where, frame):
    for cid, obj in sorted(parts().items()):
        d = off(obj.matrix_world, sw_world(obj, frame))
        if d > TOL:
            fail(where, "%s (%s) is %.5f off its SolidWorks pose"
                 % (obj.name, cid, d))


def check_carried(where, arm_obj, frame):
    """Pose the arm and the clip, and every part must move exactly as its
    bone does."""
    rest = {cid: obj.matrix_world.copy() for cid, obj in parts().items()}
    for group, angle in (("g001", 0.4), ("g002", -0.3)):
        pb = bone_of(arm_obj, group)
        if pb is None:
            fail(where, "no bone for %s" % group)
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = (0.0, angle, 0.0)
    bpy.context.view_layer.update()
    moved = 0
    for cid, obj in sorted(parts().items()):
        if obj.parent is not arm_obj or obj.parent_type != "BONE":
            # Under a tree empty, which is what rides the bone.
            holder = obj.parent
            while holder is not None and holder.parent is not arm_obj:
                holder = holder.parent
            if holder is None:
                fail(where, "%s is not on the rig" % obj.name)
            bone = holder.parent_bone
        else:
            bone = obj.parent_bone
        pb = arm_obj.pose.bones[bone]
        bone_delta = (arm_obj.matrix_world @ pb.matrix) @ (
            arm_obj.matrix_world @ pb.bone.matrix_local).inverted()
        part_delta = obj.matrix_world @ rest[cid].inverted()
        d = off(part_delta, bone_delta)
        if d > TOL:
            fail(where, "%s does not follow bone %s: %.5f off" % (obj.name, bone, d))
        if off(part_delta, Matrix.Identity(4)) > 1e-3:
            moved += 1
    if moved < 3:
        fail(where, "posing the arm and the clip moved only %d part(s)" % moved)
    for group in ("g001", "g002"):
        bone_of(arm_obj, group).rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()


def run(tmp, hierarchy, up_as, mode, keyed):
    where = "%s/%s/%s/%s" % (hierarchy, up_as, mode, "keyed" if keyed else "posed")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    frame = Matrix([tuple(r) for r in native_import.up_frame(up_as)])
    scene = bpy.context.scene
    scene.frame_set(1)

    mesh, man = export(tmp, BEFORE, 0.4)
    result = send(mesh, man, hierarchy, up_as)
    if not result.get("ok"):
        fail(where, "the first send failed: %s" % result.get("error"))
    arm_obj = rig_object()
    if arm_obj is None:
        fail(where, "no rig was built")
    check_on_sw_pose(where + " after the send", frame)
    bones_before = sorted(b.name for b in arm_obj.data.bones)
    if not any(n.startswith("LIM_") for n in bones_before):
        fail(where, "the arm has no limit dial to test with: %s" % bones_before)

    # The user poses the rig: by hand, or with a key at the current frame.
    arm_pb = bone_of(arm_obj, "g001")
    clip_pb = bone_of(arm_obj, "g002")
    for pb, angle in ((arm_pb, 0.5), (clip_pb, 0.35)):
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = (0.0, angle, 0.0)
        if keyed:
            pb.keyframe_insert("rotation_euler", index=1, frame=1)
            pb.rotation_euler = (0.0, 0.0, 0.0)
            pb.keyframe_insert("rotation_euler", index=1, frame=10)
            pb.rotation_euler = (0.0, angle, 0.0)
    scene.frame_set(1)
    bpy.context.view_layer.update()
    if off(parts()["c002"].matrix_world, sw_world(parts()["c002"], frame)) < 1e-3:
        fail(where, "posing the arm did not move it")

    # The edit in CAD, then Refresh Model.
    mesh, man = export(tmp, AFTER, 0.45)
    result = send(mesh, man, hierarchy, up_as, update=True, rig_mode=mode)
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    arm_obj = rig_object()
    if keyed and mode != "REGENERATE":
        # The animation is kept, and at a frame keyed to zero the rig is at
        # rest again.
        scene.frame_set(10)
        bpy.context.view_layer.update()
    check_on_sw_pose(where + " after the refresh", frame)

    bones_after = sorted(b.name for b in arm_obj.data.bones)
    doubled = [n for n in bones_after if "." in n.rsplit("_", 1)[-1]]
    if doubled or (mode != "REGENERATE" and bones_after != bones_before):
        fail(where, "the bones changed: before %s, after %s"
             % (bones_before, bones_after))

    if keyed and mode != "REGENERATE":
        # The key still poses the rig, and the parts go with it.
        scene.frame_set(1)
        bpy.context.view_layer.update()
        if off(parts()["c002"].matrix_world, sw_world(parts()["c002"], frame)) < 1e-3:
            fail(where, "the keyed pose did not come back")
        scene.frame_set(10)
        bpy.context.view_layer.update()
        if arm_obj.animation_data is not None:
            arm_obj.animation_data_clear()
        bpy.context.view_layer.update()
    check_carried(where, arm_obj, frame)
    return where


def on_rig(where, arm_obj):
    """Every part hangs on a bone of `arm_obj`, directly or through the
    empties that ride one."""
    for cid, obj in sorted(parts().items()):
        holder = obj
        while holder is not None and not (holder.parent is arm_obj
                                          and holder.parent_type == "BONE"):
            holder = holder.parent
        if holder is None:
            fail(where, "%s (%s) is off the rig" % (obj.name, cid))


def dead_rigs():
    out = []
    for arm in bpy.data.objects:
        if arm.type != "ARMATURE" or not arm.get("RIG_rig"):
            continue
        if not any(o.parent is arm and not o.get("RIG_rig")
                   and not o.get("RIG_helper") for o in bpy.data.objects):
            out.append(arm.name)
    return out


def run_without_relink(tmp, only=None):
    """Found 2026-09-23: Refresh took every part off the rig before the
    update, and only relink put them back. When relink did not run (Build
    rig off, a geometry-only refresh) or the job stopped early, the parts
    were left loose and the rig drove nothing."""
    from CADder.rig import rig_update
    cases = (
        ("build rig and relink off", {"build_rig": False, "relink": False}, None),
        ("geometry only", {}, "no manifest"),
        ("unreadable mesh", {}, "bad mesh"),
        ("rig update fails", {}, "rig update raises"),
    )
    done = []
    for name, steps, trouble in cases:
        if only is not None and name != only:
            continue
        where = "without relink: " + name
        bpy.ops.wm.read_factory_settings(use_empty=True)
        frame = Matrix([tuple(r) for r in native_import.up_frame("ZPOS")])
        mesh, man = export(tmp, BEFORE, 0.4)
        if not send(mesh, man, "FLAT", "ZPOS").get("ok"):
            fail(where, "the first send failed")
        arm_obj = rig_object()
        pb = bone_of(arm_obj, "g001")
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = (0.0, 0.5, 0.0)
        bpy.context.view_layer.update()

        mesh, man = export(tmp, AFTER, 0.45)
        if trouble == "no manifest":
            man = None
        if trouble == "bad mesh":
            with open(mesh, "wb") as fh:
                fh.write(b"not a mesh")
        real_apply = rig_update.apply
        if trouble == "rig update raises":
            def broken(*args, **kwargs):
                raise RuntimeError("simulated rig update failure")
            rig_update.apply = broken
        try:
            result = send(mesh, man, "FLAT", "ZPOS", update=True,
                          rig_mode="KEEP", steps=steps)
        finally:
            rig_update.apply = real_apply
        failing = trouble in ("bad mesh", "rig update raises")
        if bool(result.get("ok")) == failing:
            fail(where, "the refresh reported ok=%s: %s"
                 % (result.get("ok"), result.get("error")))
        on_rig(where, arm_obj)
        if trouble == "rig update raises":
            # released, put on the CAD pose, and bound again at rest
            check_on_sw_pose(where, frame)
            check_carried(where, arm_obj, frame)
        done.append(where)
    return done


def run_moved_rig(tmp, mode):
    """Found 2026-09-23: after the user moved the rig object to place the
    machine, Refresh put every part back at the CAD origin and bound it
    there, away from its bone."""
    where = "moved rig: " + mode
    bpy.ops.wm.read_factory_settings(use_empty=True)
    frame = Matrix([tuple(r) for r in native_import.up_frame("ZPOS")])
    mesh, man = export(tmp, BEFORE, 0.4)
    if not send(mesh, man, "FLAT", "ZPOS").get("ok"):
        fail(where, "the first send failed")
    arm_obj = rig_object()
    placed = Matrix.Translation((1.0, 2.0, 0.0)) @ Matrix.Rotation(0.3, 4, "Z")
    arm_obj.matrix_world = placed @ arm_obj.matrix_world
    bpy.context.view_layer.update()
    placed = arm_obj.matrix_world.copy()

    mesh, man = export(tmp, AFTER, 0.45)
    result = send(mesh, man, "FLAT", "ZPOS", update=True, rig_mode=mode)
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    arm_obj = rig_object()
    if off(arm_obj.matrix_world, placed) > TOL:
        fail(where, "the rig is no longer where the user placed it")
    for cid, obj in sorted(parts().items()):
        d = off(obj.matrix_world, placed @ sw_world(obj, frame))
        if d > TOL:
            fail(where, "%s (%s) is %.5f off its pose in the placed rig"
                 % (obj.name, cid, d))
    on_rig(where, arm_obj)
    check_carried(where, arm_obj, frame)
    return where


def run_other_assembly(tmp, mode):
    """Found 2026-09-23: a refresh of an assembly that is no longer in the
    scene released the rig of the one that is. The update then built the
    first one again, and the other rig was left behind, driving nothing."""
    where = "other assembly: " + mode
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh, man = export(tmp, BEFORE, 0.4)
    if not send(mesh, man, "FLAT", "ZPOS").get("ok"):
        fail(where, "the first send failed")
    other = [(cid, "o" + path, "o" + pid, x, g, "o" + name)
             for cid, path, pid, x, g, name in BEFORE]
    omesh, oman = export(tmp, other, 0.4, name="other")
    if not send(omesh, oman, "FLAT", "ZPOS").get("ok"):
        fail(where, "the second send failed")
    mesh, man = export(tmp, AFTER, 0.45)
    result = send(mesh, man, "FLAT", "ZPOS", update=True, rig_mode=mode)
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    dead = dead_rigs()
    if dead:
        fail(where, "rig(s) left driving nothing: %s" % dead)
    # The assembly refreshed hangs on its own rig, and on no other.
    for obj in bpy.data.objects:
        if obj.get("SWMESH_file") != "app" or not obj.get("RIG_component_id"):
            continue
        holder = obj
        while holder is not None and not (holder.parent is not None
                                          and holder.parent.type == "ARMATURE"
                                          and holder.parent_type == "BONE"):
            holder = holder.parent
        if holder is None:
            fail(where, "%s is off every rig" % obj.name)
        if holder.parent.name != "app_Rig":
            fail(where, "%s hangs on %s, the other assembly's rig"
                 % (obj.name, holder.parent.name))
    return where


def run_nla_tweak(tmp):
    """Found 2026-09-23: a refresh while an NLA strip of the rig was being
    tweaked failed, and left the rig's NLA switched off."""
    where = "nla tweak"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh, man = export(tmp, BEFORE, 0.4)
    if not send(mesh, man, "FLAT", "ZPOS").get("ok"):
        fail(where, "the first send failed")
    arm_obj = rig_object()
    pb = bone_of(arm_obj, "g001")
    pb.rotation_mode = "XYZ"
    pb.rotation_euler = (0.0, 0.3, 0.0)
    pb.keyframe_insert("rotation_euler", index=1, frame=1)
    anim = arm_obj.animation_data
    action = anim.action
    track = anim.nla_tracks.new()
    strip = track.strips.new("swing", 1, action)
    anim.action = None
    anim.nla_tracks.active = track
    strip.select = True
    track.select = True
    anim.use_tweak_mode = True
    if not anim.use_tweak_mode:
        fail(where, "the test could not enter NLA tweak mode")
    mesh, man = export(tmp, AFTER, 0.45)
    result = send(mesh, man, "FLAT", "ZPOS", update=True, rig_mode="KEEP")
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    if not arm_obj.animation_data.use_nla:
        fail(where, "the rig's NLA was left switched off")
    on_rig(where, arm_obj)
    return where


def main():
    tmp = os.path.join(tempfile.gettempdir(), "rig_refresh_pose_smoke")
    os.makedirs(tmp, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    done = []
    for hierarchy in native_import.HIERARCHIES:
        for mode in ("KEEP", "APPEND", "REGENERATE"):
            for keyed in (False, True):
                done.append(run(tmp, hierarchy, "ZPOS", mode, keyed))
    for mode in ("KEEP", "APPEND", "REGENERATE"):
        done.append(run(tmp, "FLAT", "YPOS", mode, False))
    done.extend(run_without_relink(tmp))
    for mode in ("KEEP", "APPEND", "REGENERATE"):
        done.append(run_moved_rig(tmp, mode))
        done.append(run_other_assembly(tmp, mode))
    done.append(run_nla_tweak(tmp))
    print("rig_refresh_pose_smoke: OK: %d refreshes of a posed rig put every "
          "part back on its SolidWorks pose and bound it there" % len(done))


main()
