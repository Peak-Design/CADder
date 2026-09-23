# SPDX-License-Identifier: GPL-3.0-or-later
"""Armature construction from a RigPlan, in three strict phases.

Phase 1 (Object mode) creates datablocks, Phase 2 is ONE Edit-mode session
that creates every bone, Phase 3 (Pose mode) configures channels and
constraints. The phases never interleave: an EditBone reference is dead the
moment Edit mode ends, and touching one crashes Blender rather than raising.

Bone convention: local +Y is the joint DOF axis, +Z comes from the
manifest's secondary_axis so roll is deterministic across exports.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List

try:
    import bpy
    from mathutils import Matrix, Vector
except ImportError:
    bpy = None
    Matrix = None
    Vector = None

from . import cam_contact, constraints
from . import shapes as shapes_mod, drivers, loops, sliders
from .graph import RigPlan, swing_cone

_RIG_NAME_FALLBACK = "SW_Rig"
# Blender 4 replaced the 32 bone layers with named bone collections, which
# is what these are. Four of them, by what a bone IS to the user:
#
#   controls   the bones with a real degree of freedom to pose. Custom shapes.
#   limits     the fixed dials and rails that show how far a control may go.
#   mechanism  bones that move but that the user does not drive - a driven
#              half of a coupling, a half of an aim pair, a welded body.
#   helpers    pure scaffolding: aim duplicates, IK targets, cone poles.
#
# Only controls and limits are visible when the rig is built. The rest exist
# and work. They just do not clutter the viewport with things that cannot be
# grabbed.
_HELPERS_COLLECTION = "SW_helpers"
_CONTROLS_COLLECTION = "SW_controls"
_LIMITS_COLLECTION = "SW_limits"
_MECHANISM_COLLECTION = "SW_mechanism"

# Blender's stock bone color sets, so they follow the user's theme.
_COLOUR_CONTROL = "THEME01"     # red
_COLOUR_LIMIT = "THEME09"       # yellow
_COLOUR_MECHANISM = "THEME03"   # green
_COLOUR_HELPER = "THEME04"      # blue


def _rig_name(manifest) -> str:
    """One rig per assembly: the STEP file's base name keys the collection
    and armature, so building a second assembly's rig sits beside the first
    instead of replacing it."""
    try:
        base = os.path.splitext(os.path.basename(manifest.step_file or ""))[0]
    except (AttributeError, TypeError):
        base = ""
    return base + "_Rig" if base else _RIG_NAME_FALLBACK
_HELPER_LENGTH_M = 0.02
_DEFAULT_BBOX_DIAG_M = 0.4
# Width of a path joint's shrinkwrap ribbon: the bone can land up to half of
# it off the true path, so it buys accuracy at the cost of nothing but
# visibility, which _make_path_rail restores by drawing the ribbon as wire.
_RAIL_WIDTH = 2e-5
# How far a path joint's rest point may sit off its own sampled curve and
# still be treated as sampling error rather than bad data.
_RAIL_SNAP = 1e-3

# A rig the user has locked. Everything that would replace or remove a rig
# asks first: a rig that has been edited by hand is work the CAD
# application cannot send again, so a send replaces the geometry and
# leaves the rig standing (Oscar, 2026-09-16).
LOCK_TAG = "RIG_locked"


class RigLocked(Exception):
    """Raised instead of replacing a rig the user locked."""


def is_locked(arm_obj):
    return bool(arm_obj is not None and arm_obj.get(LOCK_TAG))


def locked_rig(context=None):
    """The locked rig standing in the scene, or None.

    Looks at the scene rather than at one collection: a locked rig moved
    somewhere else in the outliner is still locked."""
    if bpy is None:
        return None
    try:
        objects = (context or bpy.context).scene.objects
    except AttributeError:
        objects = bpy.data.objects
    for obj in objects:
        if obj.type == "ARMATURE" and obj.get("RIG_rig") and is_locked(obj):
            return obj
    return None


@dataclass
class BuildResult:
    armature_object: object = None
    collection: object = None
    bone_names: Dict[str, str] = field(default_factory=dict)     # group id -> bone name
    helper_names: Dict[str, str] = field(default_factory=dict)   # loop id -> bone name
    # (loop id, "a"|"c") -> bone name, for the slider-crank aim pairs.
    aim_names: Dict[tuple, str] = field(default_factory=dict)
    effector_names: Dict[str, str] = field(default_factory=dict)  # loop id -> bone name
    tangent_helper_names: Dict[str, str] = field(default_factory=dict)  # group id -> bone name
    limit_names: Dict[str, str] = field(default_factory=dict)   # group id -> limit bone name
    slide_names: Dict[str, str] = field(default_factory=dict)   # group id -> stretch bone name
    # group id of a SCREW body -> the hidden bone that carries its TURN,
    # a child of the body's own bone. The geometry rides it, and so does
    # anything joined to the screw about the screw's own axis. See
    # graph.py BonePlan.spin_name.
    spin_names: Dict[str, str] = field(default_factory=dict)
    contact_mesh_names: Dict[str, str] = field(default_factory=dict)  # joint id -> rail or patch object name
    # Swing-cone balls AND cone_spin collapses: bone_names[gid] is the
    # hidden DEF bone (geometry and child bones ride the clamped result).
    # The visible user handle is here.
    ball_ctrl_names: Dict[str, str] = field(default_factory=dict)   # group id -> bone name
    ball_pole_names: Dict[str, str] = field(default_factory=dict)
    ball_goal_names: Dict[str, str] = field(default_factory=dict)
    cone_frame_names: Dict[str, str] = field(default_factory=dict)  # cone_spin plane frame
    # Cam contacts (cam_contact.py), by the follower's group id: the
    # projection bone with its (side, margin) pair, the relative and offset
    # bones of a flat face, and by joint id the cam surface object.
    cam_prj_names: Dict[str, str] = field(default_factory=dict)
    cam_prj_params: Dict[str, tuple] = field(default_factory=dict)
    cam_rel_names: Dict[str, str] = field(default_factory=dict)
    cam_off_names: Dict[str, str] = field(default_factory=dict)
    cam_surface_names: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


def _unit_scale(context):
    try:
        scale = float(context.scene.unit_settings.scale_length)
    except (AttributeError, TypeError):
        return 1.0
    # Blender units per metre. The manifest is metres everywhere.
    return 1.0 / scale if scale > 0.0 else 1.0


def _normalized(v):
    vec = Vector(v)
    if vec.length < 1e-9:
        return None
    return vec.normalized()


def _frame_matrix(y_axis, secondary, translation):
    """Columns x, y, z with y the DOF axis. Z from the secondary axis
    orthogonalised against y, falling back to global Z then X when the
    secondary is missing or parallel."""
    y = _normalized(y_axis)
    if y is None:
        y = Vector((0.0, 1.0, 0.0))
    z = None
    for candidate in (secondary, (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)):
        if candidate is None:
            continue
        c = Vector(candidate)
        ortho = c - y * c.dot(y)
        if ortho.length > 1e-6:
            z = ortho.normalized()
            break
    x = y.cross(z)
    t = Vector(translation)
    return Matrix((
        (x[0], y[0], z[0], t[0]),
        (x[1], y[1], z[1], t[1]),
        (x[2], y[2], z[2], t[2]),
        (0.0, 0.0, 0.0, 1.0),
    ))


def _mat4_to_matrix(rows, unit_scale):
    m = Matrix((tuple(rows[0]), tuple(rows[1]), tuple(rows[2]), tuple(rows[3])))
    for i in range(3):
        m[i][3] *= unit_scale
    return m


def _group_fallback_translation(manifest, group):
    comp_by_id = manifest.component_by_id()
    for cid in group.components:
        comp = comp_by_id.get(cid)
        if comp is not None:
            t = comp.transform
            return (t[0][3], t[1][3], t[2][3])
    return (0.0, 0.0, 0.0)


def _group_world_matrix(manifest, group, unit_scale):
    if group.frame is not None:
        return _mat4_to_matrix(group.frame, unit_scale)
    t = _group_fallback_translation(manifest, group)
    m = Matrix.Identity(4)
    for i in range(3):
        m[i][3] = t[i] * unit_scale
    return m


def _bone_rest_matrix(manifest, bone_plan, unit_scale):
    if bone_plan.root:
        # The root rests AT the assembly origin, world-aligned. A grounded
        # group's manifest frame is whichever member the exporter anchored
        # it to (live 2026-08-23: the flexible-sub "baseplate" bone landed
        # on the hinge boss), for static geometry the origin is the one
        # placement that never surprises.
        return Matrix.Identity(4)
    joint = bone_plan.joint
    collapsed = bone_plan.collapsed
    if collapsed is not None:
        # Contact-aligned frame so the channel locks mean the contact:
        #   planar_spin: Y = spin axis, Z = the contact plane normal
        #     (lock loc Z = stay on the plane. Rot X locked = no tilt).
        #   planar_ball / slide_ball: Y = the carrier's axis (plane normal /
        #     slide direction): same convention as planar and prismatic.
        #   orbit_spin: Y = spin axis, secondary as exported.
        j1 = collapsed.carrier_joint
        j2 = collapsed.spin_joint
        origin = j2.origin if j2.origin is not None else \
            _group_fallback_translation(manifest, bone_plan.group)
        if collapsed.kind in ("planar_spin", "cone_spin"):
            m = _frame_matrix(j2.axis, j1.axis, origin)
        elif collapsed.kind == "orbit_spin":
            m = _frame_matrix(j2.axis, j2.secondary_axis, origin)
        else:  # planar_ball, slide_ball
            m = _frame_matrix(j1.axis, j1.secondary_axis, origin)
        for i in range(3):
            m[i][3] *= unit_scale
        return m
    if bone_plan.mirror_normal is not None:
        # Half of a mirror pair: BOTH bones rest with the SAME plane-aligned
        # orientation (local +Y = the mirror normal) at their own mirrored
        # positions. Equal rest axes make the reflection exact per channel:
        # loc y negates, euler x and z negate, the rest come through
        # unchanged, and those three negating channels are exactly the ones
        # the symmetry constrains, so they are the only ones driven.
        origin = _group_fallback_translation(manifest, bone_plan.group)
        m = _frame_matrix(bone_plan.mirror_normal, None, origin)
        for i in range(3):
            m[i][3] *= unit_scale
        return m
    if bone_plan.aim_at is not None and joint is not None \
            and joint.origin is not None:
        # Half of a slider-crank. The bone rests pointing AT the other half's
        # pivot, because that is the axis Damped Track will aim: a bone
        # resting along its own joint axis would have Blender swing the PIN
        # round to face the target instead (live ClampRig, 2026-08-24:
        # the ram bones rested along their vertical pivots and pointed
        # straight up). It also lays the two halves along the ram, which is
        # what the machine looks like.
        #
        # Local Z is the bone's own PIN, exactly, not merely near it. A body
        # on a pin can turn about that pin and nothing else, which is a Locked
        # Track about local Z (sliders.py). Locking an axis that is only
        # approximately the pin lets the ram swing off it by the same angle.
        # So the aim direction is projected onto the plane the pin turns in
        # before it becomes local Y. Where the pin already stands square to
        # the ram (every ram on live ClampRig), the projection changes
        # nothing and +Y still lies exactly along the ram.
        origin = joint.origin
        toward = Vector((bone_plan.aim_at[0] - origin[0],
                         bone_plan.aim_at[1] - origin[1],
                         bone_plan.aim_at[2] - origin[2]))
        pin = None if joint.axis is None else _normalized(joint.axis)
        in_plane = toward
        if pin is not None:
            flat = toward - pin * toward.dot(pin)
            # A target straight up the pin leaves nothing to aim: keep the
            # unprojected direction and let sliders.py fall back with it.
            if flat.length > 1e-9:
                in_plane = flat
        if in_plane.length > 1e-9:
            m = _frame_matrix(list(in_plane.normalized()), joint.axis, origin)
            for i in range(3):
                m[i][3] *= unit_scale
            return m
    if swing_cone(joint):
        # The bone points along the child-side measured direction (the stud
        # axis), NOT along joint.axis, for a ball the axis is the
        # parent-fixed CONE axis the limit band is measured from. Roll from
        # the cone axis, so the frame is deterministic. At a rest inside the
        # cone the two may be parallel and _frame_matrix falls back.
        origin = joint.origin if joint.origin is not None else \
            _group_fallback_translation(manifest, bone_plan.group)
        m = _frame_matrix(joint.secondary_axis, joint.axis, origin)
        for i in range(3):
            m[i][3] *= unit_scale
        return m
    if joint is not None and joint.axis is not None:
        origin = joint.origin if joint.origin is not None else \
            _group_fallback_translation(manifest, bone_plan.group)
        m = _frame_matrix(joint.axis, joint.secondary_axis, origin)
        for i in range(3):
            m[i][3] *= unit_scale
        return m
    if joint is not None and joint.origin is not None:
        # A ball has no axis, but a world-aligned bone LIES about the pose:
        # the stud tilted 45° in SolidWorks still grew a vertical bone (live
        # corpus 04, 2026-08-23). The child part's own rotation is rigid
        # with the geometry, so the bone tilts exactly as the part does:
        # whatever local axis the part was modelled along.
        m = _group_component_rotation(manifest, bone_plan.group)
        for i in range(3):
            m[i][3] = joint.origin[i] * unit_scale
        return m
    return _group_world_matrix(manifest, bone_plan.group, unit_scale)


def _group_component_rotation(manifest, group):
    """The rotation of the group's first component, orthonormalized, as a
    4x4 with zero translation. Identity when the group owns no components
    (a carrier) or the transform is degenerate."""
    comp_by_id = manifest.component_by_id()
    for cid in group.components:
        comp = comp_by_id.get(cid)
        if comp is None:
            continue
        t = comp.transform
        x = Vector((t[0][0], t[1][0], t[2][0]))
        y = Vector((t[0][1], t[1][1], t[2][1]))
        if x.length < 1e-9 or y.length < 1e-9:
            break
        x.normalize()
        z = x.cross(y)
        if z.length < 1e-9:
            break
        z.normalize()
        y = z.cross(x)
        m = Matrix.Identity(4)
        for i in range(3):
            m[i][0], m[i][1], m[i][2] = x[i], y[i], z[i]
        return m
    return Matrix.Identity(4)


def _limit_widget_wanted(bone_plan):
    """Whether this bone earns a limit dial of its own.

    Only a bone the user can actually pose: a dial on a body that cannot be
    grabbed says "you may turn this far" about something that does not turn
    at all. The ball templates carry their limit as a swing cone on the
    handle instead, and are handled there.
    """
    if bone_plan.root or bone_plan.ball_def_name:
        return False
    joint = bone_plan.joint
    if joint is None:
        return False
    if joint.coupling is not None and joint.coupling.driver_joint:
        return False                 # driven: the driver's dial is the one
    if bone_plan.aim_at is not None:
        return False                 # the aim closure owns the orientation
    return joint.rotation_limit is not None or joint.translation_limit is not None


def _ik_driven_groups(plan):
    """Groups whose bones are placed by a loop closure rather than by hand.

    A closed loop is solved with IK on the driven side: those bones take the
    pose the solver gives them, and dragging one only fights it back. Only
    the loop's own driver is worth grabbing: live corpus 06 (2026-08-25):
    the four-bar offered two red bones and the second one did nothing.
    """
    driven = set()
    for lplan in getattr(plan, "loops", None) or []:
        driven.update(lplan.driven_chain or [])
        if lplan.ik_tip_group:
            driven.add(lplan.ik_tip_group)
    return driven


def _bone_role(bone_plan, pose_bone, ik_driven=()):
    """What this bone IS to the user, decided from the kinematics and the
    channel locks that follow from them: never from a name.

    The root is NOT special-cased into a control. A grounded group is locked
    on purpose (constraints.lock_all), so it cannot be posed, and offering it
    as something to grab would be a lie. The whole assembly is moved by its
    armature object instead.

    Nor is a driven half of a coupling special-cased any more. What decides
    is what is left UNLOCKED: a gear or mirror-feature follower has every
    channel written by its driver and is mechanism, but a plane-symmetry
    follower still slides in the plane and spins about its normal ON ITS
    OWN, and a bone with real freedom of its own is a control whatever else
    it also follows (live corpus 14 sym4, 2026-08-25).
    """
    if bone_plan.group.id in ik_driven:
        return "mechanism"
    if bone_plan.aim_at is not None:
        return "mechanism"
    try:
        welded = all(pose_bone.lock_location) and all(pose_bone.lock_rotation)
    except (AttributeError, TypeError):
        welded = False
    return "mechanism" if welded else "control"


def _widget_kind(bone_plan):
    """The motion this bone stands for, as a joint-type name.

    A collapsed contact poses on ONE bone, so its carrier, not the pair of
    joints behind it: says what the motion really is.
    """
    collapsed = bone_plan.collapsed
    if collapsed is not None:
        return {"planar_spin": "planar", "planar_ball": "planar",
                # A vertex riding a line or a face, tumbling freely, is a
                # POINT, not a barrel that slides and spins about its own
                # axis. The marker has to claim only what the contact is.
                "slide_ball": "point",
                "orbit_spin": "revolute",
                "cone_spin": "revolute"}.get(collapsed.kind)
    return bone_plan.joint.type if bone_plan.joint is not None else None


# All as fractions of the bone's own length, so a big part gets a big dial.
# The limit arc rings the dial from OUTSIDE rather than fighting it for the
# same pixels, and the dial's pointer reaches almost to it: rim ends at
# 0.5175, the point reaches 0.6075, the band starts at 0.61. They come close
# and never intersect, which matters because they are coplanar: an overlap
# would be z-fighting, not a drawing.
#
# The widths are written as a thickness divided by a radius because that is
# what they are for: the radii shrank, the bands did not.
_DIAL_RADIUS = 0.45
_DIAL_WIDTH = 0.0675 / _DIAL_RADIUS
_DIAL_POINTER = 0.35
_ARC_RADIUS = 0.70
_ARC_WIDTH = 0.09 / _ARC_RADIUS
# A slide bar is one bone length, centered on the bone's own origin, so its
# rail runs half a bone length past each stop. The rail is slimmer than the
# bar so it still reads when the bar is sitting on it.
_SLIDE_LENGTH = 1.0
_SLIDE_HALF_WIDTH = 0.225
_RAIL_HALF_WIDTH = 0.10
# A planar contact wears the dial with thickness: it spins about the plane
# normal exactly as a revolute does, and extruded it also reads as the disc
# lying on the face.
_PLANAR_RADIUS = 0.5
_PLANAR_THICKNESS = 0.14
# A ball, its stud, and the screw's wire.
_BALL_RADIUS = 0.35
_BALL_STUB = 1.0
_SCREW_RADIUS = 0.22
_SCREW_THREAD = 0.055
_POINT_SIZE = 0.4


def _control_geometry(bone_plan):
    """The widget for a control bone, as (cache key, geometry). Unit sized:
    the bone's own length scales it, so one mesh serves every joint of a
    kind and a big part gets a big dial."""
    if bone_plan.root:
        return "SWW_ground", shapes_mod.ground_cross(1.0)

    kind = _widget_kind(bone_plan)

    if kind == "revolute":
        return "SWW_dial", shapes_mod.ring_with_pointer(
            _DIAL_RADIUS, width=_DIAL_WIDTH, pointer=_DIAL_POINTER)
    if kind == "cylindrical":
        # Round section: it slides AND turns about the same axis.
        return "SWW_cylinder", shapes_mod.cylinder(_SLIDE_LENGTH,
                                                   _SLIDE_HALF_WIDTH)
    if kind == "prismatic":
        # Square section: a corner would show a spin, and there is none.
        return "SWW_slider", shapes_mod.cuboid(_SLIDE_LENGTH,
                                               _SLIDE_HALF_WIDTH)
    if kind == "screw":
        return "SWW_screw", shapes_mod.helix(
            _SLIDE_LENGTH, _SCREW_RADIUS, 2.0, thread=_SCREW_THREAD)
    if kind == "planar":
        return "SWW_planar", shapes_mod.disc_with_pointer(
            _PLANAR_RADIUS, _PLANAR_THICKNESS, pointer=_DIAL_POINTER,
            width=_DIAL_WIDTH)
    if kind == "pin_slot":
        return "SWW_pinslot", shapes_mod.slot(_SLIDE_LENGTH, _DIAL_RADIUS)
    if kind == "ball":
        return "SWW_ball", shapes_mod.ball_with_stub(_BALL_RADIUS, _BALL_STUB)
    if kind in ("point", "path", "surface", "free"):
        return "SWW_point", shapes_mod.diamond(_POINT_SIZE)
    return None, None


def _style_bones(context, arm_obj, plan, result, unit_scale,
                 controls_coll, limits_coll, mechanism_coll, helpers_coll):
    """Color every bone by what it is, and give the ones worth grabbing a
    widget that says what they do.

    Runs last, in Pose mode, because the classification reads the channel
    locks the constraint pass has just set: "nothing is unlocked" is the
    honest test for a body the user cannot move, and it follows from the
    joint rather than from any name.
    """
    pose = arm_obj.pose
    scene_col = result.collection or context.scene.collection
    widgets = shapes_mod.widget_collection(scene_col)
    ik_driven = _ik_driven_groups(plan)
    cache = {}
    styled = {"control": 0, "limit": 0, "mechanism": 0}

    def paint(pose_bone, palette, collection):
        try:
            pose_bone.color.palette = palette
            pose_bone.bone.color.palette = palette
        except (AttributeError, TypeError):
            pass
        for existing in list(pose_bone.bone.collections):
            existing.unassign(pose_bone.bone)
        collection.assign(pose_bone.bone)

    for bp in plan.bones:
        gid = bp.group.id
        # For a swing-cone ball or a cone_spin the user handle is the ctrl
        # bone. bone_names[gid] is the clamped DEF that geometry rides.
        handle_name = result.ball_ctrl_names.get(gid, result.bone_names[gid])
        handle = pose.bones.get(handle_name)
        if handle is None:
            continue
        if handle_name != result.bone_names[gid]:
            deformer = pose.bones.get(result.bone_names[gid])
            if deformer is not None:
                paint(deformer, _COLOUR_MECHANISM, mechanism_coll)
                styled["mechanism"] += 1

        role = _bone_role(bp, handle, ik_driven)
        if role != "control":
            paint(handle, _COLOUR_MECHANISM, mechanism_coll)
            styled["mechanism"] += 1
            continue

        paint(handle, _COLOUR_CONTROL, controls_coll)
        styled["control"] += 1
        key, geometry = _control_geometry(bp)
        if geometry is not None:
            handle.custom_shape = shapes_mod.widget(widgets, key, geometry, cache)
            handle.use_custom_shape_bone_size = True

        # A swing-cone ball wears its limit as the cone it may lean in.
        joint = bp.joint
        if bp.ball_def_name and joint is not None \
                and joint.rotation_limit is not None:
            swing = max(abs(joint.rotation_limit.delta_min),
                        abs(joint.rotation_limit.delta_max))
            lim_key = "SWW_cone_%.4f" % swing
            handle.custom_shape = shapes_mod.widget(
                widgets, lim_key,
                shapes_mod.swing_cone(swing, 1.0), cache)

    # The limit dials and rails.
    for gid, name in result.limit_names.items():
        lb = pose.bones.get(name)
        if lb is None:
            continue
        bp = plan.bone_by_group.get(gid)
        joint = bp.joint if bp is not None else None
        if joint is None:
            continue
        paint(lb, _COLOUR_LIMIT, limits_coll)
        lb.lock_location = [True, True, True]
        lb.lock_rotation = [True, True, True]
        lb.lock_scale = [True, True, True]
        styled["limit"] += 1

        if joint.rotation_limit is not None:
            lo = joint.rotation_limit.delta_min
            hi = joint.rotation_limit.delta_max
            key = "SWW_arc_%.4f_%.4f" % (lo, hi)
            # An angle is an angle whatever the part's size, so the bone's
            # own length may scale the dial freely.
            lb.custom_shape = shapes_mod.widget(
                widgets, key,
                shapes_mod.limit_arc(lo, hi, _ARC_RADIUS, width=_ARC_WIDTH),
                cache)
            lb.use_custom_shape_bone_size = True
        elif joint.translation_limit is not None:
            lo = joint.translation_limit.delta_min * unit_scale
            hi = joint.translation_limit.delta_max * unit_scale
            # The limit is a limit on the slide's ORIGIN, and the slide bar
            # is centered on that origin, so the rail runs half a bar past
            # each end. Otherwise the bar hangs half off the rail exactly
            # when it is hard against the stop, which is the one moment the
            # rail has to be right.
            span = _bone_length(bp.group, unit_scale)
            pad = span * _SLIDE_LENGTH * 0.5
            round_section = _widget_kind(bp) in ("cylindrical", "screw",
                                                 "pin_slot")
            key = "SWW_stroke_%.6f_%.6f_%.6f%s" % (
                lo, hi, pad, "_r" if round_section else "")
            # TRUE length, so bone-size scaling is switched off: this rail
            # is a measurement, and a rail that is not the stroke's length
            # is worse than no rail.
            lb.custom_shape = shapes_mod.widget(
                widgets, key,
                shapes_mod.stroke_bar(lo, hi,
                                      max(1e-4, span * _RAIL_HALF_WIDTH),
                                      pad=pad, round_section=round_section),
                cache)
            lb.use_custom_shape_bone_size = False

    # Everything the build already called a helper stays one.
    for name in (list(result.helper_names.values())
                 + [n for n in result.aim_names.values()]
                 + list(result.effector_names.values())
                 + list(result.tangent_helper_names.values())
                 + list(result.ball_pole_names.values())
                 + list(result.ball_goal_names.values())
                 + list(result.cone_frame_names.values())):
        pb = pose.bones.get(name)
        if pb is not None:
            paint(pb, _COLOUR_HELPER, helpers_coll)

    # Only what can be grabbed, and what says how far it may go.
    helpers_coll.is_visible = False
    mechanism_coll.is_visible = False
    controls_coll.is_visible = True
    limits_coll.is_visible = True
    shapes_mod.exclude_widgets(context.view_layer)
    return styled


def _bone_length(group, unit_scale):
    diag = group.bbox_diag if group.bbox_diag else _DEFAULT_BBOX_DIAG_M
    return max(0.01, min(1.0, diag / 4.0)) * unit_scale


def _ensure_object_mode(context):
    """Object mode is PER-OBJECT since 2.8: context.mode reports only the
    active object, so a background armature can still sit in Pose mode,
    and deleting it (the rig rebuild does) freezes Blender (live
    2026-08-23, send-while-posing). Sweep every object's own mode."""
    view_layer = context.view_layer
    prev = view_layer.objects.active
    for obj in list(view_layer.objects):
        try:
            if obj.mode == "OBJECT":
                continue
            view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="OBJECT")
        except (RuntimeError, ReferenceError):
            continue
    try:
        if prev is not None:
            view_layer.objects.active = prev
    except (RuntimeError, ReferenceError):
        pass


