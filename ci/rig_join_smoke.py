# SPDX-License-Identifier: GPL-3.0-or-later
"""Joining a subassembly's rig into a machine's rig.

The case that matters is the one that looks fine and is not: BOTH manifests
number their rigid groups from g000, so after a join two bones claim the
same id. Re-parenting keys on that id, so half the geometry would attach to
the other assembly's bones - roughly the right place, visibly wrong the
moment anything is posed. Both rigs here deliberately use the same ids and
the same bone names.

What is checked:

  * nothing moves - every bone and every part, before and after;
  * every part still rides ITS OWN bone, not the same-numbered bone of the
    other assembly;
  * the subassembly rides the bone it was attached to: posing that bone
    carries the whole subassembly, and posing the machine's own control
    does not;
  * the subassembly's own joint still works after the move;
  * couplings survive - a driver written against the joined armature is
    re-pointed at the survivor by Blender itself, and still drives;
  * a posed attach point is REFUSED rather than silently dragging the
    subassembly off its alignment.

    blender -b --factory-startup -P ci/rig_join_smoke.py
"""
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy
from mathutils import Euler, Matrix, Vector

bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT.rig import (graph, joining, manifest as mm,      # noqa: E402
                              parenting, rig_build)

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("   FAIL:", msg)
    return cond


