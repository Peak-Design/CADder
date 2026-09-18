# SPDX-License-Identifier: GPL-3.0-or-later
"""A universal joint built from its parts, driven exactly instead of by IK.

Two shafts turn on the ground, and a cross (the spider, with its pins
locked in) joins them: each shaft's yoke holds one arm of the cross on a
revolute. That is four revolutes in one loop, and the four axes meet at one
point. It is a spatial four-bar, and Blender's IK, which can only turn the
bones of a chain, cannot close it: the output yoke flips between two
solutions as the input turns (universal joint 2, 2026-09-18).

The motion itself is known exactly. Turn the input shaft by an angle and
its arm of the cross turns with it. The output shaft must then sit where its
own arm is square to that one, which has one answer that starts at rest,
and the cross is then fixed by its two arms. So the output shaft and the
cross are both functions of the input angle, and they travel as table
couplings on the input's joint: the same drive a cam gets, sampled finely
enough that the interpolation is far below anything visible. The loop then
needs no solver at all.

Pure Python, so graph.build keeps working without Blender.
"""

import math

# The cross's arms are square to each other and to their own shafts, to
# within this much (about 1.1 degrees).
SQUARE_TOL = 0.02
# The four axes meet at one point, to within this share of the loop's size.
MEET_TOL = 0.02
# Shafts closer to parallel or to square than this are not a universal
# joint anyone builds, and the relation is singular at both ends.
MIN_BEND = math.radians(1.0)
MAX_BEND = math.radians(89.0)
# Samples per turn of the input. Linear between samples, the error is
# about a thousandth of a degree at a 40 degree bend.
SAMPLES = 720
# The recovered motion has to BE a turn about each joint's own axis, or
# the shape was not what it looked like and nothing is changed.
FIT_TOL = 1e-4


# -- Vectors and rotations, as plain tuples --------------------------------

def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _unit(a):
    n = math.sqrt(_dot(a, a))
    if n < 1e-12:
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def _rotate(v, axis, angle):
    """Rodrigues: v turned by angle about the unit axis."""
    c, s = math.cos(angle), math.sin(angle)
    k = _cross(axis, v)
    d = _dot(axis, v) * (1.0 - c)
    return (v[0] * c + k[0] * s + axis[0] * d,
            v[1] * c + k[1] * s + axis[1] * d,
            v[2] * c + k[2] * s + axis[2] * d)


def _frame(x, y):
    """Rows of the orthonormal frame whose first axis is x and whose second
    lies in the plane of x and y. The arms of a real cross are square to a
    few parts in a million, and this keeps the frame a rotation anyway."""
    x = _unit(x)
    y = _unit(_sub(y, _scale(x, _dot(x, y))))
    return (x, y, _cross(x, y))


def _between(frame_from, frame_to):
    """The rotation (3x3, rows) that takes frame_from onto frame_to."""
    # R = sum over i of to_i (outer) from_i
    rows = []
    for r in range(3):
        rows.append(tuple(sum(frame_to[i][r] * frame_from[i][c]
                              for i in range(3)) for c in range(3)))
    return tuple(rows)


def _mat_t(m):
    return tuple(tuple(m[c][r] for c in range(3)) for r in range(3))


def _mat_mul(a, b):
    return tuple(tuple(sum(a[r][k] * b[k][c] for k in range(3))
                       for c in range(3)) for r in range(3))


_I = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _axis_angle(m, axis):
    """The turn of rotation m about axis, and how far m is from a pure turn
    about it (0 when it is one)."""
    # A turn about a unit axis k by t: m = cos t I + sin t [k]x + (1-cos t) kk
    trace = m[0][0] + m[1][1] + m[2][2]
    skew = (m[2][1] - m[1][2], m[0][2] - m[2][0], m[1][0] - m[0][1])
    angle = math.atan2(0.5 * _dot(skew, axis), 0.5 * (trace - 1.0))
    back = _rotation(axis, angle)
    err = max(abs(m[r][c] - back[r][c]) for r in range(3) for c in range(3))
    return angle, err


def _rotation(axis, angle):
    cols = [_rotate(e, axis, angle) for e in _I]
    return tuple(tuple(cols[c][r] for c in range(3)) for r in range(3))


# -- Recognition -------------------------------------------------------------

def _line_gap(origin, axis, point):
    """Distance from point to the line through origin along the unit axis."""
    d = _sub(point, origin)
    return math.sqrt(max(0.0, _dot(d, d) - _dot(d, axis) ** 2))


