# SPDX-License-Identifier: GPL-3.0-or-later
"""What changed between the assembly in the scene and the one the CAD
application just sent.

A send replaces everything, which is right the first time and wrong every
time after it: the work done in Blender since (materials, modifiers, the
rig, objects of the user's own) goes with it. To change only what changed,
the two assemblies have to be compared, and that needs every occurrence to
be identifiable across the edit that happened in between.

Two identities travel with every part:

  * `SWMESH_persistent_id`, which is SolidWorks' own reference
    (GetPersistReference3). It survives a rename, a rebuild and a new
    session, and it is the one to trust. A part inside a rigid subassembly
    carries the SUBASSEMBLY's, because that is the component, so the id
    alone does not name one part.
  * `SWMESH_path`, the occurrence path from the root ("lift-1/rod-2"). It
    names exactly one part, and it changes when an instance is renamed or
    re-ordered.

Neither is enough alone, so the match runs in passes, strongest first: both
together, then the persistent id where it is unambiguous, then the path.
What is left over on one side is new, and what is left over on the other
has gone.

No bpy: the records are plain data, so the comparison runs and is tested
without Blender.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# The tags native_import writes, read back here.
TAG_FILE = "SWMESH_file"
TAG_PATH = "SWMESH_path"
TAG_PERSISTENT = "SWMESH_persistent_id"
TAG_COMPONENT = "RIG_component_id"
TAG_GROUP = "RIG_group"
TAG_DEFINITION = "SWMESH_definition"
# The CAD transform the part arrived with, kept so the next export can be
# compared against what the CAD application said LAST time. The object's
# own matrix is not that: a part the user moved in Blender would read as
# moved in the CAD application.
TAG_TRANSFORM = "SWMESH_transform"
# What an object of the import is for. "node" is the empty of a
# subassembly, which carries the file and a path but is not a part.
TAG_ROLE = "SWMESH_role"

# How far a part may move and still count as standing still: a tenth of a
# micrometre, which is below anything a CAD system means by a move and well
# above the noise of a float32 round trip.
_STILL_M = 1e-7


@dataclass
class Occurrence:
    """One placement of one part, from either side of the comparison."""

    path: str = ""
    persistent: str = ""
    component_id: str = ""
    name: str = ""
    group_id: str = ""
    definition_id: int = -1
    transform: Optional[List[float]] = None      # 16, row-major
    # The scene side carries the object this came from. The export side
    # carries the swmesh instance. Neither is touched here.
    payload: object = None

    @property
    def key(self):
        return (self.persistent or "", self.path or "")


@dataclass
class Pair:
    """One occurrence that is in both assemblies."""

    old: Occurrence
    new: Occurrence
    how: str = "identity"        # which pass matched it
    moved: bool = False
    reshaped: bool = False       # a different definition, so new geometry
    regrouped: bool = False      # a different rigid group, so a different bone


@dataclass
class Diff:
    pairs: List[Pair] = field(default_factory=list)
    added: List[Occurrence] = field(default_factory=list)
    removed: List[Occurrence] = field(default_factory=list)

    @property
    def moved(self):
        return [p for p in self.pairs if p.moved]

    @property
    def reshaped(self):
        return [p for p in self.pairs if p.reshaped]

    @property
    def regrouped(self):
        return [p for p in self.pairs if p.regrouped]

    @property
    def structural(self):
        """True when the assembly is not the same set of parts in the same
        groups. A pose change alone is not structural, and the rig can be
        left alone for it."""
        return bool(self.added or self.removed or self.regrouped)

    def describe(self):
        return ("%d part(s) unchanged, %d added, %d removed, %d moved, "
                "%d re-tessellated"
                % (len(self.pairs) - len(self.moved) - len(self.reshaped),
                   len(self.added), len(self.removed), len(self.moved),
                   len(self.reshaped)))


def is_part(obj) -> bool:
    """True when this object of an import is a part occurrence.

    The import tags more than its parts. The empty of a subassembly carries
    the file and the subassembly's path, and an older build also gave it
    the component id of a rigid subassembly. Read as a part, it was an
    occurrence that no export has, so every update deleted it (and took
    the parts and the user's objects off it). A prototype of the instancing
    mode is geometry for the parts, not a placement.

    A part is a mesh, or an empty that instances a collection. An empty
    that instances nothing is a branch, whatever tags it carries."""
    if obj.get(TAG_ROLE) == "node" or obj.get("SWMESH_prototype"):
        return False
    if getattr(obj, "type", None) == "EMPTY" \
            and getattr(obj, "instance_type", "NONE") != "COLLECTION":
        return False
    return True


def from_objects(objects, stem=None) -> List[Occurrence]:
    """The occurrences a scene holds, read off the objects a direct send
    made. `stem` narrows it to one file's import."""
    out = []
    for obj in objects:
        try:
            if obj.get(TAG_FILE) is None:
                continue
            if stem is not None and obj.get(TAG_FILE) != stem:
                continue
            if not is_part(obj):
                continue
            path = obj.get(TAG_PATH) or ""
            component = obj.get(TAG_COMPONENT) or ""
            if not path and not component:
                continue
            out.append(Occurrence(
                path=path,
                persistent=obj.get(TAG_PERSISTENT) or "",
                component_id=component,
                name=obj.name,
                group_id=obj.get(TAG_GROUP) or "",
                definition_id=_int(obj.get(TAG_DEFINITION), -1),
                transform=_transform(obj.get(TAG_TRANSFORM)),
                payload=obj))
        except (AttributeError, ReferenceError):
            continue
    return out


def split_copies(occurrences: List[Occurrence], rank=None):
    """(occurrences, copies): one occurrence for each identity, and the
    copies of it that were made in Blender.

    Shift+D, Alt+D and a Full Copy of the scene copy the tags with the
    object, so the copy has the part's path and persistent id. The match
    pairs nothing that two old occurrences share, so the part and its copy
    both went to `removed`, and the update deleted both. `rank` orders the
    occurrences of one identity, the part first. Without it the shortest
    name wins, because a copy gets a ".001" name."""
    rank = rank or (lambda o: (len(o.name or ""), o.name or ""))
    groups: Dict[tuple, List[Occurrence]] = {}
    for occurrence in occurrences:
        # Only a path names one placement. An older import without paths
        # has the parts of a rigid subassembly under one component id,
        # and they are not copies of each other.
        if occurrence.path:
            groups.setdefault(occurrence.key, []).append(occurrence)
    copies = []
    for same in groups.values():
        if len(same) > 1:
            copies.extend(sorted(same, key=rank)[1:])
    extra = {id(o) for o in copies}
    return [o for o in occurrences if id(o) not in extra], copies


def from_scene_file(scene, manifest=None) -> List[Occurrence]:
    """The occurrences a .swmesh describes. The manifest adds the rigid
    group and the persistent id, which the geometry file does not carry."""
    persistent, groups = {}, {}
    for component in (manifest.components if manifest is not None else []):
        if component.sw_persistent_id:
            persistent[component.id] = component.sw_persistent_id
    for group in (manifest.rigid_groups if manifest is not None else []):
        for cid in group.components:
            groups[cid] = group.id
    out = []
    for inst in scene.instances:
        out.append(Occurrence(
            path=inst.path or "",
            persistent=persistent.get(inst.component_id, ""),
            component_id=inst.component_id,
            name=inst.name or inst.component_id,
            group_id=groups.get(inst.component_id, ""),
            definition_id=inst.definition_id,
            transform=list(inst.transform or []),
            payload=inst))
    return out


def compare(old: List[Occurrence], new: List[Occurrence]) -> Diff:
    """Pairs the two assemblies up. See the module docstring for the
    passes; each one only ever matches where the answer is unambiguous, so
    a doubtful pairing becomes an add and a remove rather than a part
    quietly turning into another part."""
    diff = Diff()
    left = list(old)
    right = list(new)

    for how, index in (("identity", _by_key),
                       ("persistent", _by_persistent),
                       ("path", _by_path)):
        if not left or not right:
            break
        used_left, used_right = _match(left, right, index, how, diff)
        left = [o for o in left if id(o) not in used_left]
        right = [o for o in right if id(o) not in used_right]

    diff.removed.extend(left)
    diff.added.extend(right)
    return diff


def _match(left, right, index, how, diff):
    """One pass. `index` maps an occurrence to its key for this pass; an
    empty key never matches, and a key that more than one occurrence on
    either side carries is left for the next pass. Occurrences are tracked
    by identity, never by value: two placements of one part differ in
    nothing this record holds."""
    lookup: Dict[object, List[Occurrence]] = {}
    for occurrence in right:
        key = index(occurrence)
        if key:
            lookup.setdefault(key, []).append(occurrence)
    counts: Dict[object, int] = {}
    for occurrence in left:
        key = index(occurrence)
        if key:
            counts[key] = counts.get(key, 0) + 1
    used_left, used_right = set(), set()
    for occurrence in left:
        key = index(occurrence)
        if not key or counts.get(key, 0) != 1:
            continue
        candidates = lookup.get(key)
        if not candidates or len(candidates) != 1:
            continue
        partner = candidates[0]
        if id(partner) in used_right:
            continue
        used_left.add(id(occurrence))
        used_right.add(id(partner))
        diff.pairs.append(_pair(occurrence, partner, how))
    return used_left, used_right


def _pair(old: Occurrence, new: Occurrence, how: str) -> Pair:
    pair = Pair(old=old, new=new, how=how)
    pair.reshaped = old.definition_id != new.definition_id
    pair.regrouped = bool(old.group_id) and bool(new.group_id) \
        and old.group_id != new.group_id
    pair.moved = _moved(old.transform, new.transform)
    return pair


def _moved(a, b) -> bool:
    """True when the two transforms differ. A scene occurrence carries no
    CAD transform (its object holds the Blender pose, which is not the same
    thing), so a missing side means "not known to have moved"."""
    if not a or not b or len(a) != 16 or len(b) != 16:
        return False
    return any(abs(float(a[i]) - float(b[i])) > _STILL_M for i in range(16))


def _by_key(occurrence: Occurrence):
    if occurrence.persistent and occurrence.path:
        return ("both", occurrence.persistent, occurrence.path)
    return None


def _by_persistent(occurrence: Occurrence):
    return ("persistent", occurrence.persistent) if occurrence.persistent else None


def _by_path(occurrence: Occurrence):
    return ("path", occurrence.path) if occurrence.path else None


def _transform(raw):
    """The 16 numbers a tag holds, or None. An older import carries none,
    and then nothing is known to have moved, which is the safe answer: the
    part keeps the place it is in."""
    if raw is None:
        return None
    try:
        values = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    return values if len(values) == 16 else None


def _int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def group_identity(occurrences: List[Occurrence]) -> Dict[str, frozenset]:
    """rigid group id -> the parts in it, as the set of names that survive
    an export: the persistent id where there is one, the occurrence path
    otherwise.

    This is what makes a BONE identifiable across an edit. A group id
    (g000, g001) is handed out per export and moves when a part is added in
    front of another, so it names nothing on its own. The set of parts does.
    """
    out: Dict[str, set] = {}
    for occurrence in occurrences:
        if not occurrence.group_id:
            continue
        out.setdefault(occurrence.group_id, set()).add(
            occurrence.persistent or occurrence.path)
    return {gid: frozenset(parts) for gid, parts in out.items()}
