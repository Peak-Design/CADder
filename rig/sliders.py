# SPDX-License-Identifier: GPL-3.0-or-later
"""Slider-crank closures: two bodies aimed at each other.

Blender's IK only rotates, so a sliding joint inside a chain can only be
locked and the mechanism freezes. A slider-crank has no need of a solver: cut
the loop at the slide and each half hangs off its own pin, and all that is
left is to keep them pointed at each other. A hydraulic ram is rigged exactly
this way by hand (Oscar, 2026-08-24).

Two tracks pointing at each other would be a dependency cycle, so each half
tracks a DUPLICATE bone carrying the other's pivot, parented to the other's
PARENT rather than to the other half. Those parents: the posed clamp and the
ground: are aimed at nothing, so the graph stays acyclic.

The bones rest with local +Y already along the ram (graph.BonePlan.aim_at,
applied by rig_build._bone_rest_matrix), which is what makes TRACK_Y the
right axis and what makes the rig look like the machine.

WHICH track. A Damped Track aims +Y and leaves every other rotation free, but
a ram half hangs on a PIN: the only turn it can make is about that pin. A
Locked Track says exactly that: turn about the locked axis until +Y comes as
near the target as it can, so it is used whenever the half's mount names an
axis. rig_build rests these bones with local Z on the pin exactly, which is
what LOCK_Z then means. Damped Track remains the fallback for a mount with no
axis of its own (a ball, a free pair), where nothing is known to lock.

Without the lock the ram is free to roll about its own length and to swing out
of the plane its pin allows. Live ClampRig (2026-08-24) showed the
second of those as a rod meeting its bore at an angle.
"""

try:
    import bpy
except ImportError:
    bpy = None

_TRACK_NAME = "CADLink Aim "


def _pinned(plan, gid):
    """Whether this half hangs on a mount with an axis of its own: the case
    rig_build rests with local Z ON that axis, so LOCK_Z is the pin."""
    bp = getattr(plan, "bone_by_group", {}).get(gid)
    if bp is None or bp.joint is None or bp.joint.axis is None:
        return False
    if bp.joint.origin is None or bp.aim_at is None:
        return False
    # rig_build only builds the pin-aligned frame when the aim direction has
    # something left after the pin is projected out of it. A target straight
    # up the pin has not, and there local Z is whatever the fallback chose.
    ax = bp.joint.axis
    d = [bp.aim_at[i] - bp.joint.origin[i] for i in range(3)]
    n2 = sum(a * a for a in ax)
    if n2 <= 1e-18:
        return False
    along = sum(d[i] * ax[i] for i in range(3)) / n2
    flat = [d[i] - along * ax[i] for i in range(3)]
    return sum(f * f for f in flat) > 1e-18


def close_sliders(arm_obj, plan, bone_names, aim_names):
    """Adds the two aim constraints per slider loop. aim_names maps
    (loop id, "a"|"c") to the bone name Blender actually kept. Returns
    (count, warnings)."""
    warnings = []
    count = 0
    pose = arm_obj.pose

    for splan in plan.sliders:
        pairs = (
            (splan.a_group, aim_names.get((splan.loop.id, "a"))),
            (splan.c_group, aim_names.get((splan.loop.id, "c"))),
        )
        if any(target is None for _, target in pairs):
            warnings.append(
                "loop {}: aim target bone missing, closure skipped".format(
                    splan.loop.id))
            continue

        made = 0
        for gid, target in pairs:
            pb = pose.bones.get(bone_names.get(gid, ""))
            if pb is None:
                warnings.append(
                    "loop {}: bone for group {} missing, half of the aim pair "
                    "skipped".format(splan.loop.id, gid))
                continue
            for con in list(pb.constraints):
                if con.name.startswith(_TRACK_NAME):
                    pb.constraints.remove(con)
            pinned = _pinned(plan, gid)
            con = pb.constraints.new(
                "LOCKED_TRACK" if pinned else "DAMPED_TRACK")
            con.name = _TRACK_NAME + splan.loop.id
            con.target = arm_obj
            con.subtarget = target
            # The target bone's HEAD is the pivot. head_tail 0 is the default
            # but it is the whole geometry of this closure, so it is stated.
            con.head_tail = 0.0
            con.track_axis = "TRACK_Y"
            if pinned:
                con.lock_axis = "LOCK_Z"
            made += 1
        if made == 2:
            count += 1
    return count, warnings
