# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a rig rebuilt inside its own armature (rig mode
APPEND): what the user added or keyed must still be there after it.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_rebuild_smoke.py

Each case sends an assembly, changes the rig the way a user does, sends
an update with rig_mode APPEND and checks what came back:

  * a limited ball keeps the name of the handle the user keys, update
    after update.
  * a bone the user parented to a rig bone hangs on that bone again.
  * a rig the user moved keeps its path rails and cam surfaces on its
    bones.
  * a rig joined from two assemblies, built again for one of them, keeps
    the other one's bones, drivers and parts.
  * a driver the user put on the armature object stays.
  * rebuilds leave no rail or cam meshes behind, and do not move their
    names on.
"""

import json
import math
import os
import struct
import sys
import tempfile

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import (graph, joining, manifest as man_mod,  # noqa: E402
                        parenting, rig_build, rig_update)
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


_REGISTERED = []


def fresh():
    """An empty scene for the next case. The rig classes survive a
    factory reset, so they are registered once."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not _REGISTERED:
        rig.register()
        _REGISTERED.append(True)
    rig_ui._reset_state()


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


# ── A bone of the user's own stays on the bone it hung on ───────────────

HINGED = [("c001", "base-1", "pbase", 0.0, "base"),
          ("c002", "arm-1", "parm", 0.2, "arm")]


def user_bone_keeps_its_parent():
    print("-- a bone of the user's own, parented to a rig bone")
    fresh()
    joints = [hinge("j001", "g000", "g001", 0.2)]
    send("armrig", HINGED, joints)
    arm = rig_object()
    if not check(arm is not None, "no rig was built"):
        return
    arm_bone = rig_ui._STATE["build"].bone_names["g001"]
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    mine = arm.data.edit_bones.new("cam_target")
    mine.head = (0.4, 0.0, 0.1)
    mine.tail = (0.4, 0.0, 0.2)
    mine.parent = arm.data.edit_bones[arm_bone]
    bpy.ops.object.mode_set(mode="OBJECT")

    send("armrig", HINGED, joints, update=True)
    now = rig_object()
    bone = now.data.bones.get("cam_target")
    if not check(bone is not None, "the user's bone was removed"):
        return
    check(bone.parent is not None and bone.parent.name == arm_bone,
          "the user's bone hangs on %r, not on %r"
          % (bone.parent.name if bone.parent else None, arm_bone))
    check((bone.head_local - Vector((0.4, 0.0, 0.1))).length < 1e-6,
          "the user's bone moved to %s" % (tuple(bone.head_local),))
    print("   cam_target hangs on", bone.parent.name if bone.parent else None)


# ── A rig the user moved keeps its rails with its bones ─────────────────

PATH_PARTS = [("c001", "base-1", "pbase", 0.0, "base"),
              ("c002", "arm-1", "parm", 0.2, "arm"),
              ("c003", "shuttle-1", "pshuttle", 0.35, "shuttle")]


def path_joint(jid, parent, child):
    return {"id": jid, "type": "path", "parent_group": parent,
            "child_group": child, "origin": [0.35, 0, 0], "axis": [1, 0, 0],
            "secondary_axis": [0, 0, 1], "limits": None,
            "path": {"points": [[0.3, 0, 0], [0.35, 0, 0], [0.4, 0.01, 0]],
                     "closed": False}}


def evaluated_head(arm, name):
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    return arm.matrix_world @ arm.evaluated_get(dg).pose.bones[name].head


