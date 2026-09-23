# SPDX-License-Identifier: GPL-3.0-or-later
"""Joint-graph planning: manifest -> RigPlan.

No bpy import: this module runs under plain Python in CI. Everything that
can make the generated rig wrong is decided and validated here, before a
single Blender datablock exists. A muted or zero-influence constraint still
counts as a depsgraph dependency in Blender, so a dependency cycle found
here means "do not create the rig", never "create it disabled".

The exporter already cut every graph cycle (SCHEMA.md: loops are pre-cut,
consumers must not re-derive them). This module only verifies the cut is
consistent and turns it into build orders.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .manifest import Joint, Loop, Manifest, ManifestError, RigidGroup
from . import hooke

# Blender truncates datablock names at 63 bytes and dedups with ".001". Names are display labels only, so plans keep them short and every module
# addresses bones through the RigPlan / build maps, never by parsing names.
_MAX_NAME = 55


@dataclass
class CollapsedContact:
    """A carrier chain folded into ONE posable bone. The exporter decomposes
    a tangent/contact mate into carrier joint + spin joint through a
    component-less group. Posing that chain means driving the CARRIER, which
    nobody thinks in ("rotate the helper to slide the puck"). The collapse
    gives the child bone the chain's combined freedom directly: locks in a
    contact-aligned frame, plus a Limit Distance for orbit contacts, so the
    puck is grabbed and moved like in SolidWorks (Oscar's ask, 2026-08-23).

    kinds: planar_spin (cylinder on plane: slide in plane, yaw about the
    normal, spin about own axis), orbit_spin (rim-tangent discs: drag around
    the base at the tangency radius, spin free), planar_ball (sphere/vertex
    on plane), slide_ball (vertex on line), cone_spin (cone tangent on a
    plane: like planar_spin but the spin axis is TILTED out of the plane by
    the cone's half-angle, so channel locks cannot hold it: the ball
    template's chord clamp pins the axis to its fixed-tilt ring instead,
    live corpus 15 cone3, 2026-08-23)."""
    kind: str
    carrier_joint: Joint
    spin_joint: Joint
    orbit_center: Optional[List[float]] = None   # metres (orbit_spin only)
    orbit_axis: Optional[List[float]] = None
    orbit_radius: float = 0.0                    # metres
    helper_name: str = ""                        # Limit Distance target bone
    # cone_spin only: the fixed angle between the spin axis and the plane
    # NORMAL (the ring the DEF clamp holds), plus the hidden template bones.
    tilt: float = 0.0                            # radians
    def_name: str = ""
    pole_name: str = ""
    goal_name: str = ""
    frame_name: str = ""                         # plane-frame custom space


@dataclass
class BonePlan:
    group: RigidGroup
    bone_name: str
    parent_group_id: Optional[str] = None
    joint: Optional[Joint] = None
    collapsed: Optional[CollapsedContact] = None
    # The one grounded root: named after the assembly, resting AT the
    # assembly origin. A grounded group's own frame is whichever member the
    # exporter happened to anchor it to (live 2026-08-23: the flexible-sub
    # "baseplate" bone landed on the hinge), and static geometry needs no
    # per-group placement: the origin is the WYSIWYG spot.
    root: bool = False
    # Swing-cone ball template (joint.axis = cone axis, secondary = the
    # child direction the limit band constrains): bone_name is the user
    # handle, DEF carries the geometry through the exact swing clamp, POLE
    # anchors the cone axis, GOAL is the clamped aim point. Empty for every
    # other joint. The template's dependencies (DEF -> ctrl/GOAL,
    # GOAL -> ctrl/POLE, all under one parent bone) are acyclic by
    # construction, so the pre-flight needs no extra nodes for them.
    ball_def_name: str = ""
    ball_pole_name: str = ""
    ball_goal_name: str = ""
    # Mirror pairs (coupling kind "mirror"): BOTH bones of the pair rest
    # with the SAME plane-aligned orientation (local +Y = this normal) at
    # their own positions: equal rest axes reduce the reflection to
    # per-channel sign flips, which is what makes the six drivers exact.
    mirror_normal: Optional[List[float]] = None
    # Half of a slider-crank aim pair: the point in the OTHER half's frame
    # this bone rests pointing at (metres, manifest frame). Damped Track aims
    # a named local axis, so the two halves of a ram can only track each
    # other if their rest +Y already lies along the ram.
    aim_at: Optional[List[float]] = None
    # A slide (prismatic or cylindrical tree edge) INSIDE an IK-solved
    # chain. Blender's IK rotates bones and stretches them, never translates
    # them, so the one translational freedom it has is the bone's length.
    # This body's bone therefore hangs, connected and without inheriting
    # scale, off a hidden stretch bone that lies along its own slide axis:
    # the solver scales that bone and the body moves along the slide by
    # exactly the difference (live actuator.sldasm, 2026-09-14: the
    # plunger the crank drives through two links never moved, its links
    # only aimed at the crank pin). slide_rest_length is the stretch bone's
    # rest length in metres, the head sitting that far back along the
    # slide so the solver can also shorten it.
    slide_name: str = ""
    slide_rest_length: float = 0.0
    # A cam contact (coupling kind "cam" on this body's slide). PRJ is the
    # hidden bone a vertex or roller follower projects onto the cam faces
    # with; REL and OFF serve a flat face: REL copies the cam's pose under
    # this body's base, OFF carries the translation term between the base
    # and this bone. cam_contact.py builds and drives them.
    cam_prj_name: str = ""
    cam_rel_name: str = ""
    cam_off_name: str = ""
    # A screw SPINS as it advances. The spin lives on a hidden bone of
    # its own, a child of the body's bone, and that is where the geometry
    # rides. Two things need it there.
    #
    # One: the turn is a function of the slide, and a driver reading the
    # slide off the bone it writes to is a depsgraph cycle. Blender reports
    # it and then breaks it with the last evaluation's value, so the turn
    # lagged the slide by a whole step and stayed lagged once the user let
    # go (live wrench.sldasm, 2026-09-16: 14.2 degrees out, and the loop it
    # closed 2.2 mm open). A child reading its PARENT is an ordinary
    # dependency with no cycle at all, and it reads the slide after the
    # joint's own travel limit rather than before it.
    #
    # Two: that spin is driven, so it happens whether or not anything asks
    # for it, and a body joined to the screw about some OTHER axis cannot
    # turn back out of it. Inheriting it would tip that body out of the
    # mechanism for good ("everything rotates with the screw but it
    # shouldn't", Oscar, same day: the wrench's centerlink sits on a point
    # on the screw's end, pinned about the mechanism normal while the screw
    # turns about its own axis at right angles to it). Hanging the spin
    # BELOW the body's bone means such a child inherits the slide alone by
    # simply parenting where every other child parents.
    #
    # parent_spin marks the children that do ride the turn: the ones joined
    # about the screw's own axis, which can turn back out of it, and welds,
    # which are part of the turning body.
    spin_name: str = ""
    parent_spin: bool = False


@dataclass
class LoopPlan:
    loop: Loop
    closure_joint: Joint
    helper_name: str
    # The helper bone rides the driver-side chain so the closure point moves
    # with the posed side. The IK chain on the driven side follows it. The
    # effector bone is the same closure point rigid on the driven tip: the
    # IK constraint lives on it because the tip bone's own tail is NOT the
    # closure point (live corpus 06: bones point along the hinge axes, so
    # tails cannot even move in the mechanism plane and the solve went dead).
    effector_name: str
    helper_parent_group: str
    ik_tip_group: str
    driven_chain: List[str] = field(default_factory=list)   # tip first, ancestor excluded
    driver_chain: List[str] = field(default_factory=list)   # driver end first, ancestor excluded
    # Bones the IK chain spans below the effector: the driven bodies plus
    # one stretch bone per slide among them (BonePlan.slide_name).
    chain_count: int = 0
    # group id -> (min, max) radians on that bone's own Y, holding the
    # chain on the branch it rests on. See _branch_limits.
    branch_limits: Dict[str, Tuple[float, float]] = field(default_factory=dict)


@dataclass
class SliderPlan:
    """A loop closed by an AIM PAIR rather than IK.

    A slider-crank (a hydraulic ram working a clamp) has no rotational
    solve: Blender's IK only rotates, so a sliding joint inside a chain can
    only be locked, and the mechanism freezes. The exporter therefore cuts
    the loop AT the slide, and the two bodies either side of it each hang off
    their own pin. All that is left is to point them at each other.

    Which is how a ram is rigged by hand (Oscar, 2026-08-24): the rod parents
    to the clamp at the rod pin, the barrel to the body at the bore pivot,
    and each gets a Damped Track at the other. Blender will not take that
    directly (the two constraints depend on each other), so each aims at a
    DUPLICATE bone carrying the other's pivot, parented to the other's PARENT
    instead of to the other. Those parents are the posed clamp and the ground,
    neither of which is aimed at anything, so the graph stays acyclic.
    """
    loop: Loop
    closure_joint: Joint            # the slide that was cut
    a_group: str                    # body on the slide's parent side
    c_group: str                    # body on the slide's child side
    a_pivot: List[float]            # where a_group swings (metres)
    c_pivot: List[float]
    # Each half's target rides the OTHER half's parent, never the other half.
    a_aim_parent: str               # = c_group's tree parent
    c_aim_parent: str               # = a_group's tree parent
    a_aim_name: str = ""            # bone at c_pivot, tracked by a_group
    c_aim_name: str = ""            # bone at a_pivot, tracked by c_group


@dataclass
class RigPlan:
    manifest: Manifest
    bones: List[BonePlan] = field(default_factory=list)     # parents always before children
    bone_by_group: Dict[str, BonePlan] = field(default_factory=dict)
    joint_group: Dict[str, str] = field(default_factory=dict)  # tree joint id -> articulating group id
    loops: List[LoopPlan] = field(default_factory=list)
    sliders: List[SliderPlan] = field(default_factory=list)
    # group id -> rest length (metres) of the stretch bone its slide needs
    # (BonePlan.slide_name): every slide that sits inside a solved chain.
    slide_rest: Dict[str, float] = field(default_factory=dict)
    # Loops the exporter cut so the tree already carries them: verified like
    # any other, then deliberately left unsolved.
    open_loops: List[Loop] = field(default_factory=list)
    grounded_groups: List[str] = field(default_factory=list)
    free_groups: List[str] = field(default_factory=list)    # unparented, not grounded
    collapsed_carriers: List[str] = field(default_factory=list)  # groups with no bone
    # group id -> the bone name that group already has in the scene, where
    # an update asked for it to be kept. See build().
    keep_names: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    # What hooke.recognise did, one line per universal joint.
    hooke_notes: List[str] = field(default_factory=list)


class _UnionFind:
    def __init__(self, ids):
        self._parent = {i: i for i in ids}

    def find(self, i):
        root = i
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[i] != root:
            self._parent[i], i = root, self._parent[i]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self._parent[ra] = rb
        return True


def _unique_name(base: str, taken: Set[str], suffix: str) -> str:
    name = base[:_MAX_NAME] if len(base) > _MAX_NAME else base
    if name in taken:
        name = "{}.{}".format(name[:_MAX_NAME - len(suffix) - 1], suffix)
    taken.add(name)
    return name


def _path_to_root(group_id: str, parent_of: Dict[str, Tuple[str, Joint]]) -> List[str]:
    path = [group_id]
    while path[-1] in parent_of:
        path.append(parent_of[path[-1]][0])
    return path


def _branches(parent_of, end_a, end_b):
    """Splits the tree path between two groups at their lowest common
    ancestor. Each branch is (groups from the end up to but excluding the
    ancestor, the joints along it)."""
    path_a = _path_to_root(end_a, parent_of)
    path_b = _path_to_root(end_b, parent_of)
    on_b = set(path_b)
    ancestor = None
    for g in path_a:
        if g in on_b:
            ancestor = g
            break
    if ancestor is None:
        return None

    def cut(path):
        groups = path[:path.index(ancestor)]
        joints = [parent_of[g][1] for g in groups]
        return groups, joints

    return ancestor, cut(path_a), cut(path_b)


def _v_sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _v_cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _v_norm(a):
    return _v_dot(a, a) ** 0.5


def _unit(a):
    n = _v_norm(a)
    return [a[0] / n, a[1] / n, a[2] / n] if n > 1e-12 else None


def swing_cone(joint: Optional[Joint]) -> bool:
    """A ball whose limit is a swing cone about a mate-defined axis. The
    exporter ships the cone frame in axis (parent-fixed cone axis, sign
    meaningful: it says which way the cone opens) and secondary_axis (the
    child-fixed direction the [min, max] band constrains). A limited ball
    WITHOUT an axis is a legacy manifest. It falls back to the Euler box."""
    return (joint is not None and joint.type == "ball"
            and joint.axis is not None and joint.secondary_axis is not None
            and joint.rotation_limit is not None)


_COLLAPSE_KINDS = {
    ("planar", "revolute"): "planar_spin",
    ("revolute", "revolute"): "orbit_spin",
    ("planar", "ball"): "planar_ball",
    ("prismatic", "ball"): "slide_ball",
}


def _collapse_carriers(plan: RigPlan, groups, parent_of) -> Dict[str, CollapsedContact]:
    """Folds P -> carrier -> K chains into P -> K where the carrier is a
    component-less contact link matching a known pattern. Mutates parent_of
    and joint_group. Returns child group id -> collapse spec. Anything with
    limits, couplings, loop membership, or geometry the patterns cannot
    absorb keeps its carrier bone: correct beats convenient."""
    children_of = {}
    for child, (parent, _) in parent_of.items():
        children_of.setdefault(parent, []).append(child)

    in_loops = set()
    for lplan in plan.loops:
        in_loops.update(lplan.driven_chain)
        in_loops.update(lplan.driver_chain)
        in_loops.add(lplan.helper_parent_group)
        in_loops.add(lplan.ik_tip_group)
        in_loops.add(lplan.closure_joint.parent_group)
        in_loops.add(lplan.closure_joint.child_group)
    for splan in plan.sliders:
        in_loops.update((splan.a_group, splan.c_group,
                         splan.a_aim_parent, splan.c_aim_parent))
    for lp in plan.open_loops:
        cj = plan.manifest.joint_by_id()[lp.closure_joint]
        in_loops.update((cj.parent_group, cj.child_group))

    coupling_drivers = set()
    for j in plan.manifest.joints:
        if j.coupling is not None and j.coupling.driver_joint:
            coupling_drivers.add(j.coupling.driver_joint)

    specs = {}
    for cid in sorted(groups):
        group = groups[cid]
        if group.components or group.grounded:
            continue
        if cid not in parent_of or cid in in_loops:
            continue
        kids = children_of.get(cid, [])
        if len(kids) != 1:
            continue
        kid = kids[0]
        parent_id, j1 = parent_of[cid]
        _, j2 = parent_of[kid]
        kind = _COLLAPSE_KINDS.get((j1.type, j2.type))
        if kind is None:
            continue
        if any(j.rotation_limit or j.translation_limit or j.coupling
               for j in (j1, j2)):
            continue
        if j1.id in coupling_drivers or j2.id in coupling_drivers:
            continue
        if j1.axis is None or (kind in ("planar_spin", "orbit_spin")
                               and j2.axis is None):
            continue

        spec = CollapsedContact(kind=kind, carrier_joint=j1, spin_joint=j2)
        if kind == "orbit_spin":
            n = _unit(j1.axis)
            a = _unit(j2.axis)
            if n is None or a is None or abs(_v_dot(n, a)) < 0.999:
                continue   # skewed axes: the chain is not a plain orbit
            if j1.origin is None or j2.origin is None:
                continue
            d = _v_sub(j2.origin, j1.origin)
            along = _v_dot(d, n)
            radial = _v_sub(d, [n[0] * along, n[1] * along, n[2] * along])
            radius = _v_norm(radial)
            if radius < 1e-6:
                continue   # concentric, not tangent, not this pattern
            # The Limit Distance target must sit IN the child's orbit plane:
            # the constraint clamps to a SPHERE, and only with zero axial
            # offset does sphere ∩ plane-lock equal the tangency circle at
            # the radial distance. So the center is the orbit-axis point at
            # the child's height, not the carrier origin itself.
            spec.orbit_center = [j1.origin[0] + n[0] * along,
                                 j1.origin[1] + n[1] * along,
                                 j1.origin[2] + n[2] * along]
            spec.orbit_axis = list(n)
            spec.orbit_radius = radius
        elif kind == "planar_spin":
            n = _unit(j1.axis)
            a = _unit(j2.axis)
            if n is None or a is None:
                continue
            if abs(_v_dot(n, a)) > 0.1:
                # The spin axis is tilted OUT of the plane: a tangent CONE
                # (live corpus 15 cone3). DECLINE the collapse and let the
                # exporter's own two-bone chain stand.
                #
                # One bone provably cannot hold this. The motion is
                # {Rn(yaw) . Ra(spin)} about two axes at the cone's
                # half-angle to each other. One bone with a channel locked
                # reaches {Rc . Rb} about two PERPENDICULAR rest axes, and
                # matching both slices forces b parallel to a and c parallel
                # to n, hence n perpendicular to a: half-angle zero, which
                # is the cylinder. That is exactly why planar_spin IS exact
                # and why this cannot be.
                #
                # It was collapsed anyway through the ball template with a
                # degenerate band, and that band is an iterated attractor
                # rather than a constraint: measured 2026-08-25 on the live
                # cone3 export it converges about 0.13x per round and leaks
                # up to 61 degrees of dip, putting the cone 17.8 mm through
                # the plate. The plain chain holds the dip to 4e-6 degrees
                # and the apex to 2e-9 m over 300 random poses, because the
                # child's only free channel IS its own axis, so the dip is
                # held by construction rather than by iteration.
                #
                # Declining a collapse is always safe: it is a posing
                # convenience, never a correctness requirement. The cost is
                # one extra grabbable bone, which is what the motion has:
                # one bone precesses about the vertical, the other spins
                # about the cone's own axis.
                continue

        # Rewire: the child hangs from the carrier's parent on the SPIN
        # joint (its axis and origin are the child's own), the carrier
        # leaves the tree and gets no bone.
        parent_of[kid] = (parent_id, j2)
        del parent_of[cid]
        plan.joint_group.pop(j1.id, None)
        plan.collapsed_carriers.append(cid)
        specs[kid] = spec
    return specs


def _assert_acyclic(edges: Dict[str, Set[str]], labels: Dict[str, str]):
    """Raises on the first directed cycle. Iterative DFS: assembly trees can
    be deep enough to hit the interpreter recursion limit."""
    state = {}  # node -> 1 while on stack, 2 when finished
    for start in sorted(edges):
        if state.get(start):
            continue
        stack = [(start, iter(sorted(edges.get(start, ()))))]
        state[start] = 1
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if state.get(nxt) == 1:
                    chain = [n for n, _ in stack]
                    cycle = chain[chain.index(nxt):] + [nxt]
                    raise ManifestError(
                        "rig dependency cycle: "
                        + " -> ".join(labels.get(n, n) for n in cycle))
                if state.get(nxt) is None:
                    state[nxt] = 1
                    stack.append((nxt, iter(sorted(edges.get(nxt, ())))))
                    advanced = True
                    break
            if not advanced:
                state[node] = 2
                stack.pop()


# A joint whose origin is one point, fixed in BOTH bodies: a link
# between two of these has a length that does not change, which is what
# makes a rest bend angle mean anything.
_PIVOTS = ("revolute", "cylindrical", "ball", "fixed")

# How far a rest bend must sit from a singularity before it names a branch
# to hold. A mechanism resting exactly straight has not chosen one.
_BRANCH_FLOOR = math.radians(2.0)


def _flatten(w, axis):
    """w with its component along axis taken out."""
    d = _v_dot(w, axis)
    return [w[0] - d * axis[0], w[1] - d * axis[1], w[2] - d * axis[2]]


def _branch_limits(lp: Loop, driven: List[str], closure: Joint, parent_of):
    """How far each elbow of a driven chain may bend, so the solver never
    reaches the pose where it could bend the other way instead.

    A chain of two links reaching one point has TWO answers, mirrored in
    the line through its ends. The two meet where the chain is straight
    (its links end to end) and where it is folded back on itself. Blender
    picks whichever its iteration lands on, so a chain driven up to one of
    those poses comes out of it on either branch, and the mechanism turns
    itself inside out in one frame.

    Live wrench.sldasm (2026-09-16, Oscar): "at some point secondgrip and
    centerlink instantly flip to the wrong position/direction ... as soon
    as this triangle flips direction it starts giving problems". Measured:
    the elbow went from -115.7 to +119.4 degrees between two neighbouring
    poses and the closure tore open by 25 mm.

    The assembly SolidWorks shipped is on one branch already, so that is
    the branch to hold. The bend at an elbow is the signed angle from the
    link coming in to the link going out, about the elbow's own axis, and
    the bone's own Y turns the outgoing link by exactly that much: the
    bend is its rest value plus the bone's Y, all the way round. Holding Y
    inside the half turn that keeps the bend's sign holds the branch. The
    limits land ON the two singular poses, so the mechanism still reaches
    both and only stops where it would otherwise have to guess.

    Planar loops only: those are the loops whose chains are held in the
    plane already (loops.py locks X and Z), so a bend about the joint axis
    is the whole of the freedom the solver has there.

    The bone at the ROOT of the chain is left out. Its bend is the chain's
    shoulder, free to swing the whole chain round, and the live wrench
    passes through its straight pose with nothing wrong at all.
    """
    limits = {}
    if not lp.planar:
        return limits
    for i in range(len(driven) - 1):
        gid = driven[i]
        joint = parent_of[gid][1]
        before = parent_of[driven[i + 1]][1]
        after = closure if i == 0 else parent_of[driven[i - 1]][1]
        if joint.type not in ("revolute", "cylindrical"):
            continue
        if before.type not in _PIVOTS or after.type not in _PIVOTS:
            continue
        axis = _unit(joint.axis) if joint.axis else None
        if (axis is None or joint.origin is None
                or before.origin is None or after.origin is None):
            continue
        u = _flatten(_v_sub(joint.origin, before.origin), axis)
        v = _flatten(_v_sub(after.origin, joint.origin), axis)
        if _v_norm(u) < 1e-9 or _v_norm(v) < 1e-9:
            continue
        bend = math.atan2(_v_dot(_v_cross(u, v), axis), _v_dot(u, v))
        if abs(bend) < _BRANCH_FLOOR or abs(bend) > math.pi - _BRANCH_FLOOR:
            continue
        if bend < 0.0:
            limits[gid] = (-math.pi - bend, -bend)
        else:
            limits[gid] = (-bend, math.pi - bend)
    return limits


def _plan_slider(plan: "RigPlan", lp: Loop, cj: Joint,
                 parent_of) -> Optional[SliderPlan]:
    """Turns a cut slide into an aim pair, or explains why it cannot.

    Both bodies either side of the slide must hang off a pin of their own,
    that pin's origin is the point the other half aims at. Without one there
    is nothing to aim from, and the loop falls back to the IK plan."""
    a_gid, c_gid = cj.parent_group, cj.child_group
    for gid in (a_gid, c_gid):
        if gid not in parent_of:
            plan.warnings.append(
                "loop {}: {} has no mount of its own, so the slide cannot be "
                "closed by aiming. Falling back to IK".format(lp.id, gid))
            return None
    a_parent, a_joint = parent_of[a_gid]
    c_parent, c_joint = parent_of[c_gid]
    if a_joint.origin is None or c_joint.origin is None:
        plan.warnings.append(
            "loop {}: a mount of the slide has no origin, so there is no "
            "point to aim at. Falling back to IK".format(lp.id))
        return None
    if a_parent == a_gid or c_parent == c_gid:
        return None
    return SliderPlan(
        loop=lp,
        closure_joint=cj,
        a_group=a_gid,
        c_group=c_gid,
        a_pivot=list(a_joint.origin),
        c_pivot=list(c_joint.origin),
        a_aim_parent=c_parent,
        c_aim_parent=a_parent,
    )


def build(manifest: Manifest, keep_names=None) -> RigPlan:
    """Plans the rig. Raises manifest.ManifestError on any inconsistency the
    exporter contract forbids, and on any dependency cycle the generated rig
    would hand to Blender's depsgraph.

    keep_names maps a group id to the bone name it already has in the
    scene. An update that keeps the animation depends on it: a keyframe
    names its bone, so a bone that stands for the same parts after an edit
    has to come back under the same name. Nothing else changes; a name
    that is taken by then is given up rather than fought over."""
    plan = RigPlan(manifest=manifest)
    plan.keep_names = dict(keep_names or {})
    # A universal joint built from its parts is driven exactly, before
    # anything reads the couplings or the loops (hooke.py says why IK
    # cannot do it).
    plan.hooke_notes = hooke.recognise(manifest)
    groups = manifest.group_by_id()
    joints = manifest.joint_by_id()

    closure_ids = set()
    for lp in manifest.loops:
        closure_ids.add(lp.closure_joint)

    # Free joints mark under-mated pairs. SCHEMA.md says leave them
    # unparented rather than silently fixing them, so they are not tree
    # edges, EXCEPT the two halves of a mirror pair: a free joint that
    # carries or drives a mirror coupling is deliberately ground-rooted
    # (the whole relation is the reflection, live corpus 14 sym4,
    # 2026-08-23), so both parent like any tree edge.
    mirror_driver_ids = set()
    mirror_normal_of = {}
    for j in manifest.joints:
        c = j.coupling
        if c is None or c.kind != "mirror" or c.mirror_plane_normal is None:
            continue
        mirror_normal_of[j.child_group] = list(c.mirror_plane_normal)
        if c.driver_joint:
            mirror_driver_ids.add(c.driver_joint)
            drv = joints.get(c.driver_joint)
            if drv is not None:
                mirror_normal_of[drv.child_group] = list(c.mirror_plane_normal)
    tree_joints = [j for j in manifest.joints
                   if (j.type != "free"
                       or j.id in mirror_driver_ids
                       or (j.coupling is not None and j.coupling.kind == "mirror"))
                   and j.id not in closure_ids]

    parent_of = {}  # child group id -> (parent group id, joint)
    forest = _UnionFind(groups.keys())
    for j in tree_joints:
        if j.parent_group == j.child_group:
            raise ManifestError(
                "joint {}: parent and child are both {}".format(j.id, j.parent_group))
        if groups[j.child_group].grounded:
            raise ManifestError(
                "joint {}: child {} is grounded. The exporter's spanning tree "
                "roots at grounded groups".format(j.id, j.child_group))
        if j.child_group in parent_of:
            raise ManifestError(
                "group {} has two tree parents (joints {} and {}). The "
                "exporter should have cut one as a loop".format(
                    j.child_group, parent_of[j.child_group][1].id, j.id))
        if not forest.union(j.child_group, j.parent_group):
            raise ManifestError(
                "joint {} closes a cycle the exporter did not declare as a "
                "loop".format(j.id))
        parent_of[j.child_group] = (j.parent_group, j)
        plan.joint_group[j.id] = j.child_group

    # Loop verification: a closure joint must be a genuine non-tree edge,
    # its endpoints already connected through the tree, and the tree path
    # between them plus the closure must be exactly the declared members.
    solved_by = {}       # group id -> the loop whose IK chain solves it
    for lp in manifest.loops:
        cj = joints[lp.closure_joint]
        if forest.find(cj.parent_group) != forest.find(cj.child_group):
            raise ManifestError(
                "loop {}: closure joint {} bridges two disconnected trees: "
                "it is a tree edge, not a loop closure".format(lp.id, cj.id))
        split = _branches(parent_of, cj.parent_group, cj.child_group)
        if split is None:
            raise ManifestError(
                "loop {}: no tree path between {} and {}".format(
                    lp.id, cj.parent_group, cj.child_group))
        ancestor, (groups_p, joints_p), (groups_c, joints_c) = split
        path_ids = {j.id for j in joints_p} | {j.id for j in joints_c} | {cj.id}
        if path_ids != set(lp.member_joints):
            raise ManifestError(
                "loop {}: member_joints {} do not match the tree path plus "
                "closure {}".format(lp.id, sorted(lp.member_joints), sorted(path_ids)))

        if lp.closure_kind == "none":
            # The exporter cut this loop so the TREE carries the motion: two
            # bodies sliding on the same ground, one now parented to the
            # other rather than sitting beside it. There is nothing left to
            # solve, and a solver here would rotate a body whose mates never
            # let it rotate (live ClampRig, 2026-08-24: the cutting head
            # on the lead screw rod).
            plan.open_loops.append(lp)
            continue

        if lp.closure_kind == "aim_pair":
            slider = _plan_slider(plan, lp, cj, parent_of)
            if slider is not None:
                plan.sliders.append(slider)
                continue
            # _plan_slider recorded why. An IK plan is still better than none.

        driver_side = None
        if lp.suggested_driver_joint:
            if any(j.id == lp.suggested_driver_joint for j in joints_p):
                driver_side = "p"
            elif any(j.id == lp.suggested_driver_joint for j in joints_c):
                driver_side = "c"
            else:
                plan.warnings.append(
                    "loop {}: suggested driver joint {} is not on the loop "
                    "path. Falling back to the closure orientation".format(
                        lp.id, lp.suggested_driver_joint))
        if driver_side is None:
            # The exporter's closure orientation is the only stable default:
            # parent side drives, child side is IK-solved.
            driver_side = "p"
        # The IK chain must contain at least one bone. When the chosen driven
        # side is the ancestor itself, the sides swap regardless of the
        # suggestion.
        driven_groups = groups_c if driver_side == "p" else groups_p
        if not driven_groups:
            driver_side = "c" if driver_side == "p" else "p"
            driven_groups = groups_c if driver_side == "p" else groups_p
            plan.warnings.append(
                "loop {}: driven side was empty. Driver/driven sides "
                "swapped".format(lp.id))
        if not driven_groups:
            raise ManifestError(
                "loop {}: closure joint {} connects a group to its own "
                "ancestor chain on both sides".format(lp.id, cj.id))
        if driver_side == "p":
            driver_groups = groups_p
            helper_parent = cj.parent_group
            ik_tip = cj.child_group
        else:
            driver_groups = groups_c
            helper_parent = cj.child_group
            ik_tip = cj.parent_group

        # A slide among the driven bodies gets a stretch bone (see
        # BonePlan.slide_name): its rest length has to cover every position
        # the solver can ask for, and no pin of the loop can travel further
        # from the slide's origin than the loop's links laid end to end.
        #
        # Each body is solved by ONE loop. Blender's solver joins IK chains
        # that share bones into one tree, and a tree with two targets
        # neither stretches (live actuator.sldasm, 2026-09-14: the two
        # links either side of the plunger each closed a loop through the
        # same slide, and the plunger sat still while both links merely
        # aimed) nor, driven from the slider instead, turns the crank both
        # rods hang from. A later loop is cut short at the first body an
        # earlier loop already solves: its chain keeps its own bodies,
        # which move to meet their helper, and the shared body comes from
        # the loop that owns it. That is how a one-degree mechanism is
        # solved by hand as well, one loop after another.
        driven_groups = list(driven_groups)
        for i, gid in enumerate(driven_groups):
            owner = solved_by.get(gid)
            if owner is None:
                continue
            if i == 0:
                plan.warnings.append(
                    "loop {}: its tip {} is already solved by loop {}. "
                    "This loop can only turn it".format(lp.id, gid, owner))
                i = 1
            driven_groups = driven_groups[:i]
            break
        # A loop with more than one input must not have every driven body
        # solved: the solver would spend a freedom the user is meant to
        # pose, and then fight whatever the user did with it. One bone per
        # spare input stays out of the chain, taken from the ROOT end,
        # where it drives the most.
        #
        # Live wrench.sldasm (2026-09-16, Oscar): four pins and a screw
        # around one ring is a five-bar with two inputs. Solved whole from
        # the screw, "the arm2 assembly can rotate around a pin on clamp2"
        # in SolidWorks and nothing in Blender could.
        spare = max(0, lp.mobility - 1)
        if spare and len(driven_groups) > spare:
            freed = driven_groups[len(driven_groups) - spare:]
            driven_groups = driven_groups[:len(driven_groups) - spare]
            plan.warnings.append(
                "loop {}: it takes {} inputs, so {} stays posable beside "
                "the driver".format(lp.id, lp.mobility,
                                    ", ".join(freed)))
        for gid in driven_groups:
            solved_by[gid] = lp.id
        slides = []
        for gid in driven_groups:
            pj = parent_of[gid][1]
            if pj.type in ("prismatic", "cylindrical"):
                slides.append((gid, pj))
        if slides:
            origins = [joints[jid].origin for jid in lp.member_joints
                       if joints[jid].origin is not None]
            for gid, pj in slides:
                reach = 0.0
                if pj.origin is not None:
                    reach = sum(_v_norm(_v_sub(o, pj.origin)) for o in origins)
                rest_len = max(2.0 * reach, 0.05)
                plan.slide_rest[gid] = max(plan.slide_rest.get(gid, 0.0), rest_len)
                if pj.translation_limit is not None:
                    plan.warnings.append(
                        "loop {}: slide {} is solved by the loop. Its travel "
                        "limit does not apply".format(lp.id, pj.id))
        plan.loops.append(LoopPlan(
            loop=lp,
            closure_joint=cj,
            helper_name="",
            effector_name="",
            helper_parent_group=helper_parent,
            ik_tip_group=ik_tip,
            driven_chain=list(driven_groups),
            driver_chain=list(driver_groups),
            chain_count=len(driven_groups) + len(slides),
            branch_limits=_branch_limits(lp, list(driven_groups), cj, parent_of),
        ))

    # Contact carrier chains fold into single posable bones AFTER loop
    # verification (a carrier inside a loop keeps its bone: IK needs the
    # chain) and BEFORE the dependency pre-flight, which must model the rig
    # actually generated.
    collapse_specs = _collapse_carriers(plan, groups, parent_of)
    skipped = set(plan.collapsed_carriers)

    # Pre-flight dependency check over the rig that WILL be generated:
    # child bone -> parent bone, IK chain bone -> helper target, helper ->
    # its parent bone, driven channel -> driver source bone. Runs before any
    # bpy mutation so a cycle aborts with the scene untouched.
    def bone_node(gid):
        return "bone:" + gid

    edges = {}
    labels = {}
    for g in groups.values():
        if g.id in skipped:
            continue
        edges.setdefault(bone_node(g.id), set())
        labels[bone_node(g.id)] = "bone {} ({})".format(g.id, g.name)
    for child, (parent, j) in parent_of.items():
        edges[bone_node(child)].add(bone_node(parent))
    for gid in plan.slide_rest:
        # The stretch bone sits between the slide's body and its parent.
        snode = "slide:" + gid
        labels[snode] = "stretch bone for the slide of " + gid
        edges.setdefault(snode, set())
        edges[snode].add(bone_node(parent_of[gid][0]))
        edges[bone_node(gid)].add(snode)
    for lplan in plan.loops:
        hnode = "helper:" + lplan.loop.id
        labels[hnode] = "helper for loop " + lplan.loop.id
        edges.setdefault(hnode, set())
        edges[hnode].add(bone_node(lplan.helper_parent_group))
        enode = "effector:" + lplan.loop.id
        labels[enode] = "effector for loop " + lplan.loop.id
        edges.setdefault(enode, set())
        edges[enode].add(bone_node(lplan.ik_tip_group))   # its parent bone
        edges[enode].add(hnode)                           # its IK target
        for gid in lplan.driven_chain:
            edges[bone_node(gid)].add(hnode)
            if gid in plan.slide_rest:
                edges["slide:" + gid].add(hnode)
    for splan in plan.sliders:
        for tag, group, aim_parent in (
                ("a", splan.a_group, splan.a_aim_parent),
                ("c", splan.c_group, splan.c_aim_parent)):
            anode = "aim:" + splan.loop.id + ":" + tag
            labels[anode] = "aim target {} for loop {}".format(tag, splan.loop.id)
            edges.setdefault(anode, set())
            edges[anode].add(bone_node(aim_parent))
            edges[bone_node(group)].add(anode)
    for j in manifest.joints:
        if j.coupling is None or j.id not in plan.joint_group:
            continue
        driver_id = j.coupling.driver_joint
        if j.coupling.kind == "screw":
            # The screw self-driver reads its own bone's location to drive
            # its rotation. Distinct channels on one bone are acyclic at
            # Blender's per-channel depsgraph granularity, so no edge.
            continue
        if driver_id is None:
            plan.warnings.append(
                "joint {}: {} coupling without a driver joint. The driver "
                "will be skipped".format(j.id, j.coupling.kind))
            continue
        if driver_id not in plan.joint_group:
            plan.warnings.append(
                "joint {}: coupling driver {} is not a tree joint. The "
                "driver will be skipped".format(j.id, driver_id))
            continue
        edges[bone_node(plan.joint_group[j.id])].add(
            bone_node(plan.joint_group[driver_id]))
    _assert_acyclic(edges, labels)

    # Build order: grounded roots, then free roots, then depth-first children,
    # every list sorted by group id so re-runs name and order identically.
    roots = [g for g in groups.values()
             if g.id not in parent_of and g.id not in skipped]
    for g in roots:
        if g.grounded:
            plan.grounded_groups.append(g.id)
        else:
            plan.free_groups.append(g.id)
    plan.grounded_groups.sort()
    plan.free_groups.sort()

    children = {}
    for child, (parent, j) in parent_of.items():
        children.setdefault(parent, []).append(child)
    for kids in children.values():
        kids.sort()

    aim_of = {}
    for splan in plan.sliders:
        aim_of[splan.a_group] = list(splan.c_pivot)
        aim_of[splan.c_group] = list(splan.a_pivot)

    # Every screw body's turn lives on a bone of its own: see
    # BonePlan.spin_name. A child joined about the screw's own axis rides
    # it, and so does a weld. Anything else stays on the body's bone and
    # takes the slide alone.
    spin_groups = set()
    spin_children = set()
    for child, (parent, cj) in parent_of.items():
        pj = parent_of.get(parent, (None, None))[1]
        if pj is None or pj.type != "screw" or pj.axis is None:
            continue
        if cj.type == "fixed":
            spin_children.add(child)
            continue
        # A ball with no cone limit and a free joint ship no axis. Such a
        # child cannot turn about the screw's axis, so it takes the slide
        # alone.
        if not cj.axis:
            continue
        a, b = _unit(cj.axis), _unit(pj.axis)
        if a is not None and b is not None and abs(_v_dot(a, b)) > 0.999:
            spin_children.add(child)
    for gid, (_parent, j) in parent_of.items():
        if j is not None and j.type == "screw":
            spin_groups.add(gid)

    taken_names = set()
    order = list(plan.grounded_groups) + list(plan.free_groups)
    stack = list(reversed(order))
    # The first grounded root becomes THE root: named after the assembly,
    # at the assembly origin (see BonePlan.root). Extra grounded islands
    # (two unconnected fixed components) keep their group identity.
    root_gid = plan.grounded_groups[0] if plan.grounded_groups else None
    assembly_stem = manifest.step_file.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in assembly_stem:
        assembly_stem = assembly_stem.rsplit(".", 1)[0]
    while stack:
        gid = stack.pop()
        group = groups[gid]
        parent_id, joint = parent_of.get(gid, (None, None))
        is_root = gid == root_gid
        bp = BonePlan(
            group=group,
            bone_name=_unique_name(
                plan.keep_names.get(group.id)
                or (assembly_stem if is_root and assembly_stem else group.name),
                taken_names, group.id),
            parent_group_id=parent_id,
            joint=joint,
            collapsed=collapse_specs.get(gid),
            root=is_root,
            mirror_normal=mirror_normal_of.get(gid),
            aim_at=aim_of.get(gid),
            parent_spin=gid in spin_children,
        )
        if gid in spin_groups:
            bp.spin_name = _unique_name(
                "SPN_" + bp.bone_name, taken_names, gid)
        if swing_cone(joint):
            bp.ball_def_name = _unique_name("DEF_" + bp.bone_name, taken_names, gid)
            bp.ball_pole_name = _unique_name("POLE_" + bp.bone_name, taken_names, gid)
            bp.ball_goal_name = _unique_name("GOAL_" + bp.bone_name, taken_names, gid)
        if gid in plan.slide_rest and parent_id is not None:
            bp.slide_name = _unique_name("SLD_" + bp.bone_name, taken_names, gid)
            bp.slide_rest_length = plan.slide_rest[gid]
        if (joint is not None and joint.coupling is not None
                and joint.coupling.kind == "cam" and joint.type == "prismatic"
                and parent_id is not None):
            if joint.coupling.follower_kind == "flat":
                bp.cam_rel_name = _unique_name("REL_" + bp.bone_name, taken_names, gid)
                bp.cam_off_name = _unique_name("OFF_" + bp.bone_name, taken_names, gid)
            else:
                bp.cam_prj_name = _unique_name("PRJ_" + bp.bone_name, taken_names, gid)
        plan.bones.append(bp)
        plan.bone_by_group[gid] = bp
        for child in reversed(children.get(gid, [])):
            stack.append(child)

    if len(plan.bones) != len(groups) - len(skipped):
        missing = sorted(set(groups) - set(plan.bone_by_group) - skipped)
        raise ManifestError(
            "groups {} are unreachable from any root. The tree walk lost "
            "them".format(missing))

    for lplan in plan.loops:
        lplan.helper_name = _unique_name(
            "HLP_" + lplan.loop.id, taken_names, lplan.loop.id)
        lplan.effector_name = _unique_name(
            "EFF_" + lplan.loop.id, taken_names, lplan.loop.id)
    for splan in plan.sliders:
        splan.a_aim_name = _unique_name(
            "AIM_" + splan.loop.id + "_a", taken_names, splan.loop.id)
        splan.c_aim_name = _unique_name(
            "AIM_" + splan.loop.id + "_c", taken_names, splan.loop.id)
    for gid in sorted(collapse_specs):
        spec = collapse_specs[gid]
        if spec.kind == "orbit_spin":
            spec.helper_name = _unique_name(
                "TGT_" + plan.bone_by_group[gid].bone_name, taken_names, gid)
        elif spec.kind == "cone_spin":
            base = plan.bone_by_group[gid].bone_name
            spec.def_name = _unique_name("DEF_" + base, taken_names, gid)
            spec.pole_name = _unique_name("POLE_" + base, taken_names, gid)
            spec.goal_name = _unique_name("GOAL_" + base, taken_names, gid)
            spec.frame_name = _unique_name("FRM_" + base, taken_names, gid)

    return plan