def _driven_objects(manifest):
    """The scene objects matching has tagged for this manifest: the ones
    relink will attach to bones."""
    ids = {c.id for c in manifest.components}
    gids = {g.id for g in manifest.rigid_groups}
    out = []
    for obj in bpy.data.objects:
        if obj.get("RIG_rig") or obj.get("RIG_helper") or obj.get("CADLINK_widget"):
            continue
        if (obj.get("RIG_component_id") in ids
                or obj.get("RIG_component_of") in ids
                or obj.get("RIG_group") in gids):
            out.append(obj)
    return out


def _collection_paths(scene):
    """Every collection under the scene, as its path from the scene root.

    Linked into two places, a collection has two paths. The shallowest wins,
    which is the one a user thinks of as where it lives.
    """
    paths = {}
    stack = [(scene.collection, [scene.collection.name])]
    while stack:
        col, path = stack.pop()
        known = paths.get(col.name)
        if known is not None and len(known) <= len(path):
            continue
        paths[col.name] = path
        for child in col.children:
            stack.append((child, path + [child.name]))
    return paths


def _prototype_collections():
    """Collections some object instances: the hidden templates of the
    collection-instance import mode, one part each. Not a place for a rig."""
    out = set()
    for obj in bpy.data.objects:
        col = getattr(obj, "instance_collection", None)
        if col is not None:
            out.add(col.name)
            for child in col.children_recursive:
                out.add(child.name)
    return out