def ident4():
    return [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def manifest(step_file, at, coupled=False):
    """Ground plus two hinges about Z, the second geared off the first when
    `coupled`. Group ids are g000..g002 in BOTH manifests, on purpose."""
    def comp(cid, name, x):
        t = ident4()
        t[0][3] = at + x
        return {"id": cid, "sw_path": name + "-1", "step_name": name,
                "step_occurrence_path": None, "transform": t}

    joints = [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [at + 0.1, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
        {"id": "j002", "type": "revolute", "parent_group": "g000",
         "child_group": "g002", "origin": [at + 0.2, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
    ]
    if coupled:
        joints[1]["coupling"] = {"kind": "gear", "driver_joint": "j001",
                                 "ratio": -2.0}
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "rig_join_smoke", "version": "1"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": step_file, "ap": "AP214", "sha1": None,
                        "occurrence_matching": None},
        "components": [comp("c001", "base", 0.0), comp("c002", "link", 0.1),
                       comp("c003", "lever", 0.2)],
        "rigid_groups": [
            {"id": "g000", "name": "base", "components": ["c001"],
             "grounded": True, "frame": None, "bbox_diag": 0.2},
            {"id": "g001", "name": "link", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": 0.1},
            {"id": "g002", "name": "lever", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.1},
        ],
        "joints": joints, "loops": [], "warnings": [],
    }


def build(name, at, coupled=False):
    source = name + ".rig.json"
    man = mm.parse(manifest(name + ".step", at, coupled), source_path=source)
    plan = graph.build(man)
    result = rig_build.build(bpy.context, man, plan)
    result.armature_object.name = name + "_Rig"
    # One part per group, tagged the way matching would have tagged it, and
    # sitting where its bone is so a drift is visible as a drift.
    parts = {}
    for gid, bone_name in result.bone_names.items():
        mesh = bpy.data.meshes.new("%s_%s" % (name, gid))
        mesh.from_pydata([(0, 0, 0), (0.02, 0, 0), (0, 0.02, 0)], [],
                         [(0, 1, 2)])
        obj = bpy.data.objects.new("%s_%s_part" % (name, gid), mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj["RIG_group"] = gid
        obj["RIG_source"] = source
        obj["STEP_file"] = name + ".step"
        pb = result.armature_object.pose.bones[bone_name]
        # OFF the bone, not on it: a revolute turns a part about its own
        # origin, so a part sitting exactly on its bone can turn through a
        # right angle without its origin moving at all.
        obj.matrix_world = ((result.armature_object.matrix_world @ pb.matrix)
                            @ Matrix.Translation((0.03, 0.0, 0.0)))
        parts[gid] = obj
    parenting.relink(bpy.context, result.armature_object)
    return result, parts, source


def world_of(objs):
    bpy.context.view_layer.update()
    return {o.name: o.matrix_world.translation.copy() for o in objs}


print("-- building two rigs whose group ids collide")
machine, machine_parts, machine_src = build("machine", 0.0)
sub, sub_parts, sub_src = build("gripper", 1.0, coupled=True)
sub_arm = sub.armature_object
machine_arm = machine.armature_object

check(sorted(machine.bone_names) == sorted(sub.bone_names),
      "the two rigs were supposed to share group ids")
print("   group ids in both:", sorted(machine.bone_names))
print("   bone names: machine %s | gripper %s"
      % (sorted(machine.bone_names.values()), sorted(sub.bone_names.values())))

# The user aligns the subassembly by moving its ARMATURE, which is an object
# transform and nothing to do with pose.
sub_arm.location = (0.35, 0.12, 0.08)
sub_arm.rotation_euler = Euler((0.0, 0.0, 0.6), "XYZ")
bpy.context.view_layer.update()
for obj in sub_parts.values():
    pass    # the parts ride the bones, so the move carries them

attach = machine.bone_names["g001"]     # the machine's own moving link
all_parts = list(machine_parts.values()) + list(sub_parts.values())
before = world_of(all_parts)

# ---- a posed attach point must be refused, not silently obeyed ------------
posed = machine_arm.pose.bones[attach]
posed.rotation_mode = "XYZ"
posed.rotation_euler[1] = 0.4
bpy.context.view_layer.update()
refused = joining.join(bpy.context, machine_arm, [sub_arm], attach_bone=attach,
                       relink=False)
check(not refused.attached_to and any("rest pose" in w
                                      for w in refused.warnings),
      "a posed attach bone should have been refused: %s" % refused.warnings)
print("   posed attach refused:", refused.warnings[:1])

print("\n-- rebuilding and joining at rest")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
machine, machine_parts, machine_src = build("machine", 0.0)
sub, sub_parts, sub_src = build("gripper", 1.0, coupled=True)
sub_arm, machine_arm = sub.armature_object, machine.armature_object
sub_arm.location = (0.35, 0.12, 0.08)
sub_arm.rotation_euler = Euler((0.0, 0.0, 0.6), "XYZ")
attach = machine.bone_names["g001"]
all_parts = list(machine_parts.values()) + list(sub_parts.values())
before = world_of(all_parts)

report = joining.join(bpy.context, machine_arm, [sub_arm], attach_bone=attach)
print("   bones added %d, attached to %s, re-parented %d, renamed %d"
      % (report.bones_added, report.attached_to, report.reparented,
         len(report.renamed)))
for w in report.warnings:
    print("   warning:", w)

check(not report.drift, "things moved during the join: %s" % report.drift[:4])
check(report.bones_added >= 3, "only %d bone(s) arrived" % report.bones_added)
check(report.attached_to == attach, "attached to %r" % report.attached_to)
check("gripper_Rig" not in bpy.data.objects,
      "the joined armature should have been consumed")
check(report.renamed, "the bone names collide, so some should be renamed")

after = world_of(all_parts)
for name, was in sorted(before.items()):
    d = (after[name] - was).length
    check(d < 1e-6, "%s moved %.6f m during the join" % (name, d))

# ---- every part rides ITS OWN bone ---------------------------------------
print("\n-- who rides what")
for label, parts, source in (("machine", machine_parts, machine_src),
                             ("gripper", sub_parts, sub_src)):
    for gid, obj in sorted(parts.items()):
        pb = machine_arm.pose.bones.get(obj.parent_bone)
        ok = (obj.parent is machine_arm and pb is not None
              and pb.get("RIG_group") == gid
              and (pb.get("RIG_source") or None) == source)
        check(ok, "%s %s rides %s (group %s, source %s) instead of its own"
              % (label, gid, obj.parent_bone,
                 pb.get("RIG_group") if pb else None,
                 pb.get("RIG_source") if pb else None))
        print("   %-8s %-5s -> %-22s group=%s" % (label, gid, obj.parent_bone,
                                                  pb.get("RIG_group") if pb else "?"))

# ---- the subassembly rides the bone it was hung off ----------------------
print("\n-- posing")


def move_under(bone, angle):
    pb = machine_arm.pose.bones[bone]
    pb.rotation_mode = "XYZ"
    base = world_of(all_parts)
    pb.rotation_euler[1] = angle
    bpy.context.view_layer.update()
    now = world_of(all_parts)
    pb.rotation_euler[1] = 0.0
    bpy.context.view_layer.update()
    return {n: (now[n] - base[n]).length for n in base}


moved = move_under(attach, 0.5)
carried = [n for n, d in moved.items() if d > 1e-5]
print("   posing the attach bone %s moves: %s" % (attach, sorted(carried)))
for gid, obj in sub_parts.items():
    check(moved[obj.name] > 1e-5,
          "gripper %s does not follow the bone it was attached to" % gid)
check(moved[machine_parts["g002"].name] < 1e-9,
      "the machine's other link should not follow that bone")

# The subassembly's own joint still articulates, and only its own parts move.
sub_link_bone = None
for pb in machine_arm.pose.bones:
    if (pb.get("RIG_source") or None) == sub_src and pb.get("RIG_group") == "g001":
        sub_link_bone = pb.name
check(sub_link_bone is not None, "the gripper's own link bone is gone")
if sub_link_bone:
    moved = move_under(sub_link_bone, 0.4)
    check(moved[sub_parts["g001"].name] > 1e-5,
          "the gripper's own joint no longer articulates")
    for gid, obj in machine_parts.items():
        check(moved[obj.name] < 1e-9,
              "machine %s moved when the gripper's own joint was posed" % gid)
    print("   posing the gripper's own link moves only its own part")

# ---- the coupling survived the join --------------------------------------
drivers = []
if machine_arm.animation_data:
    drivers = [fc.data_path for fc in machine_arm.animation_data.drivers]
check(drivers, "the gear coupling's driver did not survive the join")
print("\n   drivers on the merged rig:", drivers)
if sub_link_bone:
    driven = None
    for pb in machine_arm.pose.bones:
        if (pb.get("RIG_source") or None) == sub_src and pb.get("RIG_group") == "g002":
            driven = pb
    if check(driven is not None, "the gripper's driven bone is gone"):
        pb = machine_arm.pose.bones[sub_link_bone]
        pb.rotation_mode = "XYZ"
        pb.rotation_euler[1] = 0.3
        bpy.context.view_layer.update()
        check(abs(driven.rotation_euler[1] - (-2.0 * 0.3)) < 1e-6,
              "the gear coupling reads %.4f, wanted %.4f"
              % (driven.rotation_euler[1], -0.6))
        print("   the gear coupling still drives: %.4f rad from %.4f"
              % (driven.rotation_euler[1], 0.3))
        pb.rotation_euler[1] = 0.0
        bpy.context.view_layer.update()

# ---- a THIRD rig, onto the machine ground, with the first two undisturbed
print("\n-- joining a third rig, this time onto the ground (no bone named)")
third, third_parts, third_src = build("turret", 2.0)
third_arm = third.armature_object
third_arm.location = (-0.4, 0.3, 0.0)

settled = world_of(all_parts)
report3 = joining.join(bpy.context, machine_arm, [third_arm], attach_bone=None)
print("   bones added %d, attached to %r, re-parented %d"
      % (report3.bones_added, report3.attached_to, report3.reparented))
for w in report3.warnings:
    print("   warning:", w)

check(not report3.drift, "the third join moved things: %s" % report3.drift[:4])
check(report3.attached_to == machine.bone_names["g000"],
      "with no bone named it should land on the machine ground, not %r"
      % report3.attached_to)
check("turret_Rig" not in bpy.data.objects, "the third armature survived")

# The two rigs already in there must not have shifted or changed hands.
now = world_of(all_parts)
for name, was in sorted(settled.items()):
    d = (now[name] - was).length
    check(d < 1e-6, "%s moved %.6f m when a third rig was joined" % (name, d))

all_parts = all_parts + list(third_parts.values())
by_source = {}
for label, parts, source in (("machine", machine_parts, machine_src),
                             ("gripper", sub_parts, sub_src),
                             ("turret", third_parts, third_src)):
    for gid, obj in sorted(parts.items()):
        pb = machine_arm.pose.bones.get(obj.parent_bone)
        ok = (obj.parent is machine_arm and pb is not None
              and pb.get("RIG_group") == gid
              and (pb.get("RIG_source") or None) == source)
        check(ok, "after three joins, %s %s rides %s"
              % (label, gid, obj.parent_bone))
        by_source.setdefault(source, []).append(obj.parent_bone)
check(len(by_source) == 3, "three manifests should be represented, got %d"
      % len(by_source))
print("   three manifests in one armature: %s"
      % {k.split(".")[0]: len(v) for k, v in sorted(by_source.items())})

# Posing the machine's own link still carries the gripper and NOT the turret.
moved = move_under(attach, 0.5)
for gid, obj in sub_parts.items():
    check(moved[obj.name] > 1e-5, "gripper %s stopped following after the "
          "third join" % gid)
for gid, obj in third_parts.items():
    check(moved[obj.name] < 1e-9,
          "turret %s follows a bone it was never attached to" % gid)
print("   the gripper still rides the link; the turret does not")

print()
if FAILS:
    print("rig_join_smoke: %d FAILURE(S)" % len(FAILS))
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("rig_join_smoke: OK - two rigs with colliding ids joined, nothing "
      "moved, every part on its own bone, and the subassembly rides its "
      "attach point")
