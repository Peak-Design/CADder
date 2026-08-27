# SPDX-License-Identifier: GPL-3.0-or-later
"""Coupling annotations -> scripted drivers on bone channels.

Every driver reads exactly one TRANSFORMS variable targeting the armature
object with bone_target set, transform_space LOCAL_SPACE. The ratio is
baked into the expression as a literal: use_self would be the alternative
and it is blocked whenever Auto Run Python Scripts is off, which is the
default on every machine the rig gets shared to.

The screw self-driver (rotation from own location) is safe because the
depsgraph resolves per channel. Location never reads rotation back.
"""

import math

try:
    import bpy
except ImportError:
    bpy = None

from .manifest import Manifest


def _remove_driver(pose_bone, path, index):
    try:
        pose_bone.driver_remove(path, index)
    except (TypeError, RuntimeError):
        pass


def _add_driver(arm_obj, pose_bone, path, source_bone, transform_type, constant,
                index=1):
    _remove_driver(pose_bone, path, index)
    fcurve = pose_bone.driver_add(path, index)
    driver = fcurve.driver
    driver.type = "SCRIPTED"
    while driver.variables:
        driver.variables.remove(driver.variables[0])
    var = driver.variables.new()
    var.name = "a"
    var.type = "TRANSFORMS"
    target = var.targets[0]
    target.id = arm_obj
    target.bone_target = source_bone
    target.transform_type = transform_type
    target.transform_space = "LOCAL_SPACE"
    driver.expression = "a * {}".format(repr(float(constant)))
    return fcurve


def build(arm_obj, manifest: Manifest, plan, bone_names, unit_scale=1.0, context=None):
    """Creates every coupling driver. bone_names maps group id -> actual
    bone name (Blender may have renamed on collision, so plan names are not
    trusted). unit_scale is Blender units per metre. Returns (count,
    warnings)."""
    warnings = []
    count = 0
    pose = arm_obj.pose

    for joint in manifest.joints:
        c = joint.coupling
        if c is None:
            continue
        own_group = plan.joint_group.get(joint.id)
        if own_group is None:
            warnings.append(
                "joint {}: coupling skipped, the joint is not a tree edge".format(joint.id))
            continue
        own_pb = pose.bones.get(bone_names[own_group])
        if own_pb is None:
            warnings.append(
                "joint {}: coupling skipped, bone for group {} missing".format(
                    joint.id, own_group))
            continue

        if c.kind == "screw":
            if not c.lead_m_per_rev:
                warnings.append(
                    "joint {}: screw coupling without lead_m_per_rev".format(joint.id))
                continue
            # rotation [rad] = location [m] * 2*pi / lead. Location arrives
            # in Blender units, hence the divide by unit_scale.
            constant = (2.0 * math.pi) / (c.lead_m_per_rev * unit_scale)
            _add_driver(arm_obj, own_pb, "rotation_euler",
                        own_pb.name, "LOC_Y", constant)
            count += 1
            continue

        driver_group = plan.joint_group.get(c.driver_joint or "")
        if driver_group is None:
            warnings.append(
                "joint {}: {} coupling skipped, driver joint {!r} is not a "
                "tree edge".format(joint.id, c.kind, c.driver_joint))
            continue
        source_bone = bone_names[driver_group]

        if c.kind == "mirror":
            # rig_build rested BOTH bones with the SAME plane-aligned
            # orientation (local +Y = the mirror normal) at mirrored
            # positions, which reduces the reflection to per-channel sign
            # flips: conjugating a rotation by an improper transform gives
            # R(S·a, −angle), so with S mapping Y to −Y the channels that
            # NEGATE are location y and euler x and z, and location x, z and
            # euler y come through unchanged.
            #
            # How many of the six get drivers is what mirror_scope says.
            #
            # "plane": a symmetric MATE between two planar faces. That is a
            # plane-to-plane relation and it constrains exactly three degrees
            # of freedom: the translation along the normal and the two
            # rotations that tilt it, which are precisely the three that
            # negate. The other three are what the relation leaves free and
            # they must stay INDEPENDENT: driving them would weld the pair
            # into one rigid mirror image, so one block could not be raised
            # without the other (live corpus 14 sym4, 2026-08-24: SolidWorks
            # allows exactly that independence).
            #
            # "rigid": an assembly MIRROR FEATURE. There the instance IS a
            # full reflection of its source, so all six follow and the pair
            # really is one rigid mirror image.
            specs = (
                ("location", 1, "LOC_Y", -1.0),
                ("rotation_euler", 0, "ROT_X", -1.0),
                ("rotation_euler", 2, "ROT_Z", -1.0),
            )
            if c.mirror_scope == "rigid":
                specs += (
                    ("location", 0, "LOC_X", 1.0),
                    ("location", 2, "LOC_Z", 1.0),
                    ("rotation_euler", 1, "ROT_Y", 1.0),
                )
            for path, idx, ttype, sign in specs:
                _add_driver(arm_obj, own_pb, path, source_bone, ttype, sign,
                            index=idx)
            count += 1
            continue

        if c.kind == "gear":
            if c.ratio is None:
                warnings.append("joint {}: gear coupling without ratio".format(joint.id))
                continue
            _add_driver(arm_obj, own_pb, "rotation_euler",
                        source_bone, "ROT_Y", c.ratio)
            count += 1
        elif c.kind == "rack_pinion":
            if c.meters_per_radian is None:
                warnings.append(
                    "joint {}: rack_pinion coupling without meters_per_radian".format(joint.id))
                continue
            _add_driver(arm_obj, own_pb, "location",
                        source_bone, "ROT_Y", c.meters_per_radian * unit_scale)
            count += 1
        elif c.kind == "linear_coupler":
            if c.ratio is None:
                warnings.append(
                    "joint {}: linear_coupler coupling without ratio".format(joint.id))
                continue
            # m/m: source and driven channel share units, no scale term.
            _add_driver(arm_obj, own_pb, "location",
                        source_bone, "LOC_Y", c.ratio)
            count += 1
        else:
            warnings.append(
                "joint {}: unknown coupling kind {!r}".format(joint.id, c.kind))

    # Fresh drivers do not evaluate until the armature is tagged and the
    # view layer re-evaluates. Without this the rig looks dead until the
    # first user interaction.
    if count:
        arm_obj.data.update_tag()
        ctx = context if context is not None else bpy.context
        ctx.view_layer.update()
    return count, warnings
