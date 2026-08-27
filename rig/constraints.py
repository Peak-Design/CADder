# SPDX-License-Identifier: GPL-3.0-or-later
"""Joint type -> pose-bone locks and Limit constraints.

The bone's local +Y axis is the joint DOF axis, so every rule below speaks
in bone-local channels. Limits in the manifest are absolute mate values. The bone rest pose is the as-mated configuration, so the constraints get
the deltas (Limit.delta_min/delta_max), never the raw values.

owner_space is set to LOCAL explicitly on every constraint: the Blender
default is WORLD and a world-space limit on a child bone is silently wrong.
"""

import math

try:
    import bpy
except ImportError:
    bpy = None

from .manifest import Joint

_PREFIX = "SWTB "

# The channel a coupling drives is written by a driver, so its Limit
# constraint must not clamp the transform values back (use_transform_limit
# would fight the driver every frame).
_DRIVEN_CHANNEL = {
    "gear": "rotation",
    "screw": "rotation",
    "rack_pinion": "location",
    "linear_coupler": "location",
}


def driven_channel(joint: Joint):
    if joint.coupling is None:
        return None
    return _DRIVEN_CHANNEL.get(joint.coupling.kind)


def remove_rig_constraints(pose_bone):
    for con in list(pose_bone.constraints):
        if con.name.startswith(_PREFIX):
            pose_bone.constraints.remove(con)


def _new_limit_rotation(pose_bone, transform_limit):
    con = pose_bone.constraints.new("LIMIT_ROTATION")
    con.name = _PREFIX + "Limit Rotation"
    con.owner_space = "LOCAL"
    con.use_transform_limit = transform_limit
    # The constraint decomposes with its own euler order. Anything but the
    # bone's rotation_mode limits different angles than the ones posed.
    con.euler_order = pose_bone.rotation_mode
    con.use_legacy_behavior = False
    return con


def _new_limit_location(pose_bone, transform_limit):
    con = pose_bone.constraints.new("LIMIT_LOCATION")
    con.name = _PREFIX + "Limit Location"
    con.owner_space = "LOCAL"
    con.use_transform_limit = transform_limit
    return con


def _limit_rotation_y(pose_bone, limit, transform_limit):
    con = _new_limit_rotation(pose_bone, transform_limit)
    con.use_limit_y = True
    con.min_y = limit.delta_min
    con.max_y = limit.delta_max
    return con


def _limit_rotation_cone(pose_bone, limit, transform_limit):
    # Ball limits: the mate dimension is an UNSIGNED swing angle. The stud
    # may lean up to the max in ANY direction around the socket axis, so the
    # limit must be symmetric. Applying the raw deltas per axis pinned the
    # swing into one quadrant (live corpus 04, 2026-08-22: a 0..45 deg limit
    # resting at 0 could only reach one world-space quadrant and jittered
    # against the one-sided clamps). The symmetric amplitude on both swing
    # axes is the box approximation of the cone. Twist about Y stays free.
    amp = max(abs(limit.delta_min), abs(limit.delta_max))
    con = _new_limit_rotation(pose_bone, transform_limit)
    con.use_limit_x = True
    con.min_x = -amp
    con.max_x = amp
    con.use_limit_z = True
    con.min_z = -amp
    con.max_z = amp
    return con


def _limit_rotation_zero(pose_bone):
    con = _new_limit_rotation(pose_bone, True)
    con.use_limit_x = con.use_limit_y = con.use_limit_z = True
    con.min_x = con.max_x = 0.0
    con.min_y = con.max_y = 0.0
    con.min_z = con.max_z = 0.0
    return con


def _limit_location_y(pose_bone, limit, transform_limit, unit_scale):
    con = _new_limit_location(pose_bone, transform_limit)
    con.use_min_y = True
    con.use_max_y = True
    con.min_y = limit.delta_min * unit_scale
    con.max_y = limit.delta_max * unit_scale
    return con


