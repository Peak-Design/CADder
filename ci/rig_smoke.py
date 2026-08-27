# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless rig-build smoke test.

    blender -b --factory-startup -P ci/rig_smoke.py -- --manifest <path>

Registers the rig subpackage straight from the repo checkout (STEP import
and OCP are not touched), loads the manifest,
builds the rig with NO geometry present, and asserts the rig matches the
manifest: bone counts, joint-axis alignment, limit deltas on the
constraints, driver expressions for couplings. Any failure prints a
readable message and exits non-zero so CI fails loudly.

Without --manifest a built-in demo manifest (planar four-bar loop, a screw
with self-coupling, a gear pair) is written to a temp file and used, so the
script is self-contained.
"""

import json
import os
import sys
import tempfile
import traceback

# The addons directory (the parent of the STEPper_NEXT repo root), so the
# rig subpackage imports exactly as Blender's addon loader sees it.
_ADDON_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _identity4():
    return [[1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def _demo_manifest():
    def comp(cid, name):
        return {"id": cid, "sw_path": name + "-1", "step_name": name,
                "step_occurrence_path": "Demo/" + name + "-1",
                "transform": _identity4()}

    def group(gid, name, cid, grounded, diag):
        return {"id": gid, "name": name, "components": [cid],
                "grounded": grounded, "frame": _identity4() if grounded else None,
                "bbox_diag": diag}

    def rev(jid, parent, child, origin, limits=None, coupling=None):
        return {"id": jid, "type": "revolute", "parent_group": parent,
                "child_group": child, "origin": origin,
                "axis": [0.0, 0.0, 1.0], "secondary_axis": [1.0, 0.0, 0.0],
                "limits": limits, "coupling": coupling}

    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.SwToBlender", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "demo.step", "ap": "AP214", "sha1": None,
                        "occurrence_matching": None},
        "components": [comp("c%03d" % i, n) for i, n in enumerate(
            ["Ground", "Crank", "Rocker", "Coupler", "Nut", "Wheel", "Stud",
             "Slider", "Stud2", "Cone", "MirrorA", "MirrorB",
             "MirrorC", "MirrorD"], 1)],
        "rigid_groups": [
            group("g000", "ground", "c001", True, 0.5),
            group("g001", "crank", "c002", False, 0.1),
            group("g002", "rocker", "c003", False, 0.15),
            group("g003", "coupler", "c004", False, 0.2),
            group("g004", "nut", "c005", False, 0.05),
            group("g005", "wheel", "c006", False, 0.08),
            group("g006", "stud", "c007", False, 0.06),
            group("g007", "slider", "c008", False, 0.07),
            group("g008", "stud2", "c009", False, 0.06),
            {"id": "g009", "name": "cone_carrier", "components": [],
             "grounded": False, "frame": None, "bbox_diag": 0.03},
            group("g010", "cone", "c010", False, 0.05),
            group("g011", "mirror_a", "c011", False, 0.05),
            group("g012", "mirror_b", "c012", False, 0.05),
            group("g013", "mirror_c", "c013", False, 0.05),
            group("g014", "mirror_d", "c014", False, 0.05),
        ],
        "joints": [
            rev("j001", "g000", "g001", [0.0, 0.0, 0.0],
                limits={"rotation": {"min": -0.5, "max": 1.0,
                                     "value_at_rest": 0.25},
                        "translation": None}),
            rev("j002", "g000", "g002", [0.3, 0.0, 0.0]),
            rev("j003", "g001", "g003", [0.0, 0.1, 0.0]),
            rev("j004", "g002", "g003", [0.3, 0.1, 0.0]),
            {"id": "j005", "type": "screw", "parent_group": "g000",
             "child_group": "g004", "origin": [0.0, 0.0, 0.2],
             "axis": [0.0, 0.0, 1.0], "secondary_axis": [0.0, 1.0, 0.0],
             "limits": {"rotation": None,
                        "translation": {"min": 0.0, "max": 0.05,
                                        "value_at_rest": 0.01}},
             "coupling": {"kind": "screw", "driver_joint": None,
                          "lead_m_per_rev": 0.004}},
            rev("j006", "g000", "g005", [0.1, 0.2, 0.0],
                coupling={"kind": "gear", "driver_joint": "j001",
                          "ratio": -2.0}),
            {"id": "j007", "type": "ball", "parent_group": "g000",
             "child_group": "g006", "origin": [0.2, 0.0, 0.0],
             "axis": None, "secondary_axis": None,
             "limits": {"rotation": {"min": 0.0,
                                     "max": 0.7853981633974483,
                                     "value_at_rest": 0.0},
                        "translation": None}},
            # Swing-cone ball: axis = the parent-fixed cone axis, secondary
            # = the child direction the band [min, max] constrains (equal to
            # the axis here: resting at the cone centre).
            {"id": "j009", "type": "ball", "parent_group": "g000",
             "child_group": "g008", "origin": [0.5, 0.0, 0.0],
             "axis": [0.0, 1.0, 0.0], "secondary_axis": [0.0, 1.0, 0.0],
             "limits": {"rotation": {"min": 0.0,
                                     "max": 0.7853981633974483,
                                     "value_at_rest": 0.0},
                        "translation": None}},
            # Tangent cone on a plane, decomposed through a carrier: planar
            # on the plate normal, then a spin whose axis is TILTED out of
            # the plane by the half-angle (20 deg here: |dot| = sin(20) =
            # 0.342) — collapses to the cone_spin ring template.
            {"id": "j010", "type": "planar", "parent_group": "g000",
             "child_group": "g009", "origin": [0.6, 0.0, 0.0],
             "axis": [0.0, 0.0, 1.0], "secondary_axis": [1.0, 0.0, 0.0],
             "limits": None},
            {"id": "j011", "type": "revolute", "parent_group": "g009",
             "child_group": "g010", "origin": [0.6, 0.0, 0.0],
             "axis": [0.9397, 0.0, 0.342], "secondary_axis": [0.0, 1.0, 0.0],
             "limits": None},
            # Mirror pair: two otherwise-unmated bodies related only by a
            # symmetric mate — ground-rooted free joints, the driven one
            # reflecting the driver across the plane (y = 0 here).
            {"id": "j012", "type": "free", "parent_group": "g000",
             "child_group": "g011", "origin": None, "axis": None,
             "secondary_axis": None, "limits": None},
            {"id": "j013", "type": "free", "parent_group": "g000",
             "child_group": "g012", "origin": None, "axis": None,
             "secondary_axis": None, "limits": None,
             "coupling": {"kind": "mirror", "driver_joint": "j012",
                          "mirror_scope": "plane",
                          "mirror_plane": {"point": [0.8, 0.0, 0.0],
                                           "normal": [0.0, 1.0, 0.0]}}},
            # The other mirror flavour: an assembly MIRROR FEATURE. The
            # instance is a full reflection of its source, so all six
            # channels follow and nothing on the driven bone is posable.
            {"id": "j014", "type": "free", "parent_group": "g000",
             "child_group": "g013", "origin": None, "axis": None,
             "secondary_axis": None, "limits": None},
            {"id": "j015", "type": "free", "parent_group": "g000",
             "child_group": "g014", "origin": None, "axis": None,
             "secondary_axis": None, "limits": None,
             "coupling": {"kind": "mirror", "driver_joint": "j014",
                          "mirror_scope": "rigid",
                          "mirror_plane": {"point": [1.2, 0.0, 0.0],
                                           "normal": [0.0, 1.0, 0.0]}}},
            {"id": "j008", "type": "pin_slot", "parent_group": "g000",
             "child_group": "g007", "origin": [0.4, 0.0, 0.0],
             "axis": [0.0, 0.0, 1.0], "secondary_axis": [0.0, 1.0, 0.0],
             "limits": {"rotation": {"min": -0.3, "max": 0.6,
                                     "value_at_rest": 0.1},
                        "translation": {"min": 0.0, "max": 0.05,
                                        "value_at_rest": 0.02}}},
        ],
        "loops": [{
            "id": "L1",
            "member_joints": ["j001", "j002", "j003", "j004"],
            "closure_joint": "j003",
            "suggested_driver_joint": "j001",
            "planar": True,
            "plane_normal": [0.0, 0.0, 1.0],
        }],
        "warnings": [],
    }


def _fail(message):
    raise AssertionError(message)


def _check(condition, message):
    if not condition:
        _fail(message)


def run():
    args = []
    if "--" in sys.argv:
        args = sys.argv[sys.argv.index("--") + 1:]
    manifest_path = None
    i = 0
    while i < len(args):
        if args[i] == "--manifest" and i + 1 < len(args):
            manifest_path = args[i + 1]
            i += 2
        else:
            print("blender_smoke.py: unknown argument {!r}".format(args[i]))
            sys.exit(2)
    if manifest_path is None:
        fd, manifest_path = tempfile.mkstemp(suffix=".rig.json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_demo_manifest(), f)
        print("No --manifest given; using built-in demo:", manifest_path)

    sys.path.insert(0, _ADDON_DIR)

    import bpy
    from STEPper_NEXT import rig
    rig.register()
    _check(hasattr(bpy.types, "SWTB_PT_panel"), "panel did not register")

    from STEPper_NEXT.rig import graph, manifest as manifest_mod, rig_build

    def hidden_bones(armature):
        """Every bone in a collection the user cannot see. The rig sorts
        bones into controls, limits, mechanism and helpers, and more than one
        of those is hidden — what matters to these checks is not WHICH
        collection a bone landed in but whether it is out of the way."""
        out = set()
        for coll in armature.collections:
            if not coll.is_visible:
                out.update(b.name for b in coll.bones)
        return out

    m = manifest_mod.load(manifest_path)
    plan = graph.build(m)
    result = rig_build.build(bpy.context, m, plan)
    arm_obj = result.armature_object
    arm = arm_obj.data

    # 1) One bone per rigid group (collapsed carriers get none) plus a
    # helper and an effector per loop, DEF/POLE/GOAL for every swing-cone
    # ball, DEF/POLE/GOAL/FRM for every cone_spin collapse, and one limit
    # dial for every control that has a limit to show.
    coned = [bp for bp in plan.bones if bp.ball_def_name]
    cone_spins = [bp for bp in plan.bones
                  if bp.collapsed is not None and bp.collapsed.kind == "cone_spin"]
    limits = len(result.limit_names)
    expected = (len(m.rigid_groups) - len(plan.collapsed_carriers)
                + 2 * len(plan.loops) + 3 * len(coned) + 4 * len(cone_spins)
                + limits)
    _check(len(arm.bones) == expected,
           "bone count {} != groups {} - carriers {} + 2x loops {} + 3x "
           "coned balls {} + 4x cone spins {} + limit dials {}".format(
               len(arm.bones), len(m.rigid_groups), len(plan.collapsed_carriers),
               len(plan.loops), len(coned), len(cone_spins), limits))
    _check(limits > 0, "no limit dial was built at all")

    # 2) Every joint bone's local +Y parallel to the manifest axis — except a
    # swing-cone ball, whose axis is the CONE axis; its bone points along the
    # child-side measured direction (secondary_axis).
    from mathutils import Vector
    for joint in m.joints:
        gid = plan.joint_group.get(joint.id)
        if gid is None or joint.axis is None:
            continue
        along = joint.secondary_axis if graph.swing_cone(joint) else joint.axis
        bone = arm.bones[result.bone_names[gid]]
        y = bone.matrix_local.col[1].to_3d().normalized()
        axis = Vector(along).normalized()
        dot = abs(y.dot(axis))
        _check(dot > 0.9999,
               "joint {} bone Y {} not parallel to {} (|dot|={:.6f})".format(
                   joint.id, tuple(y), along, dot))

    # 3) Constraint ranges are the limit DELTAS, never the absolute values.
    # Blender stores constraint channels as single-precision C floats, so a
    # double like -0.5236 reads back as -0.52359998...; the tolerance must sit
    # above float32 rounding (~6e-8 at these magnitudes), not at double noise.
    tol = 1e-6
    for joint in m.joints:
        gid = plan.joint_group.get(joint.id)
        if gid is None:
            continue
        pb = arm_obj.pose.bones[result.bone_names[gid]]
        cons = {c.name: c for c in pb.constraints}
        if joint.rotation_limit is not None and joint.type in (
                "revolute", "cylindrical", "planar", "pin_slot"):
            con = cons.get("SWTB Limit Rotation")
            _check(con is not None,
                   "joint {}: Limit Rotation missing".format(joint.id))
            _check(con.use_limit_y, "joint {}: use_limit_y off".format(joint.id))
            _check(abs(con.min_y - joint.rotation_limit.delta_min) < tol
                   and abs(con.max_y - joint.rotation_limit.delta_max) < tol,
                   "joint {}: rotation limits [{}, {}] != deltas [{}, {}]".format(
                       joint.id, con.min_y, con.max_y,
                       joint.rotation_limit.delta_min,
                       joint.rotation_limit.delta_max))
            _check(con.owner_space == "LOCAL",
                   "joint {}: Limit Rotation owner_space {}".format(
                       joint.id, con.owner_space))
        if joint.rotation_limit is not None and joint.type == "ball" \
                and joint.axis is None:
            # Legacy manifests only (no cone frame): the Euler-box fallback.
            # The mate dimension is an unsigned swing angle: the cone must
            # be SYMMETRIC about the rest pose on both swing axes (a raw
            # 0..45 deg range applied one-sided pinned the swing into one
            # quadrant, live 2026-08-22), twist about Y free.
            con = cons.get("SWTB Limit Rotation")
            _check(con is not None,
                   "joint {}: ball Limit Rotation missing".format(joint.id))
            amp = max(abs(joint.rotation_limit.delta_min),
                      abs(joint.rotation_limit.delta_max))
            _check(con.use_limit_x and con.use_limit_z and not con.use_limit_y,
                   "joint {}: cone must limit X and Z, leave Y free".format(joint.id))
            _check(abs(con.min_x + amp) < tol and abs(con.max_x - amp) < tol
                   and abs(con.min_z + amp) < tol and abs(con.max_z - amp) < tol,
                   "joint {}: cone limits [{}, {}]/[{}, {}] not symmetric "
                   "+-{}".format(joint.id, con.min_x, con.max_x,
                                 con.min_z, con.max_z, amp))
        if joint.translation_limit is not None and joint.type in (
                "prismatic", "cylindrical", "screw"):
            con = cons.get("SWTB Limit Location")
            _check(con is not None,
                   "joint {}: Limit Location missing".format(joint.id))
            _check(con.use_min_y and con.use_max_y,
                   "joint {}: use_min_y/use_max_y off".format(joint.id))
            _check(abs(con.min_y - joint.translation_limit.delta_min) < tol
                   and abs(con.max_y - joint.translation_limit.delta_max) < tol,
                   "joint {}: location limits [{}, {}] != deltas [{}, {}]".format(
                       joint.id, con.min_y, con.max_y,
                       joint.translation_limit.delta_min,
                       joint.translation_limit.delta_max))
        if joint.type == "pin_slot":
            # The one joint whose slide is bone-local Z (secondary_axis is
            # the slide direction): spin about Y and slide along Z free,
            # everything else locked.
            _check(list(pb.lock_location) == [True, True, False]
                   and list(pb.lock_rotation) == [True, False, True],
                   "joint {}: pin_slot locks are {}/{}".format(
                       joint.id, list(pb.lock_location), list(pb.lock_rotation)))
            if joint.translation_limit is not None:
                con = cons.get("SWTB Limit Location")
                _check(con is not None,
                       "joint {}: pin_slot Limit Location missing".format(joint.id))
                _check(con.use_min_z and con.use_max_z
                       and not con.use_min_y and not con.use_max_y,
                       "joint {}: pin_slot slide limit must be on Z".format(joint.id))
                _check(abs(con.min_z - joint.translation_limit.delta_min) < tol
                       and abs(con.max_z - joint.translation_limit.delta_max) < tol,
                       "joint {}: pin_slot slide limits [{}, {}] != deltas "
                       "[{}, {}]".format(joint.id, con.min_z, con.max_z,
                                         joint.translation_limit.delta_min,
                                         joint.translation_limit.delta_max))

    # 3b) Swing-cone balls: the ctrl/DEF/POLE/GOAL template, then the clamp
    # behaviour itself — inside the cone DEF equals the handle exactly
    # (twist included); beyond it DEF holds the max swing angle UNIFORMLY
    # around the azimuth (the Euler box let ~1.27x the limit through at the
    # diagonals, live corpus 04, 2026-08-23).
    import math
    from mathutils import Quaternion
    for bp in coned:
        gid = bp.group.id
        joint = bp.joint
        ctrl_name = result.ball_ctrl_names[gid]
        def_name = result.bone_names[gid]
        out_of_sight = hidden_bones(arm)
        _check(ctrl_name not in out_of_sight,
               "ball {}: the user handle is hidden".format(joint.id))
        for name in (def_name, result.ball_pole_names[gid],
                     result.ball_goal_names[gid]):
            _check(name in out_of_sight,
                   "ball {}: {} is not hidden".format(joint.id, name))
        def_pb = arm_obj.pose.bones[def_name]
        _check(def_pb.get("RIG_group") == gid,
               "ball {}: RIG_group is not on DEF".format(joint.id))
        kinds = [c.type for c in def_pb.constraints]
        _check(kinds == ["COPY_ROTATION", "DAMPED_TRACK"],
               "ball {}: DEF constraints {} != Copy Rotation + Damped "
               "Track".format(joint.id, kinds))
        goal_pb = arm_obj.pose.bones[result.ball_goal_names[gid]]
        dist_cons = [c for c in goal_pb.constraints if c.type == "LIMIT_DISTANCE"]
        per_round = 2 if joint.rotation_limit.min <= 1e-9 else 3
        _check(goal_pb.constraints[0].type == "COPY_LOCATION"
               and len(dist_cons) == 3 * per_round,
               "ball {}: GOAL chain is {}".format(
                   joint.id, [c.type for c in goal_pb.constraints]))

        ctrl_pb = arm_obj.pose.bones[ctrl_name]
        _check(list(ctrl_pb.lock_rotation) == [False, False, False]
               and list(ctrl_pb.lock_location) == [True, True, True],
               "ball {}: handle locks wrong".format(joint.id))
        ctrl_pb.rotation_mode = "QUATERNION"
        axis_world = Vector(joint.axis).normalized()

        def def_swing():
            bpy.context.view_layer.update()
            y = (arm_obj.matrix_world @ def_pb.matrix).col[1].to_3d().normalized()
            return math.acos(max(-1.0, min(1.0, y.dot(axis_world))))

        # Inside the cone: DEF == handle exactly, twist included.
        swing = Quaternion((1.0, 0.0, 0.0), 0.5) @ Quaternion((0.0, 1.0, 0.0), 0.4)
        ctrl_pb.rotation_quaternion = swing
        bpy.context.view_layer.update()
        delta = (ctrl_pb.matrix.inverted() @ def_pb.matrix).to_quaternion().angle
        _check(delta < 1e-5,
               "ball {}: DEF is {} rad off the handle inside the cone".format(
                   joint.id, delta))

        # Beyond the cone: the swing clamps at max, uniformly in azimuth.
        angles = []
        for step in range(8):
            az = step * math.pi / 4.0
            ctrl_pb.rotation_quaternion = Quaternion(
                (math.cos(az), 0.0, math.sin(az)), 1.2)
            angles.append(def_swing())
        worst = max(abs(a - joint.rotation_limit.max) for a in angles)
        spread = max(angles) - min(angles)
        _check(worst < 0.005,
               "ball {}: clamped swing off by {:.4f} rad (angles {})".format(
                   joint.id, worst, ["%.4f" % a for a in angles]))
        _check(spread < 0.002,
               "ball {}: clamped swing varies {:.4f} rad around the "
               "azimuth".format(joint.id, spread))
        ctrl_pb.rotation_quaternion = Quaternion()
        bpy.context.view_layer.update()

    # 3c) cone_spin collapses: the ring template — the handle slides ON the
    # plane and rotates freely; DEF holds the spin axis at the fixed tilt
    # from the plane normal at ANY handle pose, following position exactly.
    for bp in cone_spins:
        gid = bp.group.id
        spec = bp.collapsed
        ctrl_pb = arm_obj.pose.bones[result.ball_ctrl_names[gid]]
        def_pb = arm_obj.pose.bones[result.bone_names[gid]]
        out_of_sight = hidden_bones(arm)
        _check(ctrl_pb.name not in out_of_sight,
               "cone {}: the handle is hidden".format(gid))
        for name in (result.bone_names[gid], result.ball_pole_names[gid],
                     result.ball_goal_names[gid], result.cone_frame_names[gid]):
            _check(name in out_of_sight,
                   "cone {}: {} is not hidden".format(gid, name))
        normal_world = Vector(spec.carrier_joint.axis).normalized()

        def def_tilt():
            bpy.context.view_layer.update()
            y = (arm_obj.matrix_world @ def_pb.matrix).col[1].to_3d().normalized()
            return math.acos(max(-1.0, min(1.0, y.dot(normal_world))))

        rest_z = (arm_obj.matrix_world @ ctrl_pb.matrix).translation.z
        # Slide anywhere: the plane clamp holds the handle (and DEF) at the
        # contact plane's height; DEF follows the position exactly.
        ctrl_pb.location = (0.05, 0.02, -0.03)
        bpy.context.view_layer.update()
        head = (arm_obj.matrix_world @ ctrl_pb.matrix).translation
        _check(abs(head.z - rest_z) < 1e-5,
               "cone {}: handle left the plane (z {} vs rest {})".format(
                   gid, head.z, rest_z))
        dhead = (arm_obj.matrix_world @ def_pb.matrix).translation
        _check((dhead - head).length < 1e-5,
               "cone {}: DEF is not at the handle position".format(gid))

        # Pure spin sits on the ring already: DEF == handle exactly.
        from mathutils import Quaternion
        ctrl_pb.rotation_mode = "QUATERNION"
        ctrl_pb.rotation_quaternion = Quaternion((0.0, 1.0, 0.0), 0.7)
        bpy.context.view_layer.update()
        delta = (ctrl_pb.matrix.inverted() @ def_pb.matrix).to_quaternion().angle
        _check(delta < 1e-4,
               "cone {}: DEF off the handle {} rad under pure spin".format(
                   gid, delta))
        # Arbitrary rotations: DEF's axis stays ON the fixed-tilt ring.
        for q in (Quaternion((1.0, 0.0, 0.0), 0.5),
                  Quaternion((0.0, 0.0, 1.0), 1.1) @ Quaternion((1.0, 0.0, 0.0), -0.8),
                  Quaternion((0.6, 0.8, 0.0), 2.0)):
            ctrl_pb.rotation_quaternion = q
            t = def_tilt()
            _check(abs(t - spec.tilt) < 0.005,
                   "cone {}: axis tilt {:.4f} != ring {:.4f}".format(
                       gid, t, spec.tilt))
        ctrl_pb.rotation_quaternion = Quaternion()
        ctrl_pb.location = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()

    # 3d) Mirror pair. A symmetric mate between two planar faces is a
    # plane-to-plane relation, so it couples exactly THREE degrees of
    # freedom: the translation along the mirror normal and the two
    # rotations that tilt it. Those three are also exactly the channels
    # that negate under the reflection, which is why the drivers are three
    # sign flips. The other three must stay independent — SolidWorks lets
    # one block be raised without the other (live corpus 14 sym4).
    from mathutils import Matrix as _Mx
    mirror_joints = [j for j in m.joints
                     if j.coupling is not None and j.coupling.kind == "mirror"]
    for joint in mirror_joints:
        rigid = joint.coupling.mirror_scope == "rigid"
        drv_gid = plan.joint_group[joint.coupling.driver_joint]
        dvn_gid = plan.joint_group[joint.id]
        drv_pb = arm_obj.pose.bones[result.bone_names[drv_gid]]
        dvn_pb = arm_obj.pose.bones[result.bone_names[dvn_gid]]
        want_loc = [True] * 3 if rigid else [False, True, False]
        want_rot = [True] * 3 if rigid else [True, False, True]
        _check(list(dvn_pb.lock_location) == want_loc
               and list(dvn_pb.lock_rotation) == want_rot,
               "mirror {} ({}): driven bone locks {} {}, wanted {} {}".format(
                   joint.id, joint.coupling.mirror_scope,
                   list(dvn_pb.lock_location), list(dvn_pb.lock_rotation),
                   want_loc, want_rot))
        _check(list(drv_pb.lock_location) == [False, False, False]
               and list(drv_pb.lock_rotation) == [False, False, False],
               "mirror {}: driver bone is not free".format(joint.id))

        p = Vector(joint.coupling.mirror_plane_point)
        n = Vector(joint.coupling.mirror_plane_normal).normalized()
        # Affine world reflection S (linear I - 2nn^T, translated so the
        # plane is fixed) on the LEFT; the material correspondence between
        # the twin bodies on the RIGHT is the translation-free linear part.
        S = _Mx.Identity(4)
        S_lin = _Mx.Identity(4)
        for r in range(3):
            for cc in range(3):
                S[r][cc] -= 2.0 * n[r] * n[cc]
                S_lin[r][cc] = S[r][cc]
            S[r][3] = 2.0 * p.dot(n) * n[r]

        # Both bones must decompose with the SAME euler order or the
        # per-channel sign flips stop being the reflection.
        drv_pb.rotation_mode = dvn_pb.rotation_mode = "YXZ"
        rest = (arm_obj.matrix_world
                @ arm_obj.data.bones[dvn_pb.name].matrix_local)

        # The driven body is the exact reflection of the driver. For the
        # "plane" scope only the three COUPLED channels are posed here, since
        # the other three are deliberately independent; for "rigid" every
        # channel is posed, because every channel follows.
        poses = (((0.0, 0.05, 0.0), (0.4, 0.0, 0.2)),
                 ((0.0, -0.03, 0.0), (-0.6, 0.0, 1.1)))
        if rigid:
            poses = (((0.03, 0.05, -0.02), (0.4, 0.7, 0.2)),
                     ((-0.01, -0.03, 0.04), (-0.6, -0.3, 1.1)))
        for loc, rot in poses:
            drv_pb.location = loc
            drv_pb.rotation_euler = rot
            bpy.context.view_layer.update()
            want = S @ (arm_obj.matrix_world @ drv_pb.matrix) @ S_lin
            got = arm_obj.matrix_world @ dvn_pb.matrix
            err = max(abs(want[r][cc] - got[r][cc])
                      for r in range(3) for cc in range(4))
            _check(err < 1e-5,
                   "mirror {}: driven off the reflection by {}".format(joint.id, err))

        if rigid:
            # A mirror feature leaves nothing independent, so the
            # independence checks below do not apply. Reset and move on.
            drv_pb.location = (0.0, 0.0, 0.0)
            drv_pb.rotation_euler = (0.0, 0.0, 0.0)
            bpy.context.view_layer.update()
            continue

        # FREE channels: sliding the driver within the plane, or spinning it
        # about the normal, must leave the twin exactly where it was.
        drv_pb.location = (0.04, 0.0, -0.05)
        drv_pb.rotation_euler = (0.0, 0.8, 0.0)
        bpy.context.view_layer.update()
        got = arm_obj.matrix_world @ dvn_pb.matrix
        err = max(abs(rest[r][cc] - got[r][cc])
                  for r in range(3) for cc in range(4))
        _check(err < 1e-6,
               "mirror {}: the twin followed a FREE channel by {} — the two "
               "bodies are welded".format(joint.id, err))

        # And the twin can be posed in those channels on its own.
        dvn_pb.location = (0.02, 0.0, 0.0)
        bpy.context.view_layer.update()
        moved = ((arm_obj.matrix_world @ dvn_pb.matrix).translation
                 - rest.translation).length
        _check(moved > 1e-4,
               "mirror {}: the twin cannot be moved independently".format(joint.id))
        dvn_pb.location = (0.0, 0.0, 0.0)
        drv_pb.location = (0.0, 0.0, 0.0)
        drv_pb.rotation_euler = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()

    # 4) Every coupling produced a scripted driver on the driven channel.
    coupled = [j for j in m.joints
               if j.coupling is not None and plan.joint_group.get(j.id)]
    if coupled:
        anim = arm_obj.animation_data
        _check(anim is not None, "couplings present but no animation data")
        driver_paths = {(fc.data_path, fc.array_index): fc
                       for fc in anim.drivers}
        for joint in coupled:
            pb = arm_obj.pose.bones[result.bone_names[plan.joint_group[joint.id]]]
            channel = ("rotation_euler"
                       if joint.coupling.kind in ("gear", "screw")
                       else "location")
            key = (pb.path_from_id(channel), 1)
            fc = driver_paths.get(key)
            _check(fc is not None,
                   "joint {}: no driver on {}[1]".format(joint.id, channel))
            _check(fc.driver.type == "SCRIPTED" and fc.driver.expression,
                   "joint {}: driver has no expression".format(joint.id))

    # 4b) The bone taxonomy. Only what a user can actually grab is visible,
    # and everything visible says what it does.
    controls = arm.collections.get("SW_controls")
    limits = arm.collections.get("SW_limits")
    mechanism = arm.collections.get("SW_mechanism")
    _check(controls is not None and controls.is_visible,
           "SW_controls missing or hidden")
    _check(limits is not None and limits.is_visible,
           "SW_limits missing or hidden")
    _check(mechanism is not None and not mechanism.is_visible,
           "SW_mechanism missing or visible")

    control_names = {b.name for b in controls.bones}
    _check(control_names, "no control bone at all")
    for name in control_names:
        pb = arm_obj.pose.bones[name]
        _check(pb.custom_shape is not None,
               "control {} has no widget".format(name))
        _check(pb.color.palette == "THEME01",
               "control {} is not the control colour".format(name))
        _check(not (all(pb.lock_location) and all(pb.lock_rotation)),
               "control {} has nothing unlocked to pose".format(name))

    # Being DRIVEN does not by itself make a bone mechanism — what decides
    # is whether anything of its own is left to pose. A gear follower and a
    # mirror-FEATURE instance have every channel written by their driver and
    # are mechanism; a plane-symmetry follower still slides in its plane and
    # spins about the normal on its own, and hiding that would take away a
    # freedom SolidWorks allows (live corpus 14 sym4, 2026-08-25).
    for bp in plan.bones:
        joint = bp.joint
        if joint is None or joint.coupling is None:
            continue
        if not joint.coupling.driver_joint:
            continue
        driven = result.ball_ctrl_names.get(bp.group.id,
                                            result.bone_names[bp.group.id])
        pb = arm_obj.pose.bones[driven]
        own_freedom = not (all(pb.lock_location) and all(pb.lock_rotation))
        if own_freedom:
            _check(driven in control_names,
                   "driven bone {} keeps {} unlocked channel(s) of its own "
                   "but is hidden away in the mechanism".format(
                       driven,
                       sum(1 for v in list(pb.lock_location)
                           + list(pb.lock_rotation) if not v)))
        else:
            _check(driven not in control_names,
                   "driven bone {} is offered as a control".format(driven))
            _check(driven in {b.name for b in mechanism.bones},
                   "driven bone {} is not in the mechanism".format(driven))

    # A bone the IK of a loop closure places is not posable either: dragging
    # it only fights the solver back (live corpus 06, 2026-08-25 — the
    # four-bar showed two red bones and the second one did nothing).
    for lplan in plan.loops:
        for gid in list(lplan.driven_chain or []) + [lplan.ik_tip_group]:
            if not gid or gid not in result.bone_names:
                continue
            name = result.ball_ctrl_names.get(gid, result.bone_names[gid])
            _check(name not in control_names,
                   "{} is solved by loop {} yet offered as a control".format(
                       name, lplan.loop.id))

    from mathutils import Matrix as _WidgetMx

    # A widget is drawn with its OWN origin at the bone's head. Give the
    # object a transform of its own and the shape is drawn off the bone —
    # a dial that orbits the joint instead of turning about it.
    for pb in arm_obj.pose.bones:
        shape = pb.custom_shape
        if shape is None:
            continue
        _check(shape.matrix_world == _WidgetMx.Identity(4),
               "widget {} carries a transform of its own".format(shape.name))
        _check(tuple(pb.custom_shape_translation) == (0.0, 0.0, 0.0),
               "{}: its widget is offset from the bone".format(pb.name))

    # Every limit dial is fixed, coloured as a limit, and carries a widget
    # built from the joint's own numbers.
    for gid, lname in result.limit_names.items():
        lb = arm_obj.pose.bones[lname]
        _check(lb.name in {b.name for b in limits.bones},
               "{} is not in SW_limits".format(lname))
        _check(lb.color.palette == "THEME09",
               "{} is not the limit colour".format(lname))
        _check(all(lb.lock_location) and all(lb.lock_rotation),
               "{} can be posed; a dial must stay put".format(lname))
        _check(lb.custom_shape is not None, "{} has no dial".format(lname))
        joint = plan.bone_by_group[gid].joint
        if joint.translation_limit is not None and joint.rotation_limit is None:
            # A stroke rail is a MEASUREMENT: it must not be rescaled by the
            # bone's length, and it must be as long as the travel.
            _check(not lb.use_custom_shape_bone_size,
                   "{}: a stroke rail must not scale with the bone".format(lname))
            span = (joint.translation_limit.delta_max
                    - joint.translation_limit.delta_min)
            ys = [v.co[1] for v in lb.custom_shape.data.vertices]

            # The limit clamps the slide's ORIGIN, and the slide widget
            # sticks out half its length either side of that origin. So the
            # rail runs half a slide past each stop: hard against the limit,
            # the slide's end meets the rail's end. Unpadded, the slide hung
            # half off the rail at exactly the pose that has to look right.
            ctrl = arm_obj.pose.bones[
                result.ball_ctrl_names.get(gid, result.bone_names[gid])]
            _check(ctrl.custom_shape is not None and
                   ctrl.use_custom_shape_bone_size,
                   "{}: the slide it measures has no bone-sized widget"
                   .format(lname))
            slide_ys = [v.co[1] for v in ctrl.custom_shape.data.vertices]
            half_slide = max(slide_ys) * ctrl.bone.length
            _check(half_slide > 1e-9, "{}: the slide widget is flat"
                   .format(lname))

            want = span + 2.0 * half_slide
            _check(abs((max(ys) - min(ys)) - want) < 1e-6,
                   "{}: rail is {:.4f} long, travel + slide is {:.4f}".format(
                       lname, max(ys) - min(ys), want))
            at_stop = joint.translation_limit.delta_min - half_slide
            _check(abs(min(ys) - at_stop) < 1e-6,
                   "{}: rail ends at {:.4f}, the slide reaches {:.4f}".format(
                       lname, min(ys), at_stop))

    # 5) Loop closures: helper and effector out of sight, IK on the
    # effector aiming its HEAD (the closure point) at the helper.
    for lplan in plan.loops:
        helper_name = result.helper_names[lplan.loop.id]
        effector_name = result.effector_names[lplan.loop.id]
        coll = arm.collections.get("SW_helpers")
        _check(coll is not None and not coll.is_visible,
               "SW_helpers bone collection missing or visible")
        out_of_sight = hidden_bones(arm)
        for name in (helper_name, effector_name):
            _check(name in out_of_sight, "{} is not hidden".format(name))
        tip_pb = arm_obj.pose.bones[result.bone_names[lplan.ik_tip_group]]
        _check(not any(c.type == "IK" for c in tip_pb.constraints),
               "loop {}: stray IK on the tip bone".format(lplan.loop.id))
        eff_pb = arm_obj.pose.bones[effector_name]
        _check(eff_pb.parent is not None and eff_pb.parent.name
               == result.bone_names[lplan.ik_tip_group],
               "loop {}: effector not parented to the tip".format(lplan.loop.id))
        iks = [c for c in eff_pb.constraints if c.type == "IK"]
        _check(len(iks) == 1, "loop {}: expected 1 IK constraint, found "
               "{}".format(lplan.loop.id, len(iks)))
        ik = iks[0]
        _check(ik.subtarget == helper_name and ik.target == arm_obj,
               "loop {}: IK target wrong".format(lplan.loop.id))
        _check(ik.use_tail,
               "loop {}: use_tail off re-targets the tip parent's tail; the "
               "effector TAIL is the closure point".format(lplan.loop.id))
        eff_bone = arm.bones[effector_name]
        helper_bone = arm.bones[helper_name]
        _check((eff_bone.tail_local - helper_bone.head_local).length < 1e-6,
               "loop {}: effector tail is not on the closure point".format(
                   lplan.loop.id))
        _check(ik.chain_count == lplan.chain_count + 1,
               "loop {}: chain_count {} != {} + effector".format(
                   lplan.loop.id, ik.chain_count, lplan.chain_count))
        _check(ik.pole_target is None,
               "loop {}: pole target set â€” T28313 disables IK limits".format(
                   lplan.loop.id))
        _check(not ik.use_stretch, "loop {}: use_stretch on".format(lplan.loop.id))

    # 6) Rotation order set before constraints copied it.
    for gid, name in result.bone_names.items():
        _check(arm_obj.pose.bones[name].rotation_mode == "YXZ",
               "bone {} rotation_mode is not YXZ".format(name))

    print("blender_smoke: OK â€” {} bones, {} helpers, {} drivers, {} loops".format(
        len(result.bone_names), len(result.helper_names), len(coupled),
        len(plan.loops)))


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        print("\nblender_smoke: FAILED")
        sys.exit(1)
