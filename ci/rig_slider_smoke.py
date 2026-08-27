# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for slider-crank closures: a hydraulic ram working a clamp.

    blender -b --factory-startup -P ci/rig_slider_smoke.py

The mechanism is the one live ClampRig has twice over, reduced to
numbers that can be checked by hand. Seen down the pin axis (+Z):

    B = (0,0,0)   the clamp's pivot on the body      <- the input
    A = (1,0,0)   the ram's bore pivot on the body
    C = (0,1,0)   the rod's pin on the clamp

|AB| = |BC| = 1, so the ram |AC| is sqrt(2) at rest and the corner at B is a
right angle. Rotating the clamp about B changes |AC|, which is exactly what
extending or retracting the ram does.

What this asserts is the whole point of the aim pair: after the clamp is
posed, each half of the ram still POINTS AT the other's pivot, and the gap
between them has changed. Blender's IK cannot do that — a slide inside a
chain can only be locked — so before this closure existed the ram bones sat
frozen while the clamp swung away from them.
"""

import json
import math
import os
import sys
import tempfile
import traceback

_ADDON_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT2 = math.sqrt(2.0)


def _identity4():
    return [[1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def _check(cond, message):
    if not cond:
        raise AssertionError(message)


def _ram_manifest():
    def comp(cid, name):
        return {"id": cid, "sw_path": name + "-1", "step_name": name,
                "step_occurrence_path": "Ram/" + name + "-1",
                "transform": _identity4()}

    def group(gid, name, cid, grounded):
        return {"id": gid, "name": name, "components": [cid],
                "grounded": grounded,
                "frame": _identity4() if grounded else None,
                "bbox_diag": 0.4}

    pin = [0.0, 0.0, 1.0]
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "ci", "version": "0", "solidworks_version": "0",
                      "exported_utc": "2026-08-24T00:00:00Z"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "ram.step", "ap": "AP214", "sha1": "0" * 40,
                        "occurrence_matching": "peak-nauo-tree/1"},
        "components": [comp("c001", "Body"), comp("c002", "Clamp"),
                       comp("c003", "Barrel"), comp("c004", "Rod")],
        "rigid_groups": [group("g000", "body", "c001", True),
                         group("g001", "clamp", "c002", False),
                         group("g002", "barrel", "c003", False),
                         group("g003", "rod", "c004", False)],
        "joints": [
            {"id": "j001", "type": "revolute",
             "parent_group": "g000", "child_group": "g001",
             "origin": [0.0, 0.0, 0.0], "axis": pin,
             "secondary_axis": [1.0, 0.0, 0.0],
             # The derived limit: |AC| may run from 1 to sqrt(3), which opens
             # the corner at B from 60 to 120 degrees, so +/- 30 from rest.
             "limits": {"rotation": {"min": -math.pi / 6.0,
                                     "max": math.pi / 6.0,
                                     "value_at_rest": 0.0},
                        "translation": None}},
            {"id": "j002", "type": "revolute",
             "parent_group": "g000", "child_group": "g002",
             "origin": [1.0, 0.0, 0.0], "axis": pin,
             "secondary_axis": [1.0, 0.0, 0.0], "limits": None},
            {"id": "j003", "type": "prismatic",
             "parent_group": "g002", "child_group": "g003",
             "origin": [0.5, 0.5, 0.0],
             "axis": [-1.0 / _ROOT2, 1.0 / _ROOT2, 0.0],
             "secondary_axis": [0.0, 0.0, 1.0],
             "limits": {"rotation": None,
                        "translation": {"min": 0.5 + (1.0 - _ROOT2),
                                        "max": 0.5 + (math.sqrt(3.0) - _ROOT2),
                                        "value_at_rest": 0.5}}},
            {"id": "j004", "type": "cylindrical",
             "parent_group": "g001", "child_group": "g003",
             "origin": [0.0, 1.0, 0.0], "axis": pin,
             "secondary_axis": [1.0, 0.0, 0.0], "limits": None},
        ],
        "loops": [{
            "id": "L1",
            "member_joints": ["j001", "j002", "j003", "j004"],
            "closure_joint": "j003",
            "closure_kind": "aim_pair",
            "suggested_driver_joint": "j001",
            "planar": True,
            "plane_normal": pin,
        }],
        "warnings": [],
    }


def _aim_error(arm_obj, bone_name, target_world):
    """Angle between the bone's +Y and the direction to the target point."""
    from mathutils import Vector
    pb = arm_obj.pose.bones[bone_name]
    head = arm_obj.matrix_world @ pb.head
    axis = (arm_obj.matrix_world.to_3x3() @ (pb.y_axis)).normalized()
    toward = (Vector(target_world) - head)
    if toward.length < 1e-9:
        return 0.0
    return axis.angle(toward.normalized())