def _usable_home(context, col, prototypes):
    """Whether a rig can live in this collection: the view layer must reach
    it, with no excluded collection above it, and it must not be a template.

    The collection-instance import parks every prototype in a
    "<file>.components" collection and EXCLUDES it from the view layer; with
    the import dropped at the scene root that collection is the import's
    only top-level one, so it was chosen as the rig's home, and the build
    then failed to make the armature active because the view layer did not
    contain it (live TongRig in "Collection instances" mode, 2026-09-14).
    """
    if col.name in prototypes:
        return False

    def walk(lc, excluded):
        excluded = excluded or lc.exclude
        if lc.collection == col:
            return not excluded
        for child in lc.children:
            found = walk(child, excluded)
            if found is not None:
                return found
        return None

    return bool(walk(context.view_layer.layer_collection, False))


def _rig_home(context, manifest):
    """The collection the rig belongs in: the one that holds the geometry it
    drives.

    A rig parked at the scene root beside its assembly cannot be hidden with
    it: the collection switch hides the parts and leaves the bones floating,
    or hides the bones and leaves the parts (live, 2026-08-25). Inside the
    import's own collection, one switch takes the whole machine.

    Preference order: the single top collection the import made (what the
    user calls "the collection I imported into"), then the nearest collection
    that contains every driven object, then the scene root. Only a
    collection the view layer reaches is ever chosen (see _usable_home).
    """
    scene = context.scene
    driven = _driven_objects(manifest)
    if not driven:
        return scene.collection
    prototypes = _prototype_collections()

    files = {obj.get("STEP_file") for obj in driven}
    files.discard(None)
    if len(files) == 1:
        try:
            from .. import refresh as refresh_mod
            roots = refresh_mod.owned_roots(next(iter(files)))
        except Exception:                       # not importable, or no bpy
            roots = []
        paths = _collection_paths(scene)
        roots = [c for c in roots
                 if c.name in paths and _usable_home(context, c, prototypes)]
        if len(roots) == 1:
            return roots[0]

    # No import collection to speak of: fall back to the deepest collection
    # every driven object sits under.
    paths = _collection_paths(scene)
    common = None
    for obj in driven:
        for col in obj.users_collection:
            path = paths.get(col.name)
            if path is None:                    # linked outside this scene
                return scene.collection
            if common is None:
                common = list(path)
                continue
            keep = 0
            while (keep < len(common) and keep < len(path)
                   and common[keep] == path[keep]):
                keep += 1
            common = common[:keep]
        if common is not None and len(common) <= 1:
            break                               # already down to the root
    if not common or len(common) <= 1:
        return scene.collection
    # The deepest usable collection on that path: an excluded or template
    # collection is skipped in favour of the one above it.
    for name in reversed(common[1:]):
        col = bpy.data.collections.get(name)
        if col is not None and _usable_home(context, col, prototypes):
            return col
    return scene.collection