def _limit_location_z(pose_bone, limit, transform_limit, unit_scale):
    con = _new_limit_location(pose_bone, transform_limit)
    con.use_min_z = True
    con.use_max_z = True
    con.min_z = limit.delta_min * unit_scale
    con.max_z = limit.delta_max * unit_scale
    return con


def _limit_location_xz(pose_bone, limit, transform_limit, unit_scale):
    con = _new_limit_location(pose_bone, transform_limit)
    con.use_min_x = con.use_max_x = True
    con.min_x = limit.delta_min * unit_scale
    con.max_x = limit.delta_max * unit_scale
    con.use_min_z = con.use_max_z = True
    con.min_z = limit.delta_min * unit_scale
    con.max_z = limit.delta_max * unit_scale
    return con


def _limit_location_zero(pose_bone):
    con = _new_limit_location(pose_bone, True)
    con.use_min_x = con.use_max_x = True
    con.use_min_y = con.use_max_y = True
    con.use_min_z = con.use_max_z = True
    con.min_x = con.max_x = 0.0
    con.min_y = con.max_y = 0.0
    con.min_z = con.max_z = 0.0
    return con


def apply_joint(pose_bone, joint: Joint, unit_scale=1.0, contact_mesh=None,
                aimed=False):
    """Configures one tree joint's bone. The caller has already set
    rotation_mode: the euler_order copy above depends on it. unit_scale is
    Blender units per metre. Angles need no conversion. contact_mesh is the
    geometry a path or surface joint's bone rides: a ribbon along the curve
    or the face's own triangles (rig_build creates it).

    Transform locks stop the user, not drivers: a driven channel still
    animates while locked, which is exactly what screw and coupled joints
    want (belt and braces against hand-posing the driven channel)."""
    remove_rig_constraints(pose_bone)

    lock_loc = [False, False, False]
    lock_rot = [False, False, False]
    driven = driven_channel(joint)
    rot_free = driven != "rotation"
    loc_free = driven != "location"

    if joint.type == "fixed":
        lock_loc = [True, True, True]
        lock_rot = [True, True, True]
        _limit_rotation_zero(pose_bone)
        _limit_location_zero(pose_bone)

    elif joint.type == "revolute":
        lock_loc = [True, True, True]
        # The DOF is local Y, unless a driver owns it, in which case the
        # lock is what stops a user posing a channel that will be written
        # back over on the next depsgraph pass.
        lock_rot = [True, not rot_free, True]
        if joint.rotation_limit is not None:
            _limit_rotation_y(pose_bone, joint.rotation_limit, rot_free)

    elif joint.type == "prismatic":
        lock_rot = [True, True, True]
        lock_loc = [True, not loc_free, True]
        if joint.translation_limit is not None:
            _limit_location_y(pose_bone, joint.translation_limit, loc_free, unit_scale)

    elif joint.type == "cylindrical":
        lock_rot = [True, not rot_free, True]
        lock_loc = [True, not loc_free, True]
        if joint.rotation_limit is not None:
            _limit_rotation_y(pose_bone, joint.rotation_limit, rot_free)
        if joint.translation_limit is not None:
            _limit_location_y(pose_bone, joint.translation_limit, loc_free, unit_scale)

    elif joint.type == "screw":
        # As prismatic. The rotation is owned by the self-driver in
        # drivers.py, so no rotation Limit constraint here at all.
        lock_rot = [True, True, True]
        lock_loc = [True, False, True]
        if joint.translation_limit is not None:
            _limit_location_y(pose_bone, joint.translation_limit, loc_free, unit_scale)

    elif joint.type == "ball":
        # A ball with a cone frame (axis + secondary set) never reaches
        # here: rig_build routes it to apply_ball_cone. The Euler box
        # below survives only for legacy manifests without the frame.
        lock_loc = [True, True, True]
        if joint.rotation_limit is not None:
            _limit_rotation_cone(pose_bone, joint.rotation_limit, rot_free)

    elif joint.type == "pin_slot":
        # Bone Y is the rotation axis. Bone Z is built from secondary_axis,
        # which for this type IS the slide direction (SCHEMA.md): spin about
        # Y, slide along Z, everything else locked.
        lock_loc = [True, True, False]
        lock_rot = [True, False, True]
        if joint.rotation_limit is not None:
            _limit_rotation_y(pose_bone, joint.rotation_limit, rot_free)
        if joint.translation_limit is not None:
            _limit_location_z(pose_bone, joint.translation_limit, loc_free, unit_scale)

    elif joint.type == "planar":
        # Bone Y is the plane normal: translation lives in local X/Z,
        # rotation only about the normal.
        lock_loc = [False, True, False]
        lock_rot = [True, False, True]
        if joint.rotation_limit is not None:
            _limit_rotation_y(pose_bone, joint.rotation_limit, rot_free)
        if joint.translation_limit is not None:
            _limit_location_xz(pose_bone, joint.translation_limit, loc_free, unit_scale)

    elif joint.type in ("path", "surface"):
        # A nearest-point shrinkwrap onto the path's ribbon (or the face's
        # patch) owns the position: wherever the user drags the bone, it
        # lands on the geometry, and a bone already on it stays put (see
        # _make_path_rail for why Clamp To cannot). Location must stay
        # unlocked for the drag to reach the constraint. Rotation stays free
        # for a path because the mate's pitch/yaw/roll options are not
        # readable from the API, for a surface because a point on a face
        # genuinely constrains no rotation at all.
        lock_loc = [False, False, False]
        lock_rot = [False, False, False]
        if contact_mesh is not None:
            con = pose_bone.constraints.new("SHRINKWRAP")
            con.name = _PREFIX + ("On Path" if joint.type == "path"
                                  else "On Surface")
            con.target = contact_mesh
            con.shrinkwrap_type = "NEAREST_SURFACE"
            con.distance = 0.0

    elif joint.type == "free":
        pass

    if aimed:
        # Half of a slider-crank. Its orientation belongs entirely to the aim
        # constraint sliders.py puts on it, which turns it about its own pin
        # and nothing else, so every rotation channel left open here is only a
        # way to break that by hand: a free one about local Y would ROLL the
        # ram along its own length, which no pin permits.
        #
        # A rotation Limit would clamp the wrong axis too: an aimed bone rests
        # with local Y along the RAM and its pin on local Z, where every other
        # joint measures its rotation about local Y. Nothing is lost with it:
        # this pin is stopped by the ram's own stroke at the far end of the
        # loop, which the slide still carries.
        for con in list(pose_bone.constraints):
            if con.type == "LIMIT_ROTATION" and con.name.startswith(_PREFIX):
                pose_bone.constraints.remove(con)
        lock_rot = [True, True, True]

    pose_bone.lock_location = lock_loc
    pose_bone.lock_rotation = lock_rot
    pose_bone.lock_scale = [True, True, True]


