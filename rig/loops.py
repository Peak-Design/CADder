# SPDX-License-Identifier: GPL-3.0-or-later
"""Loop closures: IK from an effector on the driven tip to a helper on the
driver side.

The manifest's loop list is authoritative — the exporter already chose the
cut edge and this module never re-derives cycles. Two hidden bones per loop
(created in rig_build's single edit session, in the SW_helpers collection)
meet at the closure point: the helper's HEAD sits on it riding the
driver-side bone, so posing the driver drags the IK target; the effector's
TAIL sits on it riding the driven tip, and owns the IK constraint — the
solver rotates the driven chain until that tail lands on the helper. Aiming
IK at the tip bone's own tail instead left the four-bar dead (live corpus
06): bones point along the hinge axes, so a tip tail sits off the closure
point and cannot even move in the mechanism plane.

No pole target, ever: a pole target disables IK bone limits for the whole
chain (Blender bug T28313), and the limits are the entire point here.

The IK solver ignores Limit Rotation constraints, so every joint limit is
duplicated into the pose bone's ik_* settings.
"""

try:
    import bpy
except ImportError:
    bpy = None

_IK_NAME = "SWTB IK "


def _set_ik_y_limit(pb, limit):
    pb.use_ik_limit_y = True
    pb.ik_min_y = limit.delta_min
    pb.ik_max_y = limit.delta_max


def _configure_chain_bone(pb, joint, planar_loop):
    pb.lock_ik_x = False
    pb.lock_ik_y = False
    pb.lock_ik_z = False
    pb.use_ik_limit_x = False
    pb.use_ik_limit_y = False
    pb.use_ik_limit_z = False

    jtype = joint.type if joint is not None else "fixed"

    if jtype in ("revolute", "cylindrical", "planar", "pin_slot"):
        # IK only rotates, so a pin_slot in a chain contributes its spin and
        # holds its slide at rest — same shape as the others here.
        pb.lock_ik_x = True
        pb.lock_ik_z = True
        if joint.rotation_limit is not None:
            _set_ik_y_limit(pb, joint.rotation_limit)
    elif jtype in ("prismatic", "screw"):
        # Blender IK only rotates; a sliding joint inside a chain cannot be
        # solved, so the bone is held rigid rather than allowed to rotate in
        # a way the joint never could.
        pb.lock_ik_x = True
        pb.lock_ik_y = True
        pb.lock_ik_z = True
    elif jtype == "ball":
        if joint.rotation_limit is not None:
            # Same symmetric swing cone the Limit Rotation applies: the mate
            # dimension is an UNSIGNED swing angle, so the raw one-sided
            # deltas would pin the IK solve into one quadrant exactly as they
            # pinned hand-posing (live corpus 04, 2026-08-22).
            amp = max(abs(joint.rotation_limit.delta_min),
                      abs(joint.rotation_limit.delta_max))
            pb.use_ik_limit_x = True
            pb.ik_min_x = -amp
            pb.ik_max_x = amp
            pb.use_ik_limit_z = True
            pb.ik_min_z = -amp
            pb.ik_max_z = amp
    elif jtype == "fixed":
        pb.lock_ik_x = True
        pb.lock_ik_y = True
        pb.lock_ik_z = True

    if planar_loop:
        # Off-plane swing lets the solver branch-flip out of the mechanism
        # plane; with joint axes normal to the plane, X and Z are off-plane.
        pb.lock_ik_x = True
        pb.lock_ik_z = True


def close_loops(arm_obj, plan, bone_names, helper_names, effector_names):
    """Adds one IK constraint per manifest loop. bone_names / helper_names /
    effector_names map plan ids to the names Blender actually kept. Returns
    (count, warnings)."""
    warnings = []
    count = 0
    pose = arm_obj.pose

    for lplan in plan.loops:
        helper = helper_names.get(lplan.loop.id)
        eff_name = effector_names.get(lplan.loop.id)
        eff_pb = pose.bones.get(eff_name) if eff_name else None
        tip_name = bone_names.get(lplan.ik_tip_group)
        tip_pb = pose.bones.get(tip_name) if tip_name else None
        if helper is None or tip_pb is None or eff_pb is None:
            warnings.append(
                "loop {}: helper, effector or tip bone missing, closure "
                "skipped".format(lplan.loop.id))
            continue

        for pb in (tip_pb, eff_pb):
            for con in list(pb.constraints):
                if con.name.startswith(_IK_NAME):
                    pb.constraints.remove(con)

        con = eff_pb.constraints.new("IK")
        con.name = _IK_NAME + lplan.loop.id
        con.target = arm_obj
        con.subtarget = helper
        # use_tail stays ON: the effector's tail IS the closure point
        # (rig_build placed it there). Turning it off would re-target the
        # owner's parent's tail — Blender's use_tail=False is only "use the
        # head" for connected bones — and walk the chain one bone too far,
        # recruiting the mechanism root into a depsgraph cycle.
        con.use_tail = True
        # The effector plus exactly the driven chain — one bone more would
        # recruit the common ancestor and bend the driver side too.
        con.chain_count = lplan.chain_count + 1
        con.use_stretch = False

        # All axes locked: the effector is a rigid extension of the tip, a
        # chain segment with zero DOF, never a joint of its own.
        eff_pb.lock_ik_x = True
        eff_pb.lock_ik_y = True
        eff_pb.lock_ik_z = True

        for gid in lplan.driven_chain:
            pb = pose.bones.get(bone_names.get(gid, ""))
            if pb is None:
                warnings.append(
                    "loop {}: chain bone for group {} missing".format(
                        lplan.loop.id, gid))
                continue
            _configure_chain_bone(pb, plan.bone_by_group[gid].joint,
                                  lplan.loop.planar)
        count += 1
    return count, warnings