def _place_rig_collection(context, manifest, rig_name):
    """The rig's own collection, put where the assembly it drives lives.

    An existing one is left where it is if the user has moved it somewhere
    deliberate. Only the default parking spot (loose at the scene root)
    is re-homed, so old scenes gain the fix on their next rebuild without
    overriding anyone's arrangement.
    """
    scene_col = context.scene.collection
    home = _rig_home(context, manifest)
    collection = bpy.data.collections.get(rig_name)
    if collection is None or collection.name not in _collection_paths(context.scene):
        collection = bpy.data.collections.new(rig_name)
        home.children.link(collection)
        return collection

    # A user who dragged geometry into the rig collection could make the
    # home the rig collection itself, or something inside it. Linking a
    # collection into its own descendant is a cycle Blender refuses.
    if home.name == collection.name or home.name in {
            c.name for c in collection.children_recursive}:
        return collection
    parents = [c for c in [scene_col] + list(bpy.data.collections)
               if collection.name in [x.name for x in c.children]]
    if [c.name for c in parents] == [scene_col.name] and home is not scene_col:
        scene_col.children.unlink(collection)
        home.children.link(collection)
    return collection


def _remove_previous_rig(collection):
    """Re-runs replace the rig instead of stacking name.001s. Geometry
    parented into the old rig is released with its world transform kept, so
    a rebuild never scatters the scene.

    The old rig is put back to its rest pose first. Its rest pose is the
    CAD pose the manifest describes, and the new rig takes the geometry
    where it finds it as ITS rest: released mid-pose, a cam turned to 90
    degrees became the new rig's zero and every follower was out of time
    with it (live cam-follower, 2026-09-15, switching the input)."""
    doomed = [o for o in list(collection.objects)
              if o.get("RIG_rig") and not is_locked(o)]
    for obj in doomed:
        if obj.type == "ARMATURE":
            rest_pose(obj)
    _remove_rig_objects(doomed)


def rest_pose(arm_obj):
    """Puts every bone of a rig back to its rest pose, which is the CAD
    pose the manifest describes. True when a bone had to move."""
    posed = False
    for pb in arm_obj.pose.bones:
        if pb.matrix_basis != Matrix.Identity(4):
            pb.matrix_basis = Matrix.Identity(4)
            posed = True
    if posed:
        try:
            bpy.context.view_layer.update()
        except (AttributeError, RuntimeError):
            pass
    return posed


def _bone_collection(arm_data, name):
    """A bone collection by name, made once. A rebuild into a standing
    armature would otherwise stack a second "Helpers" beside the first."""
    existing = arm_data.collections.get(name)
    return existing if existing is not None else arm_data.collections.new(name)


def _rig_collection_of(arm_obj):
    """The collection a standing rig lives in, so a rebuild into it does
    not move it somewhere else."""
    scene_roots = {s.collection.name for s in bpy.data.scenes}
    for col in arm_obj.users_collection:
        if col.name not in scene_roots and not col.get("CADLINK_widgets"):
            return col
    return None


# Every bone a build makes carries RIG_generated. The others are the tags
# of rigs built before it: a group, a helper, a joint and a spin bone.
_GENERATED_TAGS = ("RIG_generated", "RIG_group", "RIG_helper", "RIG_joint",
                   "RIG_spin")


def _clear_generated(context, arm_obj, collection):
    """Everything the last build made, out of a rig that is standing: its
    bones, the drivers on them, and the helper objects beside it. A bone
    with no tag of ours was put there by the user and stays, with whatever
    they hung on it."""
    _ensure_object_mode(context)
    generated = set()
    for pb in arm_obj.pose.bones:
        keys = pb.keys()
        if any(k in keys for k in _GENERATED_TAGS):
            generated.add(pb.name)
    # A limit dial of a rig built before RIG_generated carries no tag at
    # all. It is in the limits collection, and nothing else is.
    limits = arm_obj.data.collections.get(_LIMITS_COLLECTION)
    if limits is not None:
        generated.update(b.name for b in limits.bones)
    animation = arm_obj.animation_data
    if animation is not None:
        for fcurve in list(animation.drivers):
            name = _bone_in_path(fcurve.data_path)
            if name is None or name in generated:
                try:
                    animation.drivers.remove(fcurve)
                except (RuntimeError, ReferenceError):
                    pass
    for pb in arm_obj.pose.bones:
        if pb.name in generated:
            for constraint in list(pb.constraints):
                pb.constraints.remove(constraint)
    if generated:
        context.view_layer.objects.active = arm_obj
        arm_obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            for name in generated:
                eb = arm_obj.data.edit_bones.get(name)
                if eb is not None:
                    arm_obj.data.edit_bones.remove(eb)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
    # The rails, cam surfaces and group empties of the last build. The
    # armature itself stays, which is the whole point.
    doomed = [o for o in list(collection.objects)
              if o.get("RIG_rig") and o is not arm_obj]
    _remove_rig_objects(doomed)