def moved_rig_keeps_its_rails():
    print("-- a rig moved by the user, then updated")
    fresh()
    joints = [hinge("j001", "g000", "g001", 0.2),
              path_joint("j002", "g000", "g002")]
    send("railrig", PATH_PARTS, joints)
    arm = rig_object()
    if not check(arm is not None, "no rig was built"):
        return
    arm.location = (0.5, 0.0, 0.0)
    bpy.context.view_layer.update()

    send("railrig", PATH_PARTS, joints, update=True)
    now = rig_object()
    check(now is arm, "the armature object was replaced")
    build = rig_ui._STATE["build"]
    shuttle = build.bone_names["g002"]
    hinge_bone = build.bone_names["g001"]

    # Bones, parts and rails all stand where the user put the machine.
    rest = now.matrix_world @ now.data.bones[shuttle].head_local
    check((rest - Vector((0.85, 0.0, 0.0))).length < 1e-5,
          "the shuttle's bone rests at %s, not at the moved CAD place"
          % (tuple(rest),))
    part = bpy.data.objects.get("arm")
    if check(part is not None, "the arm part is gone"):
        pivot = now.matrix_world @ now.data.bones[hinge_bone].head_local
        check((part.matrix_world.translation - pivot).length < 1e-5,
              "the arm part is %.4f m from its pivot"
              % (part.matrix_world.translation - pivot).length)
    rail = bpy.data.objects.get(build.contact_mesh_names.get("j002", ""))
    if check(rail is not None, "the path joint has no rail"):
        bpy.context.view_layer.update()
        nearest = min((rail.matrix_world @ v.co - rest).length
                      for v in rail.data.vertices)
        check(nearest < 1e-4,
              "the rail is %.4f m from the shuttle's bone" % nearest)
    drift = (evaluated_head(now, shuttle) - rest).length
    check(drift < 1e-5,
          "the rail pulled the resting shuttle %.4f m" % drift)


def cam_manifest():
    """A disc cam on a hinge about Z and a follower sliding along X onto
    its rim: a circle of radius 0.03 about (0.01, 0, 0)."""
    ring = [(0.01 + 0.03 * math.cos(math.pi * k / 90),
             0.03 * math.sin(math.pi * k / 90)) for k in range(180)]
    pts, tris = [], []
    for x, y in ring:
        pts += [[x, y, -0.01], [x, y, 0.01]]
    for i in range(len(ring)):
        j = (i + 1) % len(ring)
        tris += [[2 * i, 2 * j, 2 * j + 1], [2 * i, 2 * j + 1, 2 * i + 1]]
    parts = [("c001", "frame-1", "pframe", 0.0, "frame"),
             ("c002", "cam-1", "pcam", 0.0, "cam"),
             ("c003", "tappet-1", "ptappet", 0.04, "tappet")]
    data = manifest("camrig", parts, [
        hinge("j001", "g000", "g001", 0.0),
        {"id": "j002", "type": "prismatic", "parent_group": "g000",
         "child_group": "g002", "origin": [0.04, 0, 0], "axis": [1, 0, 0],
         "secondary_axis": [0, 0, 1], "limits": None,
         "coupling": {"kind": "cam", "driver_joint": "j001",
                      "cam": {"axis": [0, 0, 1], "origin": [0, 0, 0],
                              "surface": {"points": pts, "triangles": tris},
                              "follower": {"kind": "vertex",
                                           "point": [0.04, 0, 0],
                                           "axis": None, "radius": None,
                                           "normal": None}}}},
    ])
    return data


def moved_rig_keeps_its_cam():
    print("-- a cam rig moved by the user, then built again inside")
    fresh()
    m = man_mod.parse(cam_manifest(), source_path="camrig.rig.json")
    first = rig_build.build(bpy.context, m, graph.build(m))
    arm = first.armature_object
    arm.location = (0.5, 0.0, 0.0)
    bpy.context.view_layer.update()

    again = rig_build.build(bpy.context, m, graph.build(m), into=arm)
    bpy.context.view_layer.update()
    surface = bpy.data.objects.get(again.cam_surface_names.get("j002", ""))
    if not check(surface is not None, "the cam has no surface"):
        return
    xs = [(surface.matrix_world @ v.co).x for v in surface.data.vertices]
    centre = (min(xs) + max(xs)) / 2.0
    check(abs(centre - 0.51) < 1e-4,
          "the cam surface is centered at x %.4f, not at the moved cam 0.51"
          % centre)
    follower = again.bone_names["g002"]
    head = evaluated_head(arm, follower)
    check(abs(head.x - 0.54) < 1e-4,
          "the resting follower stands at x %.4f, not on the cam at 0.54"
          % head.x)


# ── A joined rig built again for one of its assemblies ──────────────────