def _swing_band_chain(arm_obj, goal_pb, ctrl_pb, pole_pb, length,
                      band_min, band_max, rounds=3):
    """The GOAL clamp of the swing template: follow the handle's TAIL, then
    iterate chord clamps against POLE: a point on the sphere of radius L
    about the handle's head sits within swing alpha of the pole axis
    exactly when its CHORD distance to the pole is at most 2L*sin(alpha/2),
    with a re-sphere between rounds. band_min == band_max degenerates the
    band to a fixed-tilt RING (the tangent cone's precession circle).
    Every distance is between points that all ride the handle, so the
    clamp is translation-invariant and the handle may move freely."""
    chord_max = 2.0 * length * math.sin(min(max(band_max, 0.0), math.pi) / 2.0)
    chord_min = 2.0 * length * math.sin(min(max(band_min, 0.0), math.pi) / 2.0)
    con = goal_pb.constraints.new("COPY_LOCATION")
    con.name = _PREFIX + "Aim"
    con.target = arm_obj
    con.subtarget = ctrl_pb.name
    con.head_tail = 1.0
    # Each chord+resphere pair cuts the violation by ~0.15x near a 45 deg
    # band. Wider bands (a steep cone ring) converge slower and ask for
    # more rounds, but every round also stacks float32 constraint noise
    # onto the REST pose (five rounds drifted a resting ball 0.0005 rad),
    # so callers pick: 3 for the ball's band, 5 for the cone_spin ring.
    for i in range(rounds):
        con = goal_pb.constraints.new("LIMIT_DISTANCE")
        con.name = _PREFIX + "Cone Max {}".format(i + 1)
        con.target = arm_obj
        con.subtarget = pole_pb.name
        con.distance = chord_max
        con.limit_mode = "LIMITDIST_INSIDE"
        if chord_min > 1e-9:
            con = goal_pb.constraints.new("LIMIT_DISTANCE")
            con.name = _PREFIX + "Cone Min {}".format(i + 1)
            con.target = arm_obj
            con.subtarget = pole_pb.name
            con.distance = chord_min
            con.limit_mode = "LIMITDIST_OUTSIDE"
        con = goal_pb.constraints.new("LIMIT_DISTANCE")
        con.name = _PREFIX + "Resphere {}".format(i + 1)
        con.target = arm_obj
        con.subtarget = ctrl_pb.name
        con.head_tail = 0.0    # the swing center: the ctrl's own head
        con.distance = length
        con.limit_mode = "LIMITDIST_ONSURFACE"