def _bone_in_path(path):
    """The bone a driver's data path names, or None for a path that is not
    about a bone."""
    if not path or 'pose.bones[' not in path:
        return None
    start = path.index('pose.bones[') + len('pose.bones[')
    quote = path[start:start + 1]
    if quote not in ('"', "'"):
        return None
    end = path.find(quote, start + 1)
    return None if end < 0 else path[start + 1:end]


def _remove_rig_objects(doomed):
    doomed_set = set(doomed)
    if not doomed:
        return
    for obj in bpy.data.objects:
        if obj.parent in doomed_set and obj not in doomed_set:
            world = obj.matrix_world.copy()
            obj.parent = None
            obj.matrix_world = world
    for obj in doomed:
        data = obj.data
        bpy.data.objects.remove(obj)
        if data is not None and data.users == 0:
            if isinstance(data, bpy.types.Armature):
                bpy.data.armatures.remove(data)
            elif isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)


def remove_rig(arm_obj):
    """Removes one rig outright: the armature, the helpers and path curves
    that share its collection, and that collection once it is empty. Bone
    widgets stay: they live in one collection shared by every rig.
    Geometry still hanging from a bone is released where it stands.

    For a rig whose every driven part has gone. The direct link replaces
    the whole native import, so a different assembly sent into the same
    session left the previous assembly's rig standing over nothing (live
    2026-09-14).

    A locked rig is kept, and False says so: the user asked for it to
    outlive its geometry."""
    if is_locked(arm_obj):
        return False
    scene_root_names = {s.collection.name for s in bpy.data.scenes}
    cols = [c for c in arm_obj.users_collection
            if c.name not in scene_root_names and not c.get("CADLINK_widgets")]
    doomed = {arm_obj.name: arm_obj}
    for col in cols:
        for obj in col.objects:
            if obj.get("RIG_rig") and not obj.get("CADLINK_widget"):
                doomed[obj.name] = obj
    # A rig parked loose at the scene root: its helpers are the RIG_rig
    # objects that hang from the armature itself.
    for obj in bpy.data.objects:
        if obj.get("RIG_rig") and obj.parent is arm_obj:
            doomed[obj.name] = obj
    _remove_rig_objects(list(doomed.values()))
    home = bpy.context.scene.collection
    for col in cols:
        try:
            if col.objects:
                continue
            # The widget collection sits under the rig collection: it is
            # shared, so it moves up rather than going with the rig.
            for child in list(col.children):
                col.children.unlink(child)
                if child.name not in {c.name for c in home.children}:
                    home.children.link(child)
            bpy.data.collections.remove(col)
        except (ReferenceError, RuntimeError):
            pass
    return True


def _thread_through(pts, rest, tolerance):
    """Inserts the joint's rest point into a sampled polyline so the path
    passes exactly through it.

    The manifest asserts two things that must agree: the curve's samples
    and where the mate holds the part, and a chord-sampled curve misses
    its own vertex by the sagitta. Since the rest pose is what the user
    sees on load, it wins, but only within `tolerance`: a rest point
    genuinely off the path is a data problem the relink warning should
    still report, not something to bend the geometry around.

    _pin_through does the same for a triangulated face."""
    best = (tolerance, -1, 0.0)
    for i in range(len(pts) - 1):
        span = pts[i + 1] - pts[i]
        if span.length_squared < 1e-18:
            continue
        t = (rest - pts[i]).dot(span) / span.length_squared
        if not 1e-6 < t < 1.0 - 1e-6:
            continue           # at a vertex: inserting would spur the rail
        d = (rest - (pts[i] + span * t)).length
        if d < best[0]:
            best = (d, i, t)
    if best[1] >= 0 and best[0] > 1e-9:
        pts.insert(best[1] + 1, rest)


def _make_path_rail(collection, joint, frame, unit_scale):
    """The surface a path joint's bone rides: a hairline RIBBON through the
    manifest's sampled points, placed through the same scene frame as the
    bones.

    Blender has no nearest-point-on-curve constraint, and Clamp To is not
    one: it maps the bone's coordinate along a single axis linearly into
    curve parameter, so a bone resting exactly ON the curve is teleported
    somewhere else along it and drags its geometry with it (live corpus 17,
    2026-08-23: 26 mm along an 0.88 m spline). Shrinkwrap NEAREST_SURFACE
    IS nearest-point, which makes the rest pose a fixed point of the
    constraint while a dragged bone still slides along the path.

    Two constraints on the shape follow: the ribbon must have FACES (the
    shrinkwrap BVH ignores loose edges and the constraint then silently
    does nothing), and the rails must stay a hair apart so every point of
    the surface is within half a width of the true path.

    Tagged RIG_rig (replaced on rebuild) and RIG_helper (invisible to
    matching and geometry parenting)."""
    pts = [frame @ Vector((p[0] * unit_scale, p[1] * unit_scale,
                           p[2] * unit_scale))
           for p in joint.path_points]
    if joint.origin is not None:
        _thread_through(pts, frame @ Vector((joint.origin[0] * unit_scale,
                                             joint.origin[1] * unit_scale,
                                             joint.origin[2] * unit_scale)),
                        _RAIL_SNAP * unit_scale)
    half = _RAIL_WIDTH * unit_scale * 0.5
    verts = []
    offset = None
    for i, p in enumerate(pts):
        tangent = pts[i + 1] - p if i < len(pts) - 1 else p - pts[i - 1]
        if tangent.length < 1e-12:
            tangent = Vector((0.0, 0.0, 1.0))
        tangent.normalize()
        if offset is None:
            offset = tangent.orthogonal().normalized()
        else:
            # Parallel transport rather than a fresh perpendicular per point:
            # an abrupt flip would fold the quad into a bow tie whose BVH
            # entry straddles the path.
            offset = offset - tangent * offset.dot(tangent)
            offset = (offset.normalized() if offset.length > 1e-9
                      else tangent.orthogonal().normalized())
        verts.append(tuple(p - offset * half))
        verts.append(tuple(p + offset * half))
    faces = [(2 * i, 2 * i + 1, 2 * i + 3, 2 * i + 2)
             for i in range(len(pts) - 1)]
    if joint.path_closed:
        faces.append((2 * len(pts) - 2, 2 * len(pts) - 1, 1, 0))

    data = bpy.data.meshes.new("CADLINK_path_" + joint.id)
    data.from_pydata(verts, [], faces)
    data.update()
    obj = bpy.data.objects.new(data.name, data)
    obj["RIG_rig"] = True
    obj["RIG_helper"] = joint.id
    obj.hide_render = True
    # A ribbon this thin has no readable solid shading. Drawn as wire it
    # reads as the path line it stands for.
    obj.display_type = "WIRE"
    collection.objects.link(obj)
    return obj


def _pin_through(verts, faces, rest, tolerance):
    """The 2-D twin of _thread_through: fans the triangle under the joint's
    rest point into three through the point itself, so a tessellated face
    passes exactly through the contact its mate defines. Same reasoning and
    the same tolerance gate: a chordal face misses its own mate vertex by
    the sagitta, and the rest pose is what the user sees on load."""
    best = (tolerance, -1)
    for i, tri in enumerate(faces):
        a, b, c = (verts[k] for k in tri)
        normal = (b - a).cross(c - a)
        if normal.length_squared < 1e-24:
            continue
        normal = normal.normalized()
        planar = rest - normal * (rest - a).dot(normal)
        # Inside test by the sign of each edge's cross product against the
        # face normal: a point outside the triangle belongs to a neighbour.
        inside = True
        for p, q in ((a, b), (b, c), (c, a)):
            if (q - p).cross(planar - p).dot(normal) < -1e-12:
                inside = False
                break
        if not inside:
            continue
        d = (rest - planar).length
        if d < best[0]:
            best = (d, i)
    if best[1] < 0 or best[0] <= 1e-9:
        return
    a, b, c = faces.pop(best[1])
    at = len(verts)
    verts.append(rest)
    faces.extend([(a, b, at), (b, c, at), (c, a, at)])


def _make_surface_patch(collection, joint, frame, unit_scale):
    """The face a surface joint's bone rides: the manifest's triangulation,
    placed through the same scene frame as the bones.

    Same machinery as _make_path_rail one dimension up: a shrinkwrap onto
    a real mesh, because SolidWorks will mate a point to a torus, a fillet
    or a loft, and none of those has a joint type to be. The patch is the
    face alone, not the whole body, so the point cannot wander onto a
    neighbouring face the mate never mentioned."""
    verts = [frame @ Vector((p[0] * unit_scale, p[1] * unit_scale,
                             p[2] * unit_scale))
             for p in joint.surface_points]
    faces = [tuple(t) for t in joint.surface_triangles]
    if joint.origin is not None:
        _pin_through(verts, faces,
                     frame @ Vector((joint.origin[0] * unit_scale,
                                     joint.origin[1] * unit_scale,
                                     joint.origin[2] * unit_scale)),
                     _RAIL_SNAP * unit_scale)

    data = bpy.data.meshes.new("CADLINK_surface_" + joint.id)
    data.from_pydata([tuple(v) for v in verts], [], faces)
    data.update()
    obj = bpy.data.objects.new(data.name, data)
    obj["RIG_rig"] = True
    obj["RIG_helper"] = joint.id
    obj.hide_render = True
    # Wire, like the path rail: the patch is a duplicate of a face that is
    # already in the scene, and a solid copy would z-fight with it.
    obj.display_type = "WIRE"
    collection.objects.link(obj)
    return obj


