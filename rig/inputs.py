# SPDX-License-Identifier: GPL-3.0-or-later
"""Which joint drives a mechanism.

A one-degree mechanism can usually be driven from more than one joint: a
slider-crank from its crank or from its slider. Which one is convenient
depends on what the rig is for, so the panel offers the list per MECHANISM
(the loops that share joints, which must all be driven from the same input).

The exporter's `mechanisms` block lists every input as a COMPLETE
alternative: the mechanism's loops re-chosen under that input, and the
joints whose parent and child swap because the new tree reaches them from
the other side. A choice is applied whole. Applying one loop's candidate on
its own left the other loops of the mechanism on the old driver: three
controls on one degree of freedom, then a tree the loop members no longer
described (live plunger.sldasm, 2026-09-15).

Manifests written before that block carry only per-loop `driver_candidates`;
for those the choice is applied loop by loop, as before.

Nothing here reads a name to decide anything. The labels are for the
person choosing; the candidates are joints.
"""

import copy
from typing import Dict, List, Optional

from .manifest import Manifest, Mechanism

_TYPE_WORDS = {
    "revolute": "hinge",
    "prismatic": "slide",
    "cylindrical": "slide and turn",
    "ball": "ball",
    "screw": "screw",
    "planar": "planar",
    "pin_slot": "pin in slot",
    "path": "path",
    "surface": "surface",
    "fixed": "weld",
    "free": "free",
}


def mechanisms(manifest: Manifest) -> List[List[str]]:
    """The mechanisms as lists of loop ids: the exporter's block when there
    is one, else loops grouped by shared joints. Loop ids sorted within a
    group, groups sorted by their first loop."""
    if manifest.mechanisms:
        return [sorted(m.loop_ids) for m in manifest.mechanisms]
    parent: Dict[str, str] = {lp.id: lp.id for lp in manifest.loops}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: Dict[str, str] = {}
    for lp in manifest.loops:
        for jid in lp.member_joints:
            if jid in owner:
                a, b = find(owner[jid]), find(lp.id)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                owner[jid] = lp.id
    groups: Dict[str, List[str]] = {}
    for lp in manifest.loops:
        groups.setdefault(find(lp.id), []).append(lp.id)
    out = [sorted(v) for v in groups.values()]
    out.sort(key=lambda g: g[0])
    return out


def _mechanism(manifest: Manifest, loop_ids: List[str]) -> Optional[Mechanism]:
    wanted = set(loop_ids)
    for m in manifest.mechanisms:
        if set(m.loop_ids) == wanted:
            return m
    return None


def _loops(manifest: Manifest, loop_ids: List[str]):
    wanted = set(loop_ids)
    return [lp for lp in manifest.loops if lp.id in wanted]


def candidates(manifest: Manifest, loop_ids: List[str]) -> List[str]:
    """Every input the mechanism can take, the exporter's choice first."""
    mech = _mechanism(manifest, loop_ids)
    if mech is not None:
        return [o.joint for o in mech.inputs if not _driven(manifest, o.joint)]
    out: List[str] = []
    for lp in _loops(manifest, loop_ids):
        for c in lp.driver_candidates:
            if c.joint not in out and not _driven(manifest, c.joint):
                out.append(c.joint)
    return out


def _driven(manifest: Manifest, joint_id: str) -> bool:
    """A joint whose channel a coupling writes cannot be an input: a driver
    is one-way, and pushing a cam's follower never turns the cam (live
    cam-follower, 2026-09-15: the lifter's slide was offered beside the
    cam's hinge and did nothing)."""
    j = manifest.joint_by_id().get(joint_id)
    return j is not None and j.coupling is not None and bool(j.coupling.driver_joint)


def current(manifest: Manifest, loop_ids: List[str]) -> Optional[str]:
    """The joint the mechanism is driven from now. Without the exporter's
    block: the suggested driver named by the most of its loops, the first
    on a tie."""
    mech = _mechanism(manifest, loop_ids)
    if mech is not None:
        if not mech.inputs:
            return None
        return mech.inputs[mech.active].joint
    votes: Dict[str, int] = {}
    order: List[str] = []
    for lp in _loops(manifest, loop_ids):
        jid = lp.suggested_driver_joint
        if not jid:
            continue
        if jid not in votes:
            order.append(jid)
        votes[jid] = votes.get(jid, 0) + 1
    if not order:
        return None
    return max(order, key=lambda j: (votes[j], -order.index(j)))