def joined_manifest(stem, at, coupled=False):
    """A ground and two hinges. Both assemblies number their groups from
    g000 and name their bones alike, as rig_join_smoke's do."""
    parts = [("c001", "base-1", "p" + stem + "base", at, "base"),
             ("c002", "link-1", "p" + stem + "link", at + 0.1, "link"),
             ("c003", "lever-1", "p" + stem + "lever", at + 0.2, "lever")]
    joints = [hinge("j001", "g000", "g001", at + 0.1),
              hinge("j002", "g000", "g002", at + 0.2)]
    # A limit gives the first hinge a dial of its own: a bone with no
    # group that belongs to this assembly all the same.
    joints[0]["limits"] = {"rotation": {"min": -1.0, "max": 1.0,
                                        "value_at_rest": 0.0},
                           "translation": None}
    if coupled:
        joints[1]["coupling"] = {"kind": "gear", "driver_joint": "j001",
                                 "ratio": -2.0}
    source = stem + ".rig.json"
    return man_mod.parse(manifest(stem, parts, joints), source_path=source)


def built_with_parts(m):
    result = rig_build.build(bpy.context, m, graph.build(m))
    arm = result.armature_object
    parts = {}
    for gid, bone_name in result.bone_names.items():
        mesh = bpy.data.meshes.new("%s_%s" % (arm.name, gid))
        mesh.from_pydata([(0, 0, 0), (0.02, 0, 0), (0, 0.02, 0)], [],
                         [(0, 1, 2)])
        obj = bpy.data.objects.new("%s_%s_part" % (arm.name, gid), mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj["RIG_group"] = gid
        obj["RIG_source"] = m.source_path
        obj.matrix_world = (arm.matrix_world
                            @ arm.pose.bones[bone_name].matrix
                            @ Matrix.Translation((0.03, 0.0, 0.0)))
        parts[gid] = obj
    parenting.relink(bpy.context, arm)
    return result, parts


def bones_of(arm, source):
    return sorted(pb.name for pb in arm.pose.bones
                  if pb.get("RIG_source") == source and pb.get("RIG_group"))


def joined_rig_keeps_the_other_assembly():
    print("-- a joined rig, built again for the machine alone")
    fresh()
    machine_m = joined_manifest("machine", 0.0)
    gripper_m = joined_manifest("gripper", 1.0, coupled=True)
    machine, machine_parts = built_with_parts(machine_m)
    gripper, gripper_parts = built_with_parts(gripper_m)
    host = machine.armature_object
    sub = gripper.armature_object
    sub.location = (0.35, 0.12, 0.08)
    bpy.context.view_layer.update()
    # A rig built before every bone said which assembly it came from.
    for pb in sub.pose.bones:
        if not pb.get("RIG_group") and "RIG_source" in pb.keys():
            del pb["RIG_source"]

    attach = machine.bone_names["g001"]
    report = joining.join(bpy.context, host, [sub], attach_bone=attach)
    check(report.attached_to == attach, "the join did not attach the gripper")
    gripper_bones = bones_of(host, gripper_m.source_path)
    gripper_count = sum(1 for pb in host.pose.bones
                        if pb.name not in {b.name for b in host.pose.bones
                                           if b.get("RIG_source")
                                           == machine_m.source_path})
    gripper_root = [pb.name for pb in host.pose.bones
                    if pb.get("RIG_source") == gripper_m.source_path
                    and pb.get("RIG_group") == "g000"][0]
    parts = list(machine_parts.values()) + list(gripper_parts.values())
    bpy.context.view_layer.update()
    before = {o.name: o.matrix_world.translation.copy() for o in parts}

    rig_update.release(bpy.context, host)
    rig_build.build(bpy.context, machine_m, graph.build(machine_m), into=host)
    parenting.relink(bpy.context, host)
    bpy.context.view_layer.update()

    check(bones_of(host, gripper_m.source_path) == gripper_bones,
          "the gripper's bones are %s, were %s"
          % (bones_of(host, gripper_m.source_path), gripper_bones))
    check(bones_of(host, machine_m.source_path)
          == sorted(machine.bone_names.values()),
          "the machine's bones are %s" % bones_of(host, machine_m.source_path))
    left = sum(1 for pb in host.pose.bones
               if pb.get("RIG_source") != machine_m.source_path)
    check(left == gripper_count,
          "%d bone(s) of the gripper are left, was %d" % (left, gripper_count))
    root = host.data.bones.get(gripper_root)
    check(root is not None and root.parent is not None
          and root.parent.name == attach,
          "the gripper hangs on %r, not on %r"
          % (root.parent.name if root and root.parent else None, attach))
    driven = [fc.data_path for fc in host.animation_data.drivers] \
        if host.animation_data else []
    check(any(gripper.bone_names["g002"] in p or "lever.001" in p
              for p in driven),
          "the gripper's gear lost its driver: %s" % driven)
    for obj in gripper_parts.values():
        pb = host.pose.bones.get(obj.parent_bone)
        check(obj.parent is host and pb is not None
              and pb.get("RIG_source") == gripper_m.source_path,
              "%s rides %r, not a bone of its own assembly"
              % (obj.name, obj.parent_bone))
    for obj in parts:
        d = (obj.matrix_world.translation - before[obj.name]).length
        check(d < 1e-5, "%s moved %.6f m" % (obj.name, d))
    print("   gripper bones kept:", bones_of(host, gripper_m.source_path))

    # An assembly the joined rig does not hold: nothing says which bones
    # are its own, so the rig is left alone.
    stranger = joined_manifest("stranger", 2.0)
    count = len(host.data.bones)
    try:
        rig_build.build(bpy.context, stranger, graph.build(stranger),
                        into=host)
        refused = False
    except ValueError as exc:
        refused = True
        print("   refused:", exc)
    check(refused, "a joined rig was rebuilt for an assembly it does not hold")
    check(len(host.data.bones) == count,
          "the refused rebuild changed the bones: %d, was %d"
          % (len(host.data.bones), count))


# ── The user's drivers on the armature object stay ──────────────────────

def object_drivers_stay():
    print("-- drivers the user put on the armature object")
    fresh()
    joints = [hinge("j001", "g000", "g001", 0.2)]
    send("drvrig", HINGED, joints)
    arm = rig_object()
    if not check(arm is not None, "no rig was built"):
        return
    arm["my_prop"] = 0.0
    arm.driver_add('["my_prop"]').driver.expression = "frame / 10"
    arm.driver_add("rotation_euler", 2).driver.expression = "0.1"
    ours = len(arm.animation_data.drivers) - 2

    send("drvrig", HINGED, joints, update=True)
    now = rig_object()
    paths = sorted((fc.data_path, fc.array_index)
                   for fc in now.animation_data.drivers) \
        if now.animation_data else []
    check(('["my_prop"]', 0) in paths,
          "the driver on the custom property is gone: %s" % paths)
    check(("rotation_euler", 2) in paths,
          "the driver on the rotation is gone: %s" % paths)
    check(len(paths) == ours + 2,
          "%d driver(s) after the update, was %d" % (len(paths), ours + 2))


# ── Rebuilds leave no meshes behind ─────────────────────────────────────

def rebuilds_leave_no_meshes():
    print("-- a rig with a rail and a cam, built again and again")
    fresh()
    rail_m = man_mod.parse(
        manifest("meshrig", PATH_PARTS,
                 [hinge("j001", "g000", "g001", 0.2),
                  path_joint("j002", "g000", "g002")]),
        source_path="meshrig.rig.json")
    cam_m = man_mod.parse(cam_manifest(), source_path="camrig.rig.json")
    for m in (rail_m, cam_m):
        result = rig_build.build(bpy.context, m, graph.build(m))
        for _ in range(3):
            result = rig_build.build(bpy.context, m, graph.build(m))
        for _ in range(3):
            result = rig_build.build(bpy.context, m, graph.build(m),
                                     into=result.armature_object)
        names = (list(result.contact_mesh_names.values())
                 + list(result.cam_surface_names.values()))
        check(names and all("." not in n for n in names),
              "the helper objects of %s moved on to %s" % (m.step_file, names))
    orphans = sorted(me.name for me in bpy.data.meshes
                     if me.users == 0 and me.name.startswith("CADLINK_"))
    check(not orphans, "rebuilds left %d mesh(es) with no user: %s"
          % (len(orphans), orphans))


def main():
    ball_handle_keeps_its_name()
    object_drivers_stay()
    rebuilds_leave_no_meshes()
    user_bone_keeps_its_parent()
    moved_rig_keeps_its_rails()
    moved_rig_keeps_its_cam()
    joined_rig_keeps_the_other_assembly()

    print()
    if FAILS:
        print("rig_rebuild_smoke: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("rig_rebuild_smoke: OK")


main()