def apply_cone_spin(arm_obj, def_pb, ctrl_pb, goal_pb, pole_pb, frame_pb,
                    tilt, joint_id=""):
    """A cone tangent on a plane, posed through ONE grabbable bone (live
    corpus 15 cone3, 2026-08-23). The channel-lock trick of planar_spin
    cannot work here: the spin axis is tilted out of the plane by the
    half-angle, so precession about the plane normal is not a lockable
    euler channel. Instead the ball template runs with a DEGENERATE band:

      handle  (ctrl_pb, visible): slides on the plane, with Limit Location in
              the CUSTOM space of FRM (the static plane frame. A follower
              frame would cycle the depsgraph) clamps the plane-normal
              channel to its rest value, and rotates freely.
      POLE    rides the handle at head + L*normal: a LOCAL->LOCAL location
              copy, exact because rig_build gave both bones the same rest
              orientation, so the local channels map 1:1.
      GOAL    the chord clamp with band [tilt, tilt]: the handle's aim
              pinned onto the fixed-tilt ring around the moving pole.
      DEF     copies the handle's position and rotation, then Damped
              Tracks GOAL: equal to the handle wherever its axis sits on
              the ring, minimally corrected onto it elsewhere: spin
              preserved. Geometry and child bones ride DEF.
    """
    length = ctrl_pb.bone.length

    remove_rig_constraints(ctrl_pb)
    ctrl_pb.lock_location = [False, False, False]
    ctrl_pb.lock_rotation = [False, False, False]
    ctrl_pb.lock_scale = [True, True, True]
    con = ctrl_pb.constraints.new("LIMIT_LOCATION")
    con.name = _PREFIX + "On Plane"
    con.owner_space = "CUSTOM"
    con.space_object = arm_obj
    con.space_subtarget = frame_pb.name
    con.use_min_y = con.use_max_y = True
    con.min_y = con.max_y = 0.0

    remove_rig_constraints(frame_pb)
    frame_pb["RIG_helper"] = joint_id
    frame_pb.lock_location = [True, True, True]
    frame_pb.lock_rotation = [True, True, True]
    frame_pb.lock_scale = [True, True, True]

    remove_rig_constraints(pole_pb)
    pole_pb["RIG_helper"] = joint_id
    pole_pb.lock_location = [True, True, True]
    pole_pb.lock_rotation = [True, True, True]
    pole_pb.lock_scale = [True, True, True]
    con = pole_pb.constraints.new("COPY_LOCATION")
    con.name = _PREFIX + "Ride Handle"
    con.target = arm_obj
    con.subtarget = ctrl_pb.name
    con.target_space = "LOCAL"
    con.owner_space = "LOCAL"

    remove_rig_constraints(goal_pb)
    goal_pb["RIG_helper"] = joint_id
    goal_pb.lock_location = [True, True, True]
    goal_pb.lock_rotation = [True, True, True]
    goal_pb.lock_scale = [True, True, True]
    _swing_band_chain(arm_obj, goal_pb, ctrl_pb, pole_pb, length, tilt, tilt,
                      rounds=5)

    remove_rig_constraints(def_pb)
    def_pb.lock_location = [True, True, True]
    def_pb.lock_rotation = [True, True, True]
    def_pb.lock_scale = [True, True, True]
    con = def_pb.constraints.new("COPY_LOCATION")
    con.name = _PREFIX + "Follow Handle Loc"
    con.target = arm_obj
    con.subtarget = ctrl_pb.name
    con = def_pb.constraints.new("COPY_ROTATION")
    con.name = _PREFIX + "Follow Handle"
    con.target = arm_obj
    con.subtarget = ctrl_pb.name
    con.mix_mode = "REPLACE"
    con.target_space = "WORLD"
    con.owner_space = "WORLD"
    con = def_pb.constraints.new("DAMPED_TRACK")
    con.name = _PREFIX + "Swing Clamp"
    con.target = arm_obj
    con.subtarget = goal_pb.name
    con.track_axis = "TRACK_Y"


