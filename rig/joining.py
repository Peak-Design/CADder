# SPDX-License-Identifier: GPL-3.0-or-later
"""Joining one rig into another.

A machine is not exported all at once. A subassembly gets its own manifest
and its own rig, is dropped into place, and then has to become part of the
machine: its bones in the machine's armature, its root riding whichever bone
of the machine carries it, and its geometry following.

WHAT BLENDER ALREADY DOES. `object.join` on two armatures is far better than
it looks. Measured on 5.1 (2026-08-25) with the sub-rig offset AND rotated:
every bone keeps its world position to 3e-8, because the object transform is
baked into the bones; constraint subtargets and DRIVER targets are both
remapped, including across the `.001` rename a colliding bone name gets;
custom shapes, bone custom properties, channel locks and bone collections
all survive, and collections merge by name. Geometry bone-parented to the
joined armature is re-pointed at the survivor and does not move.

So this module is not a re-implementation of join. It is the four things
join cannot know:

  1. WHICH BONE IS WHICH afterwards. Group ids restart at g000 for every
     manifest, so after a join two bones claim g000 and the id stops naming
     anything. Bones carry the manifest they came from (rig_build tags them)
     and the armature accumulates the list, so `parenting` can key on the
     pair. Without this, half the geometry re-parents to the other
     assembly's bones — silently, and looking almost right.

  2. WHERE IT ATTACHES. The joined rig's root is parented to a bone of the
     host, which is what makes the subassembly ride the machine rather than
     merely share an armature with it.

  3. THAT NOTHING MOVED. Every bone and every object is measured before and
     after, and the report says so in millimetres.

  4. RE-PARENTING the geometry, which is `parenting.relink` — it works from
     the tags, so it does not care that bones were renamed.

REST POSE. Parenting a bone under a POSED bone moves it: a bone's rest
matrix is absolute, so the pose composes on top of a parent that is no
longer where the rest frame says. The attach point is therefore required to
be at rest, and the check names the bone rather than guessing. Nothing else
about the join needs it, and aligning the sub-rig is an OBJECT transform, so
this never conflicts with the alignment the user just did.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

try:
    import bpy
except ImportError:
    bpy = None

from . import parenting

_REST_TOL = 1e-6


@dataclass
class JoinReport:
    host: str = ""
    joined: List[str] = field(default_factory=list)
    # Bones the join had to rename, old -> new, per joined rig.
    renamed: Dict[str, str] = field(default_factory=dict)
    bones_added: int = 0
    attached_to: str = ""
    # Roots of a joined rig that were hung off the attach bone.
    attached_roots: List[str] = field(default_factory=list)
    reparented: int = 0
    # (name, metres) for anything that moved. Should be empty.
    drift: List[Tuple[str, float]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def is_rig(obj):
    return (obj is not None and obj.type == "ARMATURE"
            and bool(obj.get("RIG_rig")))


def joinable(context):
    """The rigs a join would act on: the active armature hosts, every other
    selected armature is folded into it."""
    active = context.view_layer.objects.active
    if not is_rig(active):
        return None, []
    others = [o for o in context.selected_objects
              if is_rig(o) and o.name != active.name]
    return active, others


def bone_off_rest(arm_obj, bone_name):
    """How far a bone sits from its own rest pose, as (metres, radians).

    Parenting under it would carry that offset into everything below.
    """
    pb = arm_obj.pose.bones.get(bone_name)
    if pb is None:
        return 0.0, 0.0
    rest = arm_obj.data.bones[bone_name].matrix_local
    delta = rest.inverted() @ pb.matrix
    return ((pb.matrix.translation - rest.translation).length,
            abs(delta.to_quaternion().angle))


def _roots_of(arm_obj, source):
    """The bones of one manifest that hang from nothing — its ground."""
    out = []
    for pb in arm_obj.pose.bones:
        if (pb.get("RIG_source") or None) != source:
            continue
        if pb.parent is None:
            out.append(pb.name)
    return out


def _snapshot(context):
    context.view_layer.update()
    bones, objects = {}, {}
    for obj in bpy.data.objects:
        objects[obj.name] = obj.matrix_world.copy()
        if obj.type != "ARMATURE":
            continue
        for pb in obj.pose.bones:
            ident = (pb.get("RIG_source") or obj.name, pb.get("RIG_group")
                     or pb.name)
            bones[ident] = (obj.matrix_world @ pb.matrix).copy()
    return bones, objects


def join(context, host, others, attach_bone=None, relink=True):
    """Folds `others` into `host`, hanging each one's root off `attach_bone`.

    `attach_bone` defaults to the host's own root, which puts the
    subassembly on the machine's ground — the right answer when the part it
    bolts to does not move, and one re-parent away from the right answer
    when it does.
    """
    report = JoinReport(host=host.name, joined=[o.name for o in others])
    if not others:
        report.warnings.append("nothing selected to join into this rig")
        return report

    host_sources = parenting.rig_sources(host)
    incoming = {}
    for other in others:
        for source in parenting.rig_sources(other):
            if source in host_sources:
                report.warnings.append(
                    "{} was built from the same manifest as {}, so its bones "
                    "cannot be told apart afterwards; joined anyway"
                    .format(other.name, host.name))
            incoming.setdefault(source, other.name)

    if attach_bone is None:
        roots = [pb.name for pb in host.pose.bones
                 if pb.parent is None and pb.get("RIG_group")]
        attach_bone = roots[0] if roots else None
    if attach_bone is not None and attach_bone not in host.pose.bones:
        report.warnings.append(
            "no bone named {} to attach to; the joined bones were left "
            "unparented".format(attach_bone))
        attach_bone = None
    if attach_bone is not None:
        off_m, off_rad = bone_off_rest(host, attach_bone)
        if off_m > _REST_TOL or off_rad > _REST_TOL:
            report.warnings.append(
                "{} is {:.1f} mm and {:.3f} rad off its rest pose. Parenting "
                "to a posed bone would carry that offset into everything "
                "joined under it, so nothing was attached: clear the pose "
                "(Alt+G, Alt+R) and join again."
                .format(attach_bone, off_m * 1000.0, off_rad))
            attach_bone = None

    before_bones, before_objects = _snapshot(context)
    names_before = {pb.name for pb in host.pose.bones}
    # Identity survives the join on a custom property; the NAME may not.
    for other in others:
        for pb in other.pose.bones:
            pb["RIG_join_was"] = pb.name

    # Geometry riding a bone that HAS A PARENT BONE does not survive the
    # join in place. Blender re-points such an object at the surviving
    # armature and keeps its parent_inverse, but the join re-expresses every
    # bone in the host's space, so the inverse no longer inverts anything —
    # measured 2026-08-25: parts on a root bone stayed put, parts one bone
    # deeper jumped 1.38 m. Their world transforms are held here and put
    # back afterwards, which is also what makes `relink=False` safe.
    doomed = {o.name for o in others}
    riders = {}
    for obj in bpy.data.objects:
        parent = obj.parent
        if parent is not None and parent.name in doomed:
            riders[obj.name] = obj.matrix_world.copy()

    for obj in list(context.selected_objects):
        obj.select_set(False)
    for other in others:
        other.select_set(True)
    host.select_set(True)
    context.view_layer.objects.active = host
    bpy.ops.object.join()

    arrived = [pb for pb in host.pose.bones if "RIG_join_was" in pb.keys()]
    report.bones_added = len(arrived)
    for pb in arrived:
        was = pb["RIG_join_was"]
        if was != pb.name:
            report.renamed[was] = pb.name
        del pb["RIG_join_was"]
    if len(names_before) + report.bones_added != len(host.pose.bones):
        report.warnings.append("bone count does not add up after the join")

    # The armature now speaks for more than one manifest.
    merged = list(host_sources)
    for source in incoming:
        if source and source not in merged:
            merged.append(source)
    host["RIG_sources"] = merged

    if attach_bone is not None:
        wanted = []
        for source in incoming:
            wanted.extend(_roots_of(host, source))
        if wanted:
            bpy.ops.object.mode_set(mode="EDIT")
            try:
                target = host.data.edit_bones.get(attach_bone)
                for name in wanted:
                    eb = host.data.edit_bones.get(name)
                    if eb is None or eb is target:
                        continue
                    # Absolute rest matrices, so re-parenting does not move
                    # the bone. It only says what it follows from now on.
                    eb.use_connect = False
                    eb.parent = target
                    report.attached_roots.append(name)
            finally:
                bpy.ops.object.mode_set(mode="OBJECT")
            report.attached_to = attach_bone

    # Put the riders back where they were, before anything measures them or
    # re-parents them.
    context.view_layer.update()
    for name, world in riders.items():
        obj = bpy.data.objects.get(name)
        if obj is not None:
            obj.matrix_world = world
    context.view_layer.update()

    if relink:
        prep = parenting.relink(context, host)
        report.reparented = prep.bone_parented
        for name, drift in prep.violations:
            report.drift.append((name, drift))

    after_bones, after_objects = _snapshot(context)
    for ident, was in before_bones.items():
        now = after_bones.get(ident)
        if now is None:
            report.warnings.append("bone {} went missing".format(ident[1]))
            continue
        moved = (now.translation - was.translation).length
        if moved > _REST_TOL:
            report.drift.append(("bone " + str(ident[1]), moved))
    for name, was in before_objects.items():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue        # the joined armatures themselves are consumed
        moved = (obj.matrix_world.translation - was.translation).length
        if moved > _REST_TOL:
            report.drift.append((name, moved))
    return report