def run():
    fd, manifest_path = tempfile.mkstemp(suffix=".rig.json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(_ram_manifest(), f)

    sys.path.insert(0, _ADDON_DIR)

    import bpy
    from mathutils import Vector
    from STEPper_NEXT import rig
    rig.register()

    from STEPper_NEXT.rig import graph, manifest as manifest_mod, rig_build

    m = manifest_mod.load(manifest_path)
    plan = graph.build(m)

    # 1) The loop is planned as an aim pair, not as IK.
    _check(len(plan.sliders) == 1,
           "expected 1 slider plan, got {}".format(len(plan.sliders)))
    _check(not plan.loops,
           "the slider loop should not also produce an IK plan")
    sp = plan.sliders[0]
    _check((sp.a_group, sp.c_group) == ("g002", "g003"),
           "slider halves are {}/{}".format(sp.a_group, sp.c_group))
    _check(sp.a_pivot == [1.0, 0.0, 0.0], "barrel pivot {}".format(sp.a_pivot))
    _check(sp.c_pivot == [0.0, 1.0, 0.0], "rod pivot {}".format(sp.c_pivot))
    # Each half's target rides the OTHER half's parent — that is what keeps
    # the two aim constraints out of a dependency cycle.
    _check(sp.a_aim_parent == "g001",
           "barrel aims at a target on {}".format(sp.a_aim_parent))
    _check(sp.c_aim_parent == "g000",
           "rod aims at a target on {}".format(sp.c_aim_parent))

    result = rig_build.build(bpy.context, m, plan)
    arm_obj = result.armature_object

    barrel = result.bone_names["g002"]
    rod = result.bone_names["g003"]
    clamp = result.bone_names["g001"]

    # 2) Both halves carry a LOCKED Track at the other's duplicate: a ram half
    #    hangs on a pin and the only turn it can make is about that pin. The
    #    bone rests with local Z on the pin, so LOCK_Z is that pin exactly.
    for gid, name, tag in (("g002", barrel, "a"), ("g003", rod, "c")):
        pb = arm_obj.pose.bones[name]
        cons = [c for c in pb.constraints if c.type == "LOCKED_TRACK"]
        _check(len(cons) == 1,
               "bone {} has {} locked tracks".format(name, len(cons)))
        con = cons[0]
        _check(con.track_axis == "TRACK_Y",
               "bone {} tracks {}".format(name, con.track_axis))
        _check(con.lock_axis == "LOCK_Z",
               "bone {} locks {}".format(name, con.lock_axis))
        _check(con.subtarget == result.aim_names[("L1", tag)],
               "bone {} tracks {!r}".format(name, con.subtarget))
        # Local Z really is the pin, or locking it means nothing.
        pin = (arm_obj.matrix_world @ pb.matrix).to_3x3().col[2]
        _check(abs(abs(pin.z) - 1.0) < 1e-6,   # bone matrices are float32
               "bone {} local Z is {}, not the pin".format(name, tuple(pin)))
        # ...and no rotation channel is left for a hand to break it with: the
        # aim owns the orientation entirely.
        _check(all(pb.lock_rotation),
               "bone {} leaves a rotation channel open: {}".format(
                   name, tuple(pb.lock_rotation)))

    # 3) At rest each half already points at the other's pivot.
    for name, target in ((barrel, (0.0, 1.0, 0.0)), (rod, (1.0, 0.0, 0.0))):
        err = _aim_error(arm_obj, name, target)
        _check(err < 1e-4,
               "bone {} is {:.4f} rad off its target at rest".format(name, err))

    # 4) Pose the clamp — the input a hand actually grabs — and the ram must
    #    follow it round. The rod pin C swings with the clamp; both halves
    #    must still be pointing at each other afterwards, and the gap between
    #    them must have changed, which is the ram extending.
    def pivots():
        bp = arm_obj.pose.bones[barrel]
        rp = arm_obj.pose.bones[rod]
        return (arm_obj.matrix_world @ bp.head,
                arm_obj.matrix_world @ rp.head)

    a_rest, c_rest = pivots()
    rest_len = (c_rest - a_rest).length
    _check(abs(rest_len - _ROOT2) < 1e-4,
           "the ram is {:.4f} long at rest, expected sqrt(2)".format(rest_len))

    for angle in (0.3, -0.3):
        pb = arm_obj.pose.bones[clamp]
        pb.rotation_mode = "YXZ"
        pb.rotation_euler = (0.0, angle, 0.0)   # about the bone's own +Y = pin
        bpy.context.view_layer.update()

        a_now, c_now = pivots()
        _check((a_now - a_rest).length < 1e-4,
               "the bore pivot moved; it is fixed to the body")
        moved = (c_now - c_rest).length
        _check(moved > 0.05,
               "posing the clamp moved the rod pin only {:.4f}".format(moved))

        length = (c_now - a_now).length
        _check(abs(length - rest_len) > 0.05,
               "the ram length did not change ({:.4f} vs {:.4f}) — the halves "
               "are frozen".format(length, rest_len))

        # The law of cosines the exporter used to derive the clamp's limit:
        # |AC| = sqrt(2 - 2*cos(90 deg + angle)) with unit sides.
        want = math.sqrt(2.0 - 2.0 * math.cos(math.pi / 2.0 + angle))
        _check(abs(length - want) < 1e-3,
               "ram length {:.4f}, triangle says {:.4f}".format(length, want))

        for name, target in ((barrel, tuple(c_now)), (rod, tuple(a_now))):
            err = _aim_error(arm_obj, name, target)
            _check(err < 1e-3,
                   "at clamp {:+.2f} rad, bone {} is {:.4f} rad off its "
                   "target".format(angle, name, err))

        pb.rotation_euler = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()

    # 4b) The pin is honoured even when the target leaves its plane. Drag the
    #     duplicate the barrel aims at 0.5 up the pin: a Damped Track would
    #     tilt the ram out of the plane its pin allows and the rod would come
    #     at the bore at an angle, which is what live ClampRig did
    #     (2026-08-24). A Locked Track turns about the pin and no further.
    helper = arm_obj.pose.bones[result.aim_names[("L1", "a")]]
    helper.location = (0.0, 0.0, 0.5)
    bpy.context.view_layer.update()
    bp = arm_obj.pose.bones[barrel]
    pin = (arm_obj.matrix_world @ bp.matrix).to_3x3().col[2]
    _check(abs(abs(pin.z) - 1.0) < 1e-6,
           "with its target off the pin plane the barrel tilted to {} — it "
           "is no longer turning about its pin".format(tuple(pin)))
    aim = (arm_obj.matrix_world @ bp.matrix).to_3x3().col[1]
    _check(abs(aim.z) < 1e-6,
           "the barrel now points {:.4f} out of its pin's plane".format(aim.z))
    helper.location = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()

    # 5) The clamp carries the limit the ram's stroke implies, so it cannot be
    #    posed past the point where the ram bottoms out.
    limits = [c for c in arm_obj.pose.bones[clamp].constraints
              if c.type == "LIMIT_ROTATION"]
    _check(limits, "the clamp has no rotation limit; it can swing off the ram")
    lim = limits[0]
    _check(abs(lim.min_y + math.pi / 6.0) < 1e-6
           and abs(lim.max_y - math.pi / 6.0) < 1e-6,
           "clamp limit is [{:.4f}, {:.4f}], expected +/- 30 deg".format(
               lim.min_y, lim.max_y))

    print("rig_slider_smoke: OK — {} bones, {} aim targets, ram tracked "
          "through {} poses".format(
              len(result.bone_names), len(result.aim_names), 2))


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        print("\nrig_slider_smoke: FAILED")
        sys.exit(1)
