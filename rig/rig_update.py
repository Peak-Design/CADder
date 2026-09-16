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
    out = {}
    for pb in arm_obj.pose.bones:
        gid = pb.get("RIG_group")
        parts = by_group.get(gid)
        if gid and parts:
            out[pb.name] = frozenset(parts)
    return out


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

    if mode == KEEP:
        report.kept = sorted(was)
        report.bones_after = report.bones_before
        return None, report
    if mode == APPEND and arm_obj is None:
        # Nothing to append to: the first rig of a scene is a fresh one.
        mode = REGENERATE
        report.mode = REGENERATE

    names = keep_names(before or {}, diff_mod.from_objects(objects)) \
        if mode == APPEND else {}
    plan = graph.build(manifest, keep_names=names)
    result = rig_build.build(
        context, manifest, plan, frame_rows=frame_rows,
        into=arm_obj if mode == APPEND else None)

    now = {b.name for b in result.armature_object.data.bones}
    report.bones_after = len(now)
    report.kept = sorted(was & now)
    report.added = sorted(now - was)
    report.removed = sorted(was - now)
    report.warnings = list(result.warnings)
    return result, report