def apply_ball_cone(arm_obj, def_pb, ctrl_pb, goal_pb, pole_pb, joint: Joint):
    """The exact swing-cone limit for a ball joint (SW To Blender live
    corpus 04, 2026-08-23: per-axis Euler limits made a constant-angle sweep
    'bounce', since the box lets ~1.27x the limit through at diagonal
    azimuths, and centered the cone on the REST pose instead of the
    socket axis).

    Frame: joint.axis = cone axis A (parent-fixed), joint.secondary_axis =
    the child direction u the mate measures. The limit values are the
    UNSIGNED angle band angle(u, A) in [min, max]. Bones (rig_build):
      ctrl  visible handle, rest +Y = u, head at the ball center. Rotation
            free, location locked. Its TAIL is the live u direction.
      POLE  static child of the parent bone at center + L*A.
      GOAL  copies ctrl's tail, then is clamped into the cone band: a point
            ON the sphere of radius L lies within swing alpha of A exactly
            when its CHORD distance to the pole is at most 2L*sin(alpha/2),
            so Limit Distance INSIDE/OUTSIDE against POLE is the band and
            Limit Distance ONSURFACE against the center re-spheres it. One
            pair is exact for small violations. Three pairs bound the error
            below ~0.1 deg even a quarter-turn past the limit, uniform in
            azimuth: the clamp direction only ever moves along the
            violation, never around it.
      DEF   carries geometry and child bones: Copy Rotation from ctrl
            (identical rest, so inside the cone DEF == ctrl exactly, twist
            included), then Damped Track +Y at GOAL: the minimal rotation
            onto the clamped direction, twist preserved, like a stud
            sliding on the socket rim.
    """
    lim = joint.rotation_limit
    length = ctrl_pb.bone.length

    remove_rig_constraints(ctrl_pb)
    ctrl_pb.lock_location = [True, True, True]
    ctrl_pb.lock_rotation = [False, False, False]
    ctrl_pb.lock_scale = [True, True, True]

    remove_rig_constraints(pole_pb)
    pole_pb["RIG_helper"] = joint.id
    pole_pb.lock_location = [True, True, True]
    pole_pb.lock_rotation = [True, True, True]
    pole_pb.lock_scale = [True, True, True]

    remove_rig_constraints(goal_pb)
    goal_pb["RIG_helper"] = joint.id
    goal_pb.lock_location = [True, True, True]
    goal_pb.lock_rotation = [True, True, True]
    goal_pb.lock_scale = [True, True, True]
    _swing_band_chain(arm_obj, goal_pb, ctrl_pb, pole_pb, length,
                      lim.min, lim.max)

    remove_rig_constraints(def_pb)
    def_pb.lock_location = [True, True, True]
    def_pb.lock_rotation = [True, True, True]
    def_pb.lock_scale = [True, True, True]
    con = def_pb.constraints.new("COPY_ROTATION")
    con.name = _PREFIX + "Follow Handle"
    con.target = arm_obj
    con.subtarget = ctrl_pb.name
    con.mix_mode = "REPLACE"
    con.target_space = "WORLD"
    con.owner_space = "WORLD"
    con = def_pb.constraints.new("DAMPED_TRACK")
    con.name = _PREFIX + "Swing Clamp"
    con.target = arm_obj
    con.subtarget = goal_pb.name
    con.track_axis = "TRACK_Y"