def _meeting_point(lines):
    """The point nearest to every line, by least squares."""
    a = [[0.0] * 3 for _ in range(3)]
    b = [0.0, 0.0, 0.0]
    for o, k in lines:
        for r in range(3):
            for c in range(3):
                p = (1.0 if r == c else 0.0) - k[r] * k[c]
                a[r][c] += p
                b[r] += p * o[c]
    # Solve the 3x3 by Cramer's rule.
    def det(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    d = det(a)
    if abs(d) < 1e-18:
        return None
    out = []
    for i in range(3):
        m = [row[:] for row in a]
        for r in range(3):
            m[r][i] = b[r]
        out.append(det(m) / d)
    return tuple(out)


def _shape(manifest, loop):
    """The loop's parts when it is a universal joint, else None:
    (driver joint, other shaft joint, the cross's two joints by shaft)."""
    if loop.closure_kind not in ("ik", "none") or len(loop.member_joints) != 4:
        return None
    joints = manifest.joint_by_id()
    groups = manifest.group_by_id()
    members = [joints.get(j) for j in loop.member_joints]
    if any(j is None or j.type != "revolute" or j.axis is None
           or j.origin is None for j in members):
        return None
    shafts = [j for j in members if groups[j.parent_group].grounded]
    arms = [j for j in members if j not in shafts]
    if len(shafts) != 2 or len(arms) != 2:
        return None
    shaft_bodies = {s.child_group: s for s in shafts}
    cross = None
    arm_of = {}
    for a in arms:
        ends = {a.parent_group, a.child_group}
        on = ends & set(shaft_bodies)
        if len(on) != 1:
            return None
        body = (ends - on).pop()
        if cross is None:
            cross = body
        elif cross != body:
            return None
        arm_of[on.pop()] = a
    if len(arm_of) != 2:
        return None

    driver = None
    if loop.suggested_driver_joint in (shafts[0].id, shafts[1].id):
        driver = joints[loop.suggested_driver_joint]
    else:
        driver = shafts[0]
    other = shafts[1] if driver is shafts[0] else shafts[0]

    a_d, a_o = _unit(driver.axis), _unit(other.axis)
    e_d = _unit(arm_of[driver.child_group].axis)
    e_o = _unit(arm_of[other.child_group].axis)
    if None in (a_d, a_o, e_d, e_o):
        return None
    if (abs(_dot(a_d, e_d)) > SQUARE_TOL or abs(_dot(a_o, e_o)) > SQUARE_TOL
            or abs(_dot(e_d, e_o)) > SQUARE_TOL):
        return None
    bend = math.acos(max(-1.0, min(1.0, abs(_dot(a_d, a_o)))))
    if not (MIN_BEND < bend < MAX_BEND):
        return None
    lines = [(tuple(j.origin), _unit(j.axis)) for j in members]
    centre = _meeting_point(lines)
    if centre is None:
        return None
    size = max(math.sqrt(_dot(_sub(o, centre), _sub(o, centre)))
               for o, _k in lines) or 1.0
    if max(_line_gap(o, k, centre) for o, k in lines) > MEET_TOL * size:
        return None
    return driver, other, arm_of, cross, (a_d, a_o, e_d, e_o), bend


def _poses(a_d, a_o, e_d, e_o, theta, previous):
    """World rotations of the driver shaft, the other shaft and the cross
    for an input turn theta, on the branch nearest the previous output."""
    arm_d = _rotate(e_d, a_d, theta)
    side = _cross(a_o, e_o)
    base = math.atan2(-_dot(arm_d, e_o), _dot(arm_d, side))
    # Two answers half a turn apart. Keep the one the motion is on.
    best = None
    for cand in (base, base + math.pi):
        while cand - previous > math.pi:
            cand -= 2.0 * math.pi
        while cand - previous < -math.pi:
            cand += 2.0 * math.pi
        if best is None or abs(cand - previous) < abs(best - previous):
            best = cand
    phi = best
    arm_o = _rotate(e_o, a_o, phi)
    rot_d = _rotation(a_d, theta)
    rot_o = _rotation(a_o, phi)
    rot_c = _between(_frame(e_d, e_o), _frame(arm_d, arm_o))
    return rot_d, rot_o, rot_c, phi


def recognise(manifest):
    """Turns every loop that is a universal joint into two table couplings
    on its input and marks the loop as needing no solver. Returns one line
    per joint changed, for the rig report. Safe to call again: the drive is
    worked out afresh from the loop's current input each time."""
    notes = []
    joints = manifest.joint_by_id()
    # Couplings made by an earlier call go first, so a change of input
    # never leaves the old drive behind.
    for jid in getattr(manifest, "_hooke_joints", ()):
        j = joints.get(jid)
        if j is not None:
            j.coupling = None
    loops_by_id = {lp.id: lp for lp in manifest.loops}
    for lid, kind in getattr(manifest, "_hooke_closures", {}).items():
        if lid in loops_by_id:
            loops_by_id[lid].closure_kind = kind
    manifest._hooke_joints = []
    manifest._hooke_closures = {}

    for loop in manifest.loops:
        shape = _shape(manifest, loop)
        if shape is None:
            continue
        driver, other, arm_of, cross, (a_d, a_o, e_d, e_o), bend = shape
        members = [joints[j] for j in loop.member_joints]
        # A joint that already carries a coupling is someone else's
        # relation, and it is left alone.
        if any(j.coupling is not None for j in members):
            continue
        body_rot = {}
        driven = [j for j in members
                  if j.id not in (driver.id, loop.closure_joint)]
        tables = {j.id: [] for j in driven}
        previous = 0.0
        fits = True
        for i in range(SAMPLES + 1):
            theta = 2.0 * math.pi * i / SAMPLES
            rot_d, rot_o, rot_c, previous = _poses(
                a_d, a_o, e_d, e_o, theta, previous)
            body_rot = {driver.child_group: rot_d,
                        other.child_group: rot_o, cross: rot_c}
            for j in members:
                parent = body_rot.get(j.parent_group, _I)
                child = body_rot[j.child_group]
                rel = _mat_mul(_mat_t(parent), child)
                angle, err = _axis_angle(rel, _unit(j.axis))
                if err > FIT_TOL:
                    fits = False
                    break
                if j.id in tables:
                    rows = tables[j.id]
                    if rows:
                        last = rows[-1][1]
                        while angle - last > math.pi:
                            angle -= 2.0 * math.pi
                        while angle - last < -math.pi:
                            angle += 2.0 * math.pi
                    rows.append((theta, angle))
            if not fits:
                break
        if not fits:
            notes.append("loop {}: looks like a universal joint, but the "
                         "exact motion does not fit its joints, so it keeps "
                         "its IK".format(loop.id))
            continue
        from .manifest import Coupling
        for j in driven:
            j.coupling = Coupling(kind="table", driver_joint=driver.id,
                                  samples=tables[j.id], periodic=True,
                                  period=2.0 * math.pi)
            manifest._hooke_joints.append(j.id)
        manifest._hooke_closures[loop.id] = loop.closure_kind
        loop.closure_kind = "none"
        notes.append(
            "loop {}: universal joint, {:.1f} degree bend. {} drives {} "
            "exactly, with no IK".format(
                loop.id, math.degrees(bend), driver.id,
                ", ".join(j.id for j in driven)))
        for other_loop in _pin_loops(manifest, loop, members):
            manifest._hooke_closures[other_loop.id] = other_loop.closure_kind
            other_loop.closure_kind = "none"
            notes.append(
                "loop {}: a pin that only spins on a hinge of loop {}. The "
                "exact drive already holds it, so it needs no IK".format(
                    other_loop.id, loop.id))
    return notes


def _collinear(a, b, size):
    """Whether two revolutes turn about the same line."""
    ka, kb = _unit(a.axis), _unit(b.axis)
    c = _cross(ka, kb)
    if math.sqrt(_dot(c, c)) > SQUARE_TOL:
        return False
    return _line_gap(tuple(a.origin), ka, tuple(b.origin)) <= MEET_TOL * size


def _pin_loops(manifest, hooke_loop, members):
    """The other loops that a pin makes with the universal joint: a body
    that only turns, on one line, between two bodies of the joint that are
    themselves hinged on that same line, or on one of them twice. With the
    pins free in their bores (universal joint 2 before its pins were
    locked, 2026-09-16) each pin closes a loop of its own, and IK on that
    loop turns the yoke and the spider away from the exact drive. The pin's
    spin changes nothing anyone can see, so the loop needs no solver."""
    joints = manifest.joint_by_id()
    bodies = set()
    for j in members:
        bodies.add(j.parent_group)
        bodies.add(j.child_group)
    ids = {j.id for j in members}
    lines = [(tuple(j.origin), _unit(j.axis)) for j in members]
    centre = _meeting_point(lines) or (0.0, 0.0, 0.0)
    size = max(math.sqrt(_dot(_sub(o, centre), _sub(o, centre)))
               for o, _k in lines) or 1.0
    found = []
    for lp in manifest.loops:
        if lp is hooke_loop or lp.closure_kind not in ("ik", "none"):
            continue
        extra = [joints.get(j) for j in lp.member_joints if j not in ids]
        if not extra or any(j is None or j.type != "revolute"
                            or j.axis is None or j.origin is None
                            for j in extra):
            continue
        outside = set()
        for j in extra:
            outside |= {j.parent_group, j.child_group} - bodies
        if len(outside) != 1:
            continue
        pin = outside.pop()
        if any(pin not in (j.parent_group, j.child_group) for j in extra):
            continue
        if not all(_collinear(extra[0], j, size) for j in extra[1:]):
            continue
        ends = {(j.child_group if j.parent_group == pin else j.parent_group)
                for j in extra}
        if len(ends) == 2:
            x, y = ends
            hinge = [j for j in members if {j.parent_group, j.child_group} == {x, y}]
            if len(hinge) != 1 or not _collinear(hinge[0], extra[0], size):
                continue
        found.append(lp)
    return found