def label(manifest: Manifest, joint_id: str) -> str:
    """'link1 - hinge on mounting_bracket': the moving body, the joint in a
    word, the body it moves against."""
    joint = next((j for j in manifest.joints if j.id == joint_id), None)
    if joint is None:
        return joint_id
    names = {g.id: g.name for g in manifest.rigid_groups}
    word = _TYPE_WORDS.get(joint.type, joint.type)
    return "{} - {} on {}".format(
        names.get(joint.child_group, joint.child_group), word,
        names.get(joint.parent_group, joint.parent_group))


def _flip(manifest: Manifest, joint_ids: List[str]):
    by_id = manifest.joint_by_id()
    for jid in joint_ids:
        j = by_id.get(jid)
        if j is not None:
            j.parent_group, j.child_group = j.child_group, j.parent_group


def _apply_limits(manifest: Manifest, mech: Mechanism, old, new):
    """Leaves the old option's limits (the manifest's own come back) and
    takes the new option's. The manifest's own limits are kept the first
    time an option touches a joint."""
    by_id = manifest.joint_by_id()
    for jid in list(old.joint_limits) + list(new.joint_limits):
        j = by_id.get(jid)
        if j is not None and jid not in mech.original_limits:
            mech.original_limits[jid] = (j.rotation_limit, j.translation_limit)
    for jid in old.joint_limits:
        j = by_id.get(jid)
        if j is not None and jid in mech.original_limits:
            j.rotation_limit, j.translation_limit = mech.original_limits[jid]
    for jid, (rot, trans) in new.joint_limits.items():
        j = by_id.get(jid)
        if j is not None:
            j.rotation_limit, j.translation_limit = rot, trans


def apply(manifest: Manifest, loop_ids: List[str], joint_id: str) -> List[str]:
    """Drives the mechanism from `joint_id`. With the exporter's block the
    option is applied whole: the previous option's swaps are undone, the
    new one's made, and the mechanism's loops replaced. Without it, every
    loop that lists the joint as a candidate takes that candidate (driver,
    cut, closure kind). Returns the loops that changed."""
    mech = _mechanism(manifest, loop_ids)
    if mech is not None:
        index = next((i for i, o in enumerate(mech.inputs) if o.joint == joint_id), None)
        if index is None or index == mech.active:
            return []
        old, new = mech.inputs[mech.active], mech.inputs[index]
        _flip(manifest, old.flipped_joints)
        _flip(manifest, new.flipped_joints)
        _apply_limits(manifest, mech, old, new)
        keep = [lp for lp in manifest.loops if lp.id not in set(mech.loop_ids)]
        # Copies: the option stays pristine for the next switch back.
        fresh = [copy.deepcopy(lp) for lp in new.loops]
        manifest.loops[:] = keep + fresh
        mech.loop_ids = [lp.id for lp in fresh]
        mech.active = index
        return list(mech.loop_ids)

    changed = []
    for lp in _loops(manifest, loop_ids):
        for c in lp.driver_candidates:
            if c.joint != joint_id:
                continue
            if (lp.suggested_driver_joint, lp.closure_joint, lp.closure_kind) != (
                    c.joint, c.closure_joint, c.closure_kind):
                changed.append(lp.id)
            lp.suggested_driver_joint = c.joint
            lp.closure_joint = c.closure_joint
            lp.closure_kind = c.closure_kind
            break
    return changed


def key(manifest: Manifest, joint_id: str) -> Optional[str]:
    """An identity for a joint that survives re-export: joint ids and group
    ids renumber with the component walk, component paths do not. The two
    sides are sorted, so a flipped joint keeps its key."""
    joint = next((j for j in manifest.joints if j.id == joint_id), None)
    if joint is None:
        return None
    paths = {c.id: c.sw_path for c in manifest.components}
    members = {g.id: g.components for g in manifest.rigid_groups}

    def side(gid):
        return "+".join(sorted(paths.get(cid, cid) for cid in members.get(gid, [])))

    sides = sorted([side(joint.parent_group), side(joint.child_group)])
    return "{}|{}|{}".format(sides[0], sides[1], joint.type)


def find_by_key(manifest: Manifest, wanted: str) -> Optional[str]:
    for j in manifest.joints:
        if key(manifest, j.id) == wanted:
            return j.id
    return None