def build(context, manifest, plan: RigPlan, frame_rows=None, into=None) -> BuildResult:
    """Builds the armature, constraints, drivers and loop closures.
    plan already passed the dependency pre-flight in graph.build: nothing
    here is allowed to create a depsgraph cycle. Geometry is not required. The rig builds identically on an empty scene.

    frame_rows is the scene-frame transform from matching (manifest
    coordinates in Blender units -> where the geometry actually sits, e.g.
    the rotation a Y-up STEP import applied, cursor offset included). Every
    bone is placed through it, so the rig lands on the geometry whatever up
    axis the import used. Without a frame (no match run, or nothing to
    anchor one) the rig lands at the 3D cursor, like the STEP import itself
    does: never silently at the world origin. Limits and drivers are
    bone-local and need no adjustment.

    `into` is an armature to rebuild INSIDE, rather than a new one to make:
    the object, its action, its drivers and any bones the user added are
    kept, and only the bones this addon generated are replaced. With the
    plan's keep_names holding the names those bones already had, a keyframe
    still names the bone it was written for."""
    result = BuildResult()
    result.warnings.extend(plan.warnings)
    unit_scale = _unit_scale(context)
    if frame_rows:
        frame = Matrix([tuple(r) for r in frame_rows])
    else:
        try:
            frame = Matrix.Translation(context.scene.cursor.location)
        except (AttributeError, TypeError):
            frame = Matrix.Identity(4)

    # ---- Phase 1: Object mode, datablocks only -------------------------
    # Before anything is created or removed: a locked rig is not rebuilt,
    # and a half-built one is worse than none.
    standing = locked_rig(context)
    if standing is not None:
        raise RigLocked(
            "%s is locked. Unlock it to build the rig again." % standing.name)
    _ensure_object_mode(context)

    rig_name = _rig_name(manifest)
    if into is not None and getattr(into, "type", None) == "ARMATURE":
        # Into the armature that is already there. The action, the drivers
        # the user wrote, the bones they added and everything keyed against
        # a bone NAME live on this object, so keeping it is what lets an
        # update keep an animation. Only what this addon generated goes.
        arm_obj = into
        arm_data = arm_obj.data
        collection = _rig_collection_of(arm_obj)             or _place_rig_collection(context, manifest, rig_name)
        _clear_generated(context, arm_obj, collection)
    else:
        collection = _place_rig_collection(context, manifest, rig_name)
        _remove_previous_rig(collection)

        # ops.armature_add would depend on cursor, context overrides and the
        # active collection. Direct datablock creation depends on nothing.
        arm_data = bpy.data.armatures.new(rig_name)
        arm_obj = bpy.data.objects.new(rig_name, arm_data)
        collection.objects.link(arm_obj)
    arm_obj["RIG_rig"] = True
    arm_obj["RIG_source"] = manifest.source_path or ""
    arm_obj["RIG_frame"] = [v for row in frame for v in row]
    arm_obj.show_in_front = True
    # What is left in the armature now is the user's own. Every bone added
    # below is this build's, and says so, so the next rebuild in place
    # takes all of them out and not only the ones it has a name for.
    users_bones = {b.name for b in arm_data.bones}
    result.armature_object = arm_obj
    result.collection = collection

    helpers_coll = _bone_collection(arm_data, _HELPERS_COLLECTION)
    controls_coll = _bone_collection(arm_data, _CONTROLS_COLLECTION)
    limits_coll = _bone_collection(arm_data, _LIMITS_COLLECTION)
    mechanism_coll = _bone_collection(arm_data, _MECHANISM_COLLECTION)

    # ---- Phase 2: one Edit-mode session, every bone --------------------
    context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = arm_data.edit_bones
        for bp in plan.bones:
            eb = edit_bones.new(bp.bone_name)
            # A zero-length edit bone is silently deleted on mode exit, so
            # the bone gets a provisional tail before the matrix.
            eb.head = (0.0, 0.0, 0.0)
            eb.tail = (0.0, 1.0, 0.0)
            eb.matrix = frame @ _bone_rest_matrix(manifest, bp, unit_scale)
            eb.length = _bone_length(bp.group, unit_scale)
            # Connected bones ignore Limit Location entirely: a prismatic
            # joint dies silently, so no bone is ever connected, except a
            # slide the loop solver owns (below).
            eb.use_connect = False
            parent_name = None
            if bp.parent_group_id is not None:
                parent_name = result.bone_names[bp.parent_group_id]
                if bp.parent_spin:
                    # Joined about the screw's OWN axis, or welded to it:
                    # this body turns with the screw, so it rides the bone
                    # that turns. See graph.py BonePlan.spin_name.
                    parent_name = result.spin_names.get(
                        bp.parent_group_id, parent_name)
            if bp.slide_name and bp.parent_group_id is not None:
                # A slide inside a solved chain (graph.py BonePlan.slide_name):
                # a hidden stretch bone lies along this bone's own +Y, its
                # tail on this bone's head. IK scales the stretch bone, and
                # this bone, connected and inheriting no scale, rides the
                # tail along the slide by the exact difference. Its own
                # location is then no longer anyone's to pose.
                sb = edit_bones.new(bp.slide_name)
                sb.head = (0.0, 0.0, 0.0)
                sb.tail = (0.0, 1.0, 0.0)
                rest_len = bp.slide_rest_length * unit_scale
                m = eb.matrix.copy()
                m.translation = m.translation - (m.to_3x3() @ Vector((0.0, 1.0, 0.0))) * rest_len
                sb.matrix = m
                sb.length = rest_len
                sb.use_connect = False
                sb.parent = edit_bones[parent_name]
                helpers_coll.assign(sb)
                result.slide_names[bp.group.id] = sb.name
                eb.parent = sb
                eb.use_connect = True
                eb.inherit_scale = "NONE"
            elif bp.parent_group_id is not None:
                eb.parent = edit_bones[parent_name]
            result.bone_names[bp.group.id] = eb.name
            if bp.spin_name:
                # A screw's TURN, on a bone of its own: a child of the body,
                # resting exactly where the body rests. Its driver reads the
                # slide off its PARENT, which is an ordinary dependency
                # rather than the cycle a bone driving itself makes, and it
                # reads that slide after the joint's travel limit. Geometry
                # rides this bone (parenting.py follows RIG_spin); children
                # ride the body's own bone unless they turn with it.
                sp = edit_bones.new(bp.spin_name)
                sp.head = (0.0, 0.0, 0.0)
                sp.tail = (0.0, 1.0, 0.0)
                sp.matrix = eb.matrix.copy()
                sp.length = eb.length
                sp.use_connect = False
                sp.parent = eb
                helpers_coll.assign(sp)
                result.spin_names[bp.group.id] = sp.name
            if (bp.cam_prj_name or bp.cam_rel_name) and cam_contact.wanted(bp):
                cam_contact.add_helper_bones(
                    edit_bones, eb, bp, plan, manifest, frame, unit_scale,
                    result, helpers_coll)

            # A limit gets a bone of its own, at the same place and the same
            # rest orientation, parented to the control's PARENT and never to
            # the control: the dial has to stay still while the pointer moves
            # over it, and a rail has to stay put while the slide runs along
            # it. A solver-owned slide has no travel to dial.
            if _limit_widget_wanted(bp) and not bp.slide_name:
                lb = edit_bones.new("LIM_" + eb.name)
                lb.head = (0.0, 0.0, 0.0)
                lb.tail = (0.0, 1.0, 0.0)
                lb.matrix = eb.matrix.copy()
                lb.length = eb.length
                lb.use_connect = False
                lb.parent = eb.parent
                limits_coll.assign(lb)
                result.limit_names[bp.group.id] = lb.name

            if bp.ball_def_name:
                # Swing-cone ball: the bone above is the USER HANDLE (free
                # rotation, no clamps). Geometry and child bones ride DEF,
                # which equals the handle inside the cone and is minimally
                # corrected onto the cone surface beyond it (constraints in
                # Phase 3). POLE anchors the cone axis at one handle-length
                # from the center. GOAL rests at the handle's tail. All three
                # created BEFORE any child bone looks up bone_names[gid], so
                # children parent to DEF, never to the unclamped handle.
                result.ball_ctrl_names[bp.group.id] = eb.name
                parent_eb = eb.parent
                ctrl_matrix = eb.matrix.copy()
                ctrl_length = eb.length
                joint = bp.joint
                head = ctrl_matrix.translation.copy()
                cone_axis = (frame.to_3x3() @ Vector(joint.axis)).normalized()

                db = edit_bones.new(bp.ball_def_name)
                db.head = (0.0, 0.0, 0.0)
                db.tail = (0.0, 1.0, 0.0)
                db.matrix = ctrl_matrix
                db.length = ctrl_length
                db.use_connect = False
                db.parent = parent_eb
                helpers_coll.assign(db)
                result.bone_names[bp.group.id] = db.name

                pb_ = edit_bones.new(bp.ball_pole_name)
                pb_.head = (0.0, 0.0, 0.0)
                pb_.tail = (0.0, 1.0, 0.0)
                m = _frame_matrix(cone_axis, None, head + cone_axis * ctrl_length)
                pb_.matrix = m
                pb_.length = _HELPER_LENGTH_M * unit_scale
                pb_.use_connect = False
                pb_.parent = parent_eb
                helpers_coll.assign(pb_)
                result.ball_pole_names[bp.group.id] = pb_.name

                gb = edit_bones.new(bp.ball_goal_name)
                gb.head = (0.0, 0.0, 0.0)
                gb.tail = (0.0, 1.0, 0.0)
                m = ctrl_matrix.copy()
                ctrl_y = ctrl_matrix.col[1].to_3d().normalized()
                for i in range(3):
                    m[i][3] = head[i] + ctrl_y[i] * ctrl_length
                gb.matrix = m
                gb.length = _HELPER_LENGTH_M * unit_scale
                gb.use_connect = False
                gb.parent = parent_eb
                helpers_coll.assign(gb)
                result.ball_goal_names[bp.group.id] = gb.name

            c = bp.collapsed
            if c is not None and c.kind == "cone_spin":
                # Tangent cone on ONE grabbable bone (live corpus 15 cone3,
                # 2026-08-23): the bone above is the HANDLE. It slides on
                # the plane and rotates freely. DEF carries geometry and
                # children, following the handle with its axis clamped onto
                # the fixed-tilt ring (the ball template with a degenerate
                # band). POLE anchors the ring and RIDES the handle through
                # a local-space location copy: its rest orientation must
                # equal the handle's so the local channels map 1:1. FRM is
                # the STATIC plane frame the handle's on-plane clamp
                # measures in (a follower frame would cycle the depsgraph).
                result.ball_ctrl_names[bp.group.id] = eb.name
                parent_eb = eb.parent
                ctrl_matrix = eb.matrix.copy()
                ctrl_length = eb.length
                head = ctrl_matrix.translation.copy()
                normal = (frame.to_3x3()
                          @ Vector(c.carrier_joint.axis)).normalized()

                db = edit_bones.new(c.def_name)
                db.head = (0.0, 0.0, 0.0)
                db.tail = (0.0, 1.0, 0.0)
                db.matrix = ctrl_matrix
                db.length = ctrl_length
                db.use_connect = False
                db.parent = parent_eb
                helpers_coll.assign(db)
                result.bone_names[bp.group.id] = db.name

                fb = edit_bones.new(c.frame_name)
                fb.head = (0.0, 0.0, 0.0)
                fb.tail = (0.0, 1.0, 0.0)
                fb.matrix = _frame_matrix(normal, None, head)
                fb.length = _HELPER_LENGTH_M * unit_scale
                fb.use_connect = False
                fb.parent = parent_eb
                helpers_coll.assign(fb)
                result.cone_frame_names[bp.group.id] = fb.name

                pb_ = edit_bones.new(c.pole_name)
                pb_.head = (0.0, 0.0, 0.0)
                pb_.tail = (0.0, 1.0, 0.0)
                m = ctrl_matrix.copy()
                for i in range(3):
                    m[i][3] = head[i] + normal[i] * ctrl_length
                pb_.matrix = m
                pb_.length = _HELPER_LENGTH_M * unit_scale
                pb_.use_connect = False
                pb_.parent = parent_eb
                helpers_coll.assign(pb_)
                result.ball_pole_names[bp.group.id] = pb_.name

                gb = edit_bones.new(c.goal_name)
                gb.head = (0.0, 0.0, 0.0)
                gb.tail = (0.0, 1.0, 0.0)
                m = ctrl_matrix.copy()
                ctrl_y = ctrl_matrix.col[1].to_3d().normalized()
                for i in range(3):
                    m[i][3] = head[i] + ctrl_y[i] * ctrl_length
                gb.matrix = m
                gb.length = _HELPER_LENGTH_M * unit_scale
                gb.use_connect = False
                gb.parent = parent_eb
                helpers_coll.assign(gb)
                result.ball_goal_names[bp.group.id] = gb.name

        # Orbit-contact targets: a hidden bone at the orbit center, riding
        # the parent body: the child bone's Limit Distance holds the
        # tangency radius against it.
        for bp in plan.bones:
            c = bp.collapsed
            if c is None or c.kind != "orbit_spin" or not c.helper_name:
                continue
            m = _frame_matrix(c.orbit_axis, None, c.orbit_center)
            for i in range(3):
                m[i][3] *= unit_scale
            eb = edit_bones.new(c.helper_name)
            eb.head = (0.0, 0.0, 0.0)
            eb.tail = (0.0, 1.0, 0.0)
            eb.matrix = frame @ m
            eb.length = _HELPER_LENGTH_M * unit_scale
            eb.use_connect = False
            if bp.parent_group_id is not None:
                eb.parent = edit_bones[result.bone_names[bp.parent_group_id]]
            helpers_coll.assign(eb)
            result.tangent_helper_names[bp.group.id] = eb.name

        for lplan in plan.loops:
            cj = lplan.closure_joint
            origin = cj.origin
            if origin is None:
                origin = _group_fallback_translation(
                    manifest, plan.bone_by_group[lplan.ik_tip_group].group)
            if cj.axis is not None:
                m = _frame_matrix(cj.axis, cj.secondary_axis, origin)
                for i in range(3):
                    m[i][3] *= unit_scale
            else:
                m = Matrix.Identity(4)
                for i in range(3):
                    m[i][3] = origin[i] * unit_scale
            eb = edit_bones.new(lplan.helper_name)
            eb.head = (0.0, 0.0, 0.0)
            eb.tail = (0.0, 1.0, 0.0)
            eb.matrix = frame @ m
            eb.length = _HELPER_LENGTH_M * unit_scale
            eb.use_connect = False
            # A helper marks a POINT on the driver-side body, so on a
            # screw it rides the turning half: a point off the screw's axis
            # really does swing round it once per turn.
            eb.parent = edit_bones[
                result.spin_names.get(lplan.helper_parent_group)
                or result.bone_names[lplan.helper_parent_group]]
            helpers_coll.assign(eb)
            result.helper_names[lplan.loop.id] = eb.name

            # The effector carries the closure point rigidly on the driven
            # tip: its TAIL sits exactly at the closure origin, because the
            # tip bone's own tail is NOT the closure point, with bones along
            # the hinge axes a tail cannot even move in the mechanism plane,
            # and aiming IK at it left the solve dead (live corpus 06). The
            # tail, not the head: IK's use_tail=False actually re-targets the
            # owner's PARENT's tail, which is only the owner's head for
            # connected bones, so loops.py keeps use_tail on.
            eb = edit_bones.new(lplan.effector_name)
            eb.head = (0.0, 0.0, 0.0)
            eb.tail = (0.0, 1.0, 0.0)
            eb.matrix = frame @ m
            eb.length = _HELPER_LENGTH_M * unit_scale
            eb.translate(eb.head - eb.tail)   # land the tail on the closure
            eb.use_connect = False
            eb.parent = edit_bones[result.bone_names[lplan.ik_tip_group]]
            helpers_coll.assign(eb)
            result.effector_names[lplan.loop.id] = eb.name

        for splan in plan.sliders:
            # One duplicate per half, each carrying the OTHER half's pivot and
            # parented to that half's PARENT: never to the half itself, or
            # the two Damped Tracks would depend on each other.
            for tag, pivot, aim_parent in (
                    ("a", splan.c_pivot, splan.a_aim_parent),
                    ("c", splan.a_pivot, splan.c_aim_parent)):
                name = splan.a_aim_name if tag == "a" else splan.c_aim_name
                eb = edit_bones.new(name)
                eb.head = (0.0, 0.0, 0.0)
                eb.tail = (0.0, 1.0, 0.0)
                m = Matrix.Identity(4)
                for i in range(3):
                    m[i][3] = pivot[i] * unit_scale
                eb.matrix = frame @ m
                eb.length = _HELPER_LENGTH_M * unit_scale
                eb.use_connect = False
                eb.parent = edit_bones[result.bone_names[aim_parent]]
                helpers_coll.assign(eb)
                result.aim_names[(splan.loop.id, tag)] = eb.name
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")

    helpers_coll.is_visible = False

    # The bone the assembly stands on, named on the armature so relink can
    # find it with no session state: anything the rig does not drive is
    # hung off it rather than left behind in world space.
    for bp in plan.bones:
        if bp.root and bp.group.id in result.bone_names:
            arm_obj["RIG_ground_bone"] = result.bone_names[bp.group.id]
            break

    # Rails and patches are plain objects (Object mode work) and must exist
    # before Phase 3 hands them to the shrinkwrap constraints.
    contact_meshes = {}
    for bp in plan.bones:
        if bp.joint is None:
            continue
        obj = None
        if bp.joint.type == "path" and bp.joint.path_points:
            obj = _make_path_rail(collection, bp.joint, frame, unit_scale)
        elif bp.joint.type == "surface" and bp.joint.surface_triangles:
            obj = _make_surface_patch(collection, bp.joint, frame, unit_scale)
        if obj is not None:
            contact_meshes[bp.joint.id] = obj
            result.contact_mesh_names[bp.joint.id] = obj.name
    cam_surfaces = cam_contact.make_surfaces(
        collection, arm_obj, manifest, plan, result, frame, unit_scale)

    # ---- Phase 3: Pose mode, with channels, constraints, drivers and IK --------
    bpy.ops.object.mode_set(mode="POSE")
    try:
        pose = arm_obj.pose
        # rotation_mode before anything else: the Limit Rotation euler_order
        # is copied from it, and switching it later re-interprets any value a
        # constraint or driver already wrote.
        for name in (list(result.bone_names.values())
                     + list(result.helper_names.values())
                     + list(result.effector_names.values())
                     + list(result.slide_names.values())
                     + list(result.spin_names.values())
                     + list(result.tangent_helper_names.values())
                     + list(result.ball_ctrl_names.values())
                     + list(result.ball_pole_names.values())
                     + list(result.ball_goal_names.values())
                     + list(result.cone_frame_names.values())
                     + list(result.cam_prj_names.values())
                     + list(result.cam_rel_names.values())
                     + list(result.cam_off_names.values())):
            pose.bones[name].rotation_mode = "YXZ"

        for pb in pose.bones:
            if pb.name not in users_bones:
                pb["RIG_generated"] = True

        source = manifest.source_path or ""
        for bp in plan.bones:
            pb = pose.bones[result.bone_names[bp.group.id]]
            pb["RIG_group"] = bp.group.id
            # WHICH manifest this bone came from. Group ids restart at g000
            # for every assembly, so once two rigs are joined into one
            # armature the id alone no longer names a bone: half the
            # geometry would re-parent to the other assembly's bones.
            pb["RIG_source"] = source
            if bp.ball_def_name:
                pb["RIG_joint"] = bp.joint.id
                ctrl_pb = pose.bones[result.ball_ctrl_names[bp.group.id]]
                # The handle has no group: the parts ride DEF. The joint and
                # the source name it, so an update keeps its name
                # (rig_update.snapshot).
                ctrl_pb["RIG_joint"] = bp.joint.id
                ctrl_pb["RIG_source"] = source
                constraints.apply_ball_cone(
                    arm_obj, pb, ctrl_pb,
                    pose.bones[result.ball_goal_names[bp.group.id]],
                    pose.bones[result.ball_pole_names[bp.group.id]],
                    bp.joint)
                continue
            if bp.collapsed is not None and bp.collapsed.kind == "cone_spin":
                pb["RIG_joint"] = bp.collapsed.spin_joint.id
                ctrl_pb = pose.bones[result.ball_ctrl_names[bp.group.id]]
                ctrl_pb["RIG_joint"] = bp.collapsed.spin_joint.id
                ctrl_pb["RIG_source"] = source
                constraints.apply_cone_spin(
                    arm_obj, pb, ctrl_pb,
                    pose.bones[result.ball_goal_names[bp.group.id]],
                    pose.bones[result.ball_pole_names[bp.group.id]],
                    pose.bones[result.cone_frame_names[bp.group.id]],
                    bp.collapsed.tilt, bp.collapsed.spin_joint.id)
                continue
            if bp.collapsed is not None:
                pb["RIG_joint"] = bp.collapsed.spin_joint.id
                target_name = result.tangent_helper_names.get(bp.group.id)
                constraints.apply_collapsed_contact(
                    pb, bp.collapsed, unit_scale,
                    orbit_target=arm_obj, orbit_subtarget=target_name)
                if target_name is not None:
                    hb = pose.bones[target_name]
                    hb["RIG_helper"] = bp.collapsed.spin_joint.id
                    hb.lock_location = [True, True, True]
                    hb.lock_rotation = [True, True, True]
                    hb.lock_scale = [True, True, True]
            elif bp.joint is not None:
                pb["RIG_joint"] = bp.joint.id
                constraints.apply_joint(
                    pb, bp.joint, unit_scale,
                    contact_mesh=contact_meshes.get(bp.joint.id),
                    aimed=bp.aim_at is not None)
                if (bp.joint.coupling is not None
                        and bp.joint.coupling.kind == "mirror"):
                    # Only the three channels a plane-to-plane symmetry
                    # actually constrains are driver-owned: the translation
                    # along the mirror normal (local Y) and the two rotations
                    # that tilt it. Locks stop the user posing those. the
                    # sign-flip drivers still animate them through the locks.
                    #
                    # The other three stay UNLOCKED on purpose: sliding
                    # within the plane and spinning about its normal are
                    # what the mate leaves free, and SolidWorks lets the two
                    # bodies do them independently.
                    #
                    # A mirror FEATURE is the other case: the instance is a
                    # full reflection of its source, every channel is
                    # driver-owned, and nothing here is posable.
                    if bp.joint.coupling.mirror_scope == "rigid":
                        pb.lock_location = [True, True, True]
                        pb.lock_rotation = [True, True, True]
                    else:
                        pb.lock_location = [False, True, False]
                        pb.lock_rotation = [True, False, True]
            elif bp.group.grounded:
                constraints.lock_all(pb)
            else:
                constraints.unlock_all(pb)

        for splan in plan.sliders:
            # The aim duplicates only ever move with their parents. posing
            # one by hand would quietly detune the closure.
            for tag in ("a", "c"):
                name = result.aim_names.get((splan.loop.id, tag))
                pb = pose.bones.get(name) if name else None
                if pb is None:
                    continue
                pb["RIG_helper"] = splan.loop.id
                pb.lock_location = [True, True, True]
                pb.lock_rotation = [True, True, True]
                pb.lock_scale = [True, True, True]

        for lplan in plan.loops:
            # Helper and effector only ever move with their parents.
            # Hand-posing either would silently detune the closure. Both
            # carry RIG_helper so parenting and matching skip them.
            for name in (result.helper_names[lplan.loop.id],
                         result.effector_names[lplan.loop.id]):
                pb = pose.bones[name]
                pb["RIG_helper"] = lplan.loop.id
                pb.lock_location = [True, True, True]
                pb.lock_rotation = [True, True, True]
                pb.lock_scale = [True, True, True]

        for gid, name in result.spin_names.items():
            # RIG_spin, not RIG_group: one bone per group answers to the
            # group id (joining and diff both key on it), and this bone is
            # only where the geometry of that group hangs.
            pb = pose.bones[name]
            pb["RIG_spin"] = gid
            pb["RIG_source"] = source
            pb.lock_location = [True, True, True]
            pb.lock_rotation = [True, True, True]
            pb.lock_scale = [True, True, True]

        for gid, name in result.slide_names.items():
            # The stretch bone belongs to the solver (loops.py sets its IK
            # stretch); the body on it is connected, so its own location is
            # not posable either, and the locks say so to the styling pass.
            pb = pose.bones[name]
            pb["RIG_helper"] = gid
            pb.lock_location = [True, True, True]
            pb.lock_rotation = [True, True, True]
            pb.lock_scale = [True, True, True]
            body = pose.bones.get(result.bone_names.get(gid, ""))
            if body is not None:
                body.lock_location = [True, True, True]

        n_drivers, drv_warnings = drivers.build(
            arm_obj, manifest, plan, result.bone_names,
            unit_scale=unit_scale, context=context,
            spin_names=result.spin_names)
        result.warnings.extend(drv_warnings)
        result.warnings.extend(cam_contact.apply(
            arm_obj, manifest, plan, result, cam_surfaces, unit_scale))

        n_sliders, slider_warnings = sliders.close_sliders(
            arm_obj, plan, result.bone_names, result.aim_names)
        result.warnings.extend(slider_warnings)
        n_loops, loop_warnings = loops.close_loops(
            arm_obj, plan, result.bone_names, result.helper_names,
            result.effector_names, result.slide_names)
        result.warnings.extend(loop_warnings)

        # Last, because it reads the channel locks the constraint pass has
        # just set: a bone with nothing unlocked is a body the user cannot
        # move, whatever its joint was called.
        styled = _style_bones(
            context, arm_obj, plan, result, unit_scale,
            controls_coll, limits_coll, mechanism_coll, helpers_coll)
        print("[CADLink rig] %d control(s), %d limit dial(s), %d mechanism bone(s)"
              % (styled["control"], styled["limit"], styled["mechanism"]))
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")

    # A rail or patch is geometry of the joint's PARENT side, when that body
    # moves, the slot the child follows moves with it. Same bone-parent
    # matrix math as parenting.py: BONE parenting evaluates at the tail, so
    # the parent inverse is set against it and matrix_basis restores the
    # world placement exactly.
    if contact_meshes:
        context.view_layer.update()
        for bp in plan.bones:
            if bp.joint is None or bp.joint.id not in contact_meshes:
                continue
            obj = contact_meshes[bp.joint.id]
            bone_name = result.bone_names.get(bp.joint.parent_group)
            if bone_name is None:
                continue
            world_before = obj.matrix_world.copy()
            bone = arm_obj.data.bones[bone_name]
            p = (arm_obj.matrix_world
                 @ bone.matrix_local
                 @ Matrix.Translation((0.0, bone.length, 0.0)))
            obj.parent = arm_obj
            obj.parent_type = "BONE"
            obj.parent_bone = bone_name
            obj.matrix_parent_inverse = p.inverted()
            obj.matrix_basis = world_before
        context.view_layer.update()

    return result