def apply_collapsed_contact(pose_bone, collapsed, unit_scale=1.0,
                            orbit_target=None, orbit_subtarget=None):
    """A folded carrier chain on ONE bone: the puck is grabbed and moved
    directly instead of through a helper bone (2026-08-23).

    Frames are set by rig_build per kind:
      planar_spin: Y = spin axis, Z = plane normal. Slide in-plane (X/Y
        free, Z locked), spin about Y, yaw about Z, no tilt (X locked).
        rotation_mode YXZ composes Rz@Rx@Ry, with X locked that is
        yaw-outside-spin, exactly the tangent-preserving set.
      orbit_spin: Y = spin axis (parallel to the orbit axis). Drag in the
        orbit plane (X/Z free, Y locked). The Limit Distance against the
        hidden center bone holds the tangency radius. Spin about Y free.
      planar_ball: Y = plane normal. Slide in-plane, all rotations free.
      slide_ball: Y = slide axis. Slide along Y only, all rotations free.
    """
    remove_rig_constraints(pose_bone)
    kind = collapsed.kind

    if kind == "planar_spin":
        lock_loc = [False, False, True]
        lock_rot = [True, False, False]
    elif kind == "orbit_spin":
        lock_loc = [False, True, False]
        lock_rot = [True, False, True]
        if orbit_target is not None and orbit_subtarget:
            con = pose_bone.constraints.new("LIMIT_DISTANCE")
            con.name = _PREFIX + "Tangency Radius"
            con.target = orbit_target
            con.subtarget = orbit_subtarget
            con.distance = collapsed.orbit_radius * unit_scale
            # ONSURFACE: the bone must sit exactly at the tangency radius.
            # inside is interference, outside breaks contact.
            con.limit_mode = "LIMITDIST_ONSURFACE"
    elif kind == "planar_ball":
        lock_loc = [False, True, False]
        lock_rot = [False, False, False]
    elif kind == "slide_ball":
        lock_loc = [True, False, True]
        lock_rot = [False, False, False]
    else:
        lock_loc = [False, False, False]
        lock_rot = [False, False, False]

    pose_bone.lock_location = lock_loc
    pose_bone.lock_rotation = lock_rot
    pose_bone.lock_scale = [True, True, True]


def lock_all(pose_bone):
    """Grounded roots: the armature root must not be hand-posed away from
    the CAD ground."""
    remove_rig_constraints(pose_bone)
    pose_bone.lock_location = [True, True, True]
    pose_bone.lock_rotation = [True, True, True]
    pose_bone.lock_scale = [True, True, True]


def unlock_all(pose_bone):
    """Free/unmatched groups stay posable on every channel: under-mated in
    CAD means undecided, not fixed."""
    remove_rig_constraints(pose_bone)
    pose_bone.lock_location = [False, False, False]
    pose_bone.lock_rotation = [False, False, False]
    pose_bone.lock_scale = [True, True, True]
