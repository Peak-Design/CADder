# SPDX-License-Identifier: GPL-3.0-or-later
"""What to do with the rig when the assembly it was built from has changed.

Three answers, and the user picks:

  KEEP        Leave the rig exactly as it is. The parts are brought up to
              date around it. For a rig that has been taken over by hand.
  APPEND      Rebuild it inside the armature that is already there: bones
              for parts that are gone go, bones for new parts arrive, and
              a bone that stands for the same parts as before comes back
              under the SAME NAME. A keyframe names its bone, so that is
              what lets an animation survive an edit to the assembly.
  REGENERATE  Build a new rig from the new manifest, as a first send does.

APPEND rests on one thing: naming the same body the same way after an edit.
A rigid group's id (g000, g001) is handed out per export and moves as soon
as a part is added in front of another, so it names nothing on its own. The
set of PARTS in the group does, and every part carries an identity that
survives the edit (rig/diff.py). So the snapshot below records, for every
bone, which parts it drives; afterwards the same parts find the same name.
"""

from dataclasses import dataclass, field
from typing import Dict, List

from . import diff as diff_mod

try:
    import bpy
except ImportError:
    bpy = None

KEEP = "KEEP"
APPEND = "APPEND"
REGENERATE = "REGENERATE"
MODES = (KEEP, APPEND, REGENERATE)


@dataclass
class RigUpdateReport:
    mode: str = KEEP
    bones_before: int = 0
    bones_after: int = 0
    kept: List[str] = field(default_factory=list)      # bones that kept their name
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def describe(self):
        if self.mode == KEEP:
            return "the rig was left as it is"
        if self.mode == REGENERATE:
            return "the rig was built again from the new manifest"
        return ("%d bone(s) kept their name, %d added, %d removed"
                % (len(self.kept), len(self.added), len(self.removed)))


def snapshot(arm_obj, objects) -> Dict[str, frozenset]:
    """bone name -> the parts that bone drives, by their durable names.

    Taken BEFORE the geometry is brought up to date, because it reads the
    group ids of the export the rig was built from, and the update
    overwrites those with the new export's."""
    if arm_obj is None:
        return {}
    by_group: Dict[str, set] = {}
    for occurrence in diff_mod.from_objects(objects):
        if not occurrence.group_id:
            continue
        by_group.setdefault(occurrence.group_id, set()).add(
            occurrence.persistent or occurrence.path)
    # A limited ball and a cone contact hang their parts on a hidden DEF
    # bone. The bone the user poses and keys is the handle: the bone of
    # the same joint that has no group. Its name is the one to keep, or
    # the handle comes back as DEF_<name> and the keys lose it. A rig
    # built before the handle carried RIG_source is matched on the joint
    # alone, when only one handle claims that joint.
    handles = {}
    for pb in arm_obj.pose.bones:
        jid = pb.get("RIG_joint")
        if not jid or pb.get("RIG_group"):
            continue
        key = (pb.get("RIG_source") or None, jid)
        handles[key] = None if key in handles else pb.name
    out = {}
    for pb in arm_obj.pose.bones:
        gid = pb.get("RIG_group")
        parts = by_group.get(gid)
        if gid and parts:
            jid = pb.get("RIG_joint")
            name = None
            if jid:
                name = (handles.get((pb.get("RIG_source") or None, jid))
                        or handles.get((None, jid)))
            out[name or pb.name] = frozenset(parts)
    return out


@dataclass
class Released:
    """What release() took off a standing rig, to give back once the parts
    are bound to the rig again."""
    arm_obj: object = None
    objects: list = field(default_factory=list)   # were on a bone
    parts: list = field(default_factory=list)     # those and what they hold
    animation: bool = False
    action: object = None
    slot: object = None
    use_nla: bool = True
    posed: bool = False
    done: bool = False
    # (object, bone, parent inverse) for each object taken off a bone
    links: list = field(default_factory=list)

    def rebind(self, context):
        """Puts every part release() took off back on its bone, where it
        stands, when nothing bound it again. The job ended before relink
        (a failed import or rig update), or relink did not run. Without
        this the parts stay loose and the rig drives nothing. Returns how
        many went back."""
        arm = self.arm_obj
        try:
            bones = arm.data.bones
        except (ReferenceError, AttributeError):
            return 0
        back = []
        for obj, bone, mpi in self.links:
            try:
                if obj.parent is not None or bones.get(bone) is None:
                    continue
                back.append((obj, bone, mpi, obj.matrix_world.copy()))
            except ReferenceError:
                continue
        for obj, bone, mpi, world in back:
            obj.parent = arm
            obj.parent_type = "BONE"
            obj.parent_bone = bone
            obj.matrix_parent_inverse = mpi
        if back:
            context.view_layer.update()
        for obj, bone, mpi, world in back:
            obj.matrix_world = world
        return len(back)

    def finish(self):
        """Gives the rig its animation back. Safe to call twice: the job
        calls it when relink is done, and again whatever ended the job."""
        if self.done:
            return
        self.done = True
        if not self.animation or self.arm_obj is None:
            return
        try:
            anim = self.arm_obj.animation_data
            if anim is None:
                anim = self.arm_obj.animation_data_create()
            if self.action is not None:
                anim.action = self.action
                if self.slot is not None and hasattr(anim, "action_slot"):
                    anim.action_slot = self.slot
            anim.use_nla = self.use_nla
        except (ReferenceError, AttributeError, RuntimeError, TypeError):
            pass


