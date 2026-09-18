# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a universal joint built from its parts, in Blender.

Builds universal joint 2 (pins locked, and pins free) from its live
manifests, turns the input yoke through two turns each way, and measures
the EVALUATED pose: the cross's arm on each side has to stay on that
side's yoke pin, and the output yoke has to turn at a universal joint's
speed, between cos(bend) and 1/cos(bend) of the input's.

It also counts the keys of the table drive. Keyed in radians, a table
sampled every half degree lost half its keys to Blender's merge of keys
closer than 0.01, and the output ran a step ahead of the input (the cross
came 0.5 degrees off its pin, 2026-09-18).

Run:  blender -b --factory-startup -P rig_hooke_smoke.py
"""

import math
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, hooke, manifest as man_mod, rig_build  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = [os.path.join(HERE, "rig", "fixtures", name) for name in (
    "universal_joint_2_pins_locked.rig.json",
    "universal_joint_2_pins_free.rig.json")]


def _check(ok, message):
    if not ok:
        print("rig_hooke_smoke: FAIL: " + message)
        sys.exit(1)


def _group(m, word):
    names = {c.id: c.sw_path for c in m.components}
    for g in m.rigid_groups:
        if any(word in names[c] for c in g.components):
            return g.id
    _check(False, "no group holds " + word)


def run(path):
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    m = man_mod.load(path)
    plan = graph.build(m)
    _check(plan.hooke_notes, os.path.basename(path) + ": not recognised")
    _check(not plan.loops, os.path.basename(path) + ": a loop still has IK")
    res = rig_build.build(bpy.context, m, plan)
    arm = res.armature_object
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")

    female, male, cross = (_group(m, w) for w in ("yoke_female", "yoke_male", "spider"))
    pose = {g: arm.pose.bones[res.bone_names[g]] for g in (female, male, cross)}
    rest = {g: pb.bone.matrix_local.to_3x3().inverted() for g, pb in pose.items()}
    arm_d = [j for j in m.joints if {j.parent_group, j.child_group} == {female, cross}][0]
    arm_o = [j for j in m.joints if {j.parent_group, j.child_group} == {male, cross}][0]
    e_d, e_o = Vector(arm_d.axis).normalized(), Vector(arm_o.axis).normalized()
    shaft_f = [j for j in m.joints if j.child_group == female][0]
    shaft_m = [j for j in m.joints if j.child_group == male][0]
    a_m = Vector(shaft_m.axis).normalized()
    bend = Vector(shaft_f.axis).angle(a_m)
    bend = min(bend, math.pi - bend)

    # Every sample of every table survived as a key.
    for fc in arm.animation_data.drivers:
        if fc.keyframe_points:
            _check(len(fc.keyframe_points) == hooke.SAMPLES + 1,
                   "a table drive has %d keys, not %d" % (
                       len(fc.keyframe_points), hooke.SAMPLES + 1))

    def turn_of(g, axis):
        r = pose[g].matrix.to_3x3() @ rest[g]
        q = r.to_quaternion()
        return 2.0 * math.atan2(Vector(q[1:]).dot(axis), q[0])

    step = math.radians(0.5)
    worst = 0.0
    speeds = []
    previous = None
    for i in range(-1440, 1441):
        e = pose[female].rotation_euler.copy()
        e[1] = i * step
        pose[female].rotation_euler = e
        bpy.context.view_layer.update()
        rf = pose[female].matrix.to_3x3() @ rest[female]
        rm = pose[male].matrix.to_3x3() @ rest[male]
        rc = pose[cross].matrix.to_3x3() @ rest[cross]
        worst = max(worst, math.degrees((rc @ e_d).angle(rf @ e_d)),
                    math.degrees((rc @ e_o).angle(rm @ e_o)))
        out = turn_of(male, a_m)
        if previous is not None:
            d = (out - previous + math.pi) % (2 * math.pi) - math.pi
            speeds.append(abs(d) / step)
        previous = out
    name = os.path.basename(path)
    _check(worst < 0.01, "%s: the cross came %.4f deg off a pin" % (name, worst))
    _check(min(speeds) > math.cos(bend) - 0.01 and max(speeds) < 1 / math.cos(bend) + 0.01,
           "%s: output speed %.3f to %.3f, not a %.1f deg universal joint's %.3f to %.3f" % (
               name, min(speeds), max(speeds), math.degrees(bend),
               math.cos(bend), 1 / math.cos(bend)))
    bpy.ops.object.mode_set(mode="OBJECT")
    return worst, min(speeds), max(speeds), math.degrees(bend)


results = [run(p) for p in FIXTURES]
print("rig_hooke_smoke: OK: universal joint 2 with its pins locked and with "
      "them free, two turns each way: the cross stays on both pins to "
      "%.4f deg, and the output turns at %.3f to %.3f of the input, as a "
      "%.1f deg universal joint does" % (
          max(r[0] for r in results), min(r[1] for r in results),
          max(r[2] for r in results), results[0][3]))