def release(context, arm_obj, hold=None) -> Released:
    """Takes the parts off a standing rig at its rest pose, before an update
    moves any of them.

    The rest pose is the CAD pose the manifest describes, and relink binds
    each part where it finds it. A part left on a posed bone through an
    update is moved by the update against the pose, and then bound with the
    pose in it, so it carries the pose as an offset for ever after (Oscar,
    2026-09-22: "the parts that were parented to the old bones don't reset
    to the SW pose before being re-parented"). So the rig goes to rest
    first, and only the parts relink attached come off it, with their world
    transforms kept. A bone the user parented something to by hand keeps
    it.

    The animation comes off too, until the parts are bound again: a keyed
    bone would otherwise take its keyed pose back on the next depsgraph
    update, in the middle of the rebind.

    `hold` (a dict) gets the Released under "release" before anything
    changes, so the job can give everything back whatever happens after."""
    out = Released(arm_obj=arm_obj)
    if hold is not None:
        hold["release"] = out
    if arm_obj is None:
        return out
    from . import rig_build

    anim = arm_obj.animation_data
    if anim is not None:
        # The action cannot be changed while an NLA strip is tweaked, so
        # tweak mode ends first, as the other object modes do before a job.
        if getattr(anim, "use_tweak_mode", False):
            anim.use_tweak_mode = False
        out.animation = True
        out.action = anim.action
        out.slot = getattr(anim, "action_slot", None)
        out.use_nla = anim.use_nla
        anim.use_nla = False
        if anim.action is not None:
            anim.action = None
    out.posed = rig_build.rest_pose(arm_obj)
    context.view_layer.update()

    for obj in list(bpy.data.objects):
        if obj.parent is arm_obj and obj.parent_type == "BONE"                 and obj.get("RIG_parent_mode") == "BONE":
            world = obj.matrix_world.copy()
            out.links.append((obj, obj.parent_bone,
                              obj.matrix_parent_inverse.copy()))
            obj.parent = None
            obj.matrix_world = world
            out.objects.append(obj)
    context.view_layer.update()

    released = set(out.objects)
    for obj in bpy.data.objects:
        holder = obj
        while holder is not None and holder not in released:
            holder = holder.parent
        if holder is not None and obj.get("RIG_component_id"):
            out.parts.append(obj)
    return out


def alive(objects):
    """The objects of a list that still exist: an update removes the parts
    CAD removed."""
    out = []
    for obj in objects:
        try:
            obj.name
        except ReferenceError:
            continue
        out.append(obj)
    return out


def scale_changed(context, arm_obj) -> bool:
    """True when the rig was built at another Unit Scale than the scene
    has now. A rig built before the scale was recorded is taken as right."""
    built = arm_obj.get("RIG_unit_scale") if arm_obj is not None else None
    if built is None:
        return False
    from . import rig_build
    now = rig_build._unit_scale(context)
    return abs(float(built) - now) > 1e-9 * max(1.0, now)


def keep_names(before: Dict[str, frozenset], occurrences) -> Dict[str, str]:
    """new group id -> the bone name that group's parts already have.

    A group whose parts are not exactly one earlier bone's parts is left
    out, and gets a new name: half a body is not that body."""
    by_parts = {}
    for name, parts in before.items():
        # A set of parts that named two bones names neither.
        by_parts[parts] = None if parts in by_parts else name
    out = {}
    for gid, parts in diff_mod.group_identity(occurrences).items():
        name = by_parts.get(parts)
        if name:
            out[gid] = name
    return out


def apply(context, mode, manifest, arm_obj, objects, frame_rows=None,
          before=None):
    """Does what `mode` says. Returns (BuildResult or None, report).

    `before` is the snapshot taken ahead of the geometry update; without
    one, APPEND can still rebuild in place, but every bone is free to be
    renamed and an animation may lose its target."""
    from . import graph, rig_build

    report = RigUpdateReport(mode=mode)
    if arm_obj is not None:
        report.bones_before = len(arm_obj.data.bones)
        was = {b.name for b in arm_obj.data.bones}
    else:
        was = set()

    if arm_obj is None and mode in (KEEP, APPEND):
        # Nothing to keep or append to: the first rig of a scene is a fresh
        # one, and so is the rig of an assembly whose rig went with the
        # import this send replaced.
        mode = REGENERATE
        report.mode = REGENERATE
    if mode == KEEP and scale_changed(context, arm_obj):
        # The bones were built at another Unit Scale, and the parts come in
        # at this one: kept, the rig no longer meets its parts. Rebuilt in
        # place, every bone keeps its name, so an animation keeps its bones.
        # A locked rig does not come here: the job keeps it as it is.
        mode = APPEND
        report.mode = APPEND
        report.warnings.append(
            "The Unit Scale changed after the rig was built, so the rig was "
            "built again in place and not kept")
    if mode == KEEP:
        report.kept = sorted(was)
        report.bones_after = report.bones_before
        return None, report

    names = keep_names(before or {}, diff_mod.from_objects(objects)) \
        if mode == APPEND else {}
    plan = graph.build(manifest, keep_names=names)
    result = rig_build.build(
        context, manifest, plan, frame_rows=frame_rows,
        into=arm_obj if mode == APPEND else None)
    if mode == REGENERATE:
        # The input the user chose for each mechanism is kept on the
        # armature, and the old one went with its choices. The manifest
        # already carries them (load_manifest read them off the old rig),
        # so the new rig takes them as Build Rig writes them. Without this
        # the next send went back to the default input.
        from . import ui
        try:
            ui._store_choices(context, result.armature_object)
        except AttributeError as exc:
            print("[CADLink] the mechanism inputs were not kept:", exc)

    now = {b.name for b in result.armature_object.data.bones}
    report.bones_after = len(now)
    report.kept = sorted(was & now)
    report.added = sorted(now - was)
    report.removed = sorted(was - now)
    report.warnings.extend(result.warnings)
    return result, report
