# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometry matching: manifest components -> scene objects.

Blender object names are display labels: 63-byte truncation and ".001"
dedup make them useless as identity, so nothing here matches on obj.name
except the last-resort fuzzy step, which exists for foreign importers that
wrote no STEP_* properties at all.

The five-step cascade runs strictly in order, each step only over the
still-unmatched components and still-unclaimed objects. Results are written
back as obj["RIG_component_id"] / obj["RIG_group"], and a re-run pre-seeds
from those tags, so matching is idempotent.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .manifest import Component, Manifest

# Importable under plain Python. Every entry point that needs Blender
# checks for bpy itself.
try:
    import bpy
except ImportError:
    bpy = None

_TRANSLATION_TOL = 1e-4   # metres
_ROTATION_TOL = 1e-3      # radians
_BBOX_REL_TOL = 0.01

_METHOD_NAMES = {
    0: "existing",
    1: "exact",
    2: "path",
    3: "transform",
    4: "bbox",
    5: "fuzzy",
    6: "collection",
}


@dataclass
class StepKey:
    name: Optional[str] = None
    uuid: Optional[int] = None
    parent: Optional[int] = None
    file: Optional[str] = None
    applied_scale: Optional[float] = None
    instance_of: Optional[str] = None


@dataclass
class MatchEntry:
    component_id: str
    object_name: str
    step: int
    confidence: str
    # Every object this component owns. A plain part owns one. A rigid
    # SUBASSEMBLY owns all of its parts, because the manifest treats the
    # subassembly as one body and names the assembly occurrence rather than
    # the parts inside it. Appended last and defaulted: the tests and
    # native_import build MatchEntry positionally.
    object_names: List[str] = field(default_factory=list)
    # Set when the component was resolved to a collection subtree rather
    # than to an object of its own: there is then no single object holding
    # the occurrence's pose, which pose_sync has to know.
    collection_name: Optional[str] = None
    # The object's occurrence path (SWMESH_path) on the direct link. The
    # name above is the one the object had when the report was made, and
    # the user can rename it. The component id and this path find it
    # again, also inside a rigid subassembly, where one component id
    # names several parts.
    object_path: Optional[str] = None

    def __post_init__(self):
        # Every other producer of this record (native_import, the tests)
        # names one object. The list is the general form of the same thing.
        if not self.object_names and self.collection_name is None:
            self.object_names = [self.object_name]


@dataclass
class MatchReport:
    matched: List[MatchEntry] = field(default_factory=list)
    ambiguous: List[Tuple[str, List[str]]] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    unclaimed_objects: List[str] = field(default_factory=list)
    # Set when the whole report has one cause worth saying out loud, e.g.
    # the import mode left every subassembly occurrence without an object.
    hint: Optional[str] = None
    # Per-occurrence remarks from the subassembly step: what it could not
    # resolve, and what it resolved on thinner evidence than usual.
    notes: List[str] = field(default_factory=list)
    # The scene-frame transform: manifest coordinates (Blender units) ->
    # where the geometry actually sits. Identity when the import kept the
    # manifest's Z-up frame. A rotation when the user imported with another
    # up axis. rig_build applies it to every bone it places.
    frame_rows: Optional[List[List[float]]] = None
    frame_agree: int = 0


def get_step_key(obj) -> StepKey:
    """The only place STEP_* custom properties are touched. Foreign importers
    (no CADder metadata) come back as an all-None key and simply fall
    through to the fuzzy step."""
    key = StepKey()
    try:
        key.name = obj.get("STEP_name")
        key.uuid = obj.get("STEP_uuid")
        key.parent = obj.get("STEP_parent")
        key.file = obj.get("STEP_file")
        key.applied_scale = obj.get("STEP_applied_scale")
        key.instance_of = obj.get("STEP_instance_of")
    except (AttributeError, TypeError):
        pass
    return key


def _norm_path(path: str) -> str:
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    return "/".join(parts)


def _strip_instance_suffix(segment: str) -> str:
    return re.sub(r"-\d+$", "", segment)


def _loose_path(path: str) -> str:
    return "/".join(_strip_instance_suffix(p) for p in _norm_path(path).split("/") if p)


def _fuzzy_label(text: str) -> str:
    # ".NNN" is Blender's duplicate suffix, "-N" is the CAD instance suffix.
    # Both are identity noise for a last-resort name comparison.
    label = str(text)
    while True:
        stripped = re.sub(r"\.\d{3,}$", "", label)
        stripped = re.sub(r"-\d+$", "", stripped)
        if stripped == label:
            break
        label = stripped
    return label.casefold()


def _strip_dedup(name: str) -> str:
    """Blender appends '.001' to a duplicate datablock name. The CAD node
    name underneath it is what the manifest knows.

    Blender's suffix is always at least three digits. A product number is
    not: the hydraulic cylinder of live TongRig (2026-09-14) is
    "42S TC100.2", its body "42S TC100.1.02", and stripping every dotted
    number folded them and their nested subassemblies into one label, so
    the manifest's one occurrence met four collections and claimed none.
    """
    label = str(name)
    while True:
        stripped = re.sub(r"\.\d{3,}$", "", label)
        if stripped == label:
            return label
        label = stripped


def _collection_label(col) -> str:
    """The CAD name of a collection: what the importer stamped on it when
    it has, the label with Blender's duplicate suffix removed otherwise."""
    try:
        name = col.get("STEP_name")
    except (AttributeError, TypeError):
        name = None
    return str(name) if name else _strip_dedup(col.name)


def collect_collections(collections=None):
    if collections is not None:
        return list(collections)
    if bpy is None:
        return []
    return list(bpy.data.collections)


def _collection_paths(collections):
    """name -> the CAD path of that collection, root first.

    CADder's "Tree collection" import builds one collection per assembly
    node and links each object into the collection of the node that owns it
    (main.py: hierarchy_collections[node.index], then
    hierarchy_collections[obj["STEP_parent"]].objects.link(obj)). So the
    collection nesting IS the assembly tree, and it is the only place a
    subassembly occurrence exists at all in that mode: no object is made
    for a node that carries no shape of its own.

    bpy gives a collection its children and never its parent, so the parent
    map is built by inversion.
    """
    parent = {}
    for col in collections:
        for child in col.children:
            parent[child.name] = col.name
    by_name = {col.name: col for col in collections}
    paths = {}
    for col in collections:
        segs, cur, seen = [], col.name, set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            segs.append(_collection_label(by_name[cur]) if cur in by_name
                        else _strip_dedup(cur))
            cur = parent.get(cur)
        segs.reverse()
        paths[col.name] = segs
    return parent, paths


def _container_depth(paths, wanted) -> int:
    """How many leading segments of a collection path belong to the IMPORT
    rather than to the STEP tree: "<file>.hierarchy", plus any collection
    the user has since nested the import inside. Voted rather than assumed:
    the offset that lines the most collection paths up with a path the
    manifest actually names."""
    best, best_score = 0, -1
    for depth in range(0, 6):
        score = 0
        for segs in paths.values():
            if len(segs) > depth and "/".join(segs[depth:]) in wanted:
                score += 1
        if score > best_score:
            best, best_score = depth, score
    return best if best_score > 0 else 0


def _subtree_objects(col):
    """Every object in a collection or below it, deterministically ordered.
    Written out rather than using Collection.all_objects so the same code
    runs against the fake collections in the tests."""
    out, stack, seen = [], [col], set()
    while stack:
        cur = stack.pop()
        key = cur.name
        if key in seen:
            continue
        seen.add(key)
        out.extend(list(cur.objects))
        stack.extend(list(cur.children))
    out.sort(key=lambda o: o.name)
    return out


def _occurrence_path(obj, by_uuid: Dict[Tuple[Optional[str], int], object]) -> Optional[str]:
    """Rebuilds the STEP occurrence path from STEP_uuid/STEP_parent chains.
    CADder's artificial root node (parent -1, labelled '<file>.empties') is
    not part of the STEP tree and never enters the path. FLAT/TREE imports
    have no ancestor objects, so the chain degrades to the leaf name and the
    exact-path step simply fails over to the later steps."""
    segments = []
    cur = obj
    for _ in range(256):
        key = get_step_key(cur)
        if key.uuid is None:
            break
        if key.parent is not None and key.parent == -1:
            break
        segments.append(key.name or "")
        if key.parent is None:
            break
        cur = by_uuid.get((key.file, key.parent))
        if cur is None:
            break
    if not segments:
        return None
    segments.reverse()
    return "/".join(segments)


def _collection_occurrence_path(obj, col_of_object, paths, depth) -> Optional[str]:
    """The object's occurrence path taken from its collection ancestry.

    This is the TREE-mode counterpart of _occurrence_path: there the
    STEP_uuid/STEP_parent chain dead-ends at the first assembly node,
    because no object was made for it, and every leaf's rebuilt path
    collapses to its own name."""
    col = col_of_object.get(obj.name)
    if col is None:
        return None
    segs = paths.get(col, [])
    if len(segs) <= depth:
        return None
    name = get_step_key(obj).name or obj.name
    return "/".join(list(segs[depth:]) + [_strip_dedup(name)])


def _object_collections(collections, paths):
    """object name -> the collection that places it deepest in the tree. An
    object linked into several (a FLAT_AND_TREE import links it into both a
    hierarchy collection and a by-name one) is read through the collection
    that says most about where it sits."""
    out = {}
    for col in collections:
        depth = len(paths.get(col.name, ()))
        for obj in col.objects:
            prev = out.get(obj.name)
            if prev is None or depth > len(paths.get(prev, ())):
                out[obj.name] = col.name
    return out


_LAYOUT_TOL = 1e-4          # metres


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def _invert_rows(rows):
    """Inverse of a 4x4 with a [0,0,0,1] bottom row, via the 3x3 adjugate:
    the import can bake a scale into the matrix, so no rigid shortcut."""
    d = (rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
         - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
         + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0]))
    if abs(d) < 1e-15:
        return None
    cof = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            m = [[rows[r][c] for c in range(3) if c != j]
                 for r in range(3) if r != i]
            cof[j][i] = ((-1) ** (i + j)) * (m[0][0] * m[1][1]
                                             - m[0][1] * m[1][0]) / d
    t = [rows[i][3] for i in range(3)]
    it = [-(cof[i][0] * t[0] + cof[i][1] * t[1] + cof[i][2] * t[2])
          for i in range(3)]
    return [[cof[0][0], cof[0][1], cof[0][2], it[0]],
            [cof[1][0], cof[1][1], cof[1][2], it[1]],
            [cof[2][0], cof[2][1], cof[2][2], it[2]],
            [0.0, 0.0, 0.0, 1.0]]


def _internal_layout(objs, frame_rows, scene_scale):
    """Where an occurrence's parts sit IN ITS OWN FRAME, if frame_rows is
    that occurrence's frame: T^-1 applied to every part's world matrix.

    This is what identifies an occurrence without ever knowing its frame.
    STEP stores ONE internal layout per product, so every occurrence of that
    product has the same one: under the right pairing of occurrences to
    manifest components the layouts agree exactly, and under a wrong pairing
    they do not."""
    inv = _invert_rows(frame_rows)
    if inv is None:
        return None
    # Ordered by STEP_uuid, which is the node's position in a pre-order walk
    # of the tree. Two occurrences of one product are walked identically, so
    # ordering by it puts corresponding parts opposite each other. Ordering
    # by position instead would compare a part of one occurrence against a
    # DIFFERENT part of the other wherever several parts share a name, and
    # in an assembly of fasteners, most of them do (live ClampRig,
    # 2026-08-24: a 98-part module read as laid out differently from its own
    # twin).
    ordered = sorted(objs, key=lambda o: (get_step_key(o).uuid is None,
                                          get_step_key(o).uuid or 0, o.name))
    rows = []
    for obj in ordered:
        local = _mat_mul(inv, _matrix_rows(obj))
        name = get_step_key(obj).name or obj.name
        rows.append((_strip_dedup(name),
                     local[0][3] * scene_scale,
                     local[1][3] * scene_scale,
                     local[2][3] * scene_scale))
    return rows


def _layouts_agree(a, b) -> bool:
    if a is None or b is None or len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if ra[0] != rb[0]:
            return False
        if max(abs(ra[i] - rb[i]) for i in (1, 2, 3)) >= _LAYOUT_TOL:
            return False
    return True


def _centroid(objs, scene_scale):
    if not objs:
        return None
    acc = [0.0, 0.0, 0.0]
    for obj in objs:
        rows = _matrix_rows(obj)
        for i in range(3):
            acc[i] += rows[i][3]
    return [v / len(objs) * scene_scale for v in acc]


def _matrix_rows(obj) -> List[List[float]]:
    return [[float(v) for v in row] for row in obj.matrix_world]


def _scene_scale() -> float:
    if bpy is None:
        return 1.0
    try:
        scale = float(bpy.context.scene.unit_settings.scale_length)
    except (AttributeError, TypeError):
        return 1.0
    return scale if scale > 0.0 else 1.0


def _rotation_columns(rows) -> Optional[List[List[float]]]:
    """Unit-length rotation columns of a 4x4, scale divided out. A degenerate
    column (STEP_applied_scale left in the matrix can be 0 only through
    corruption) disqualifies the object from the transform step."""
    cols = []
    for c in range(3):
        v = [rows[0][c], rows[1][c], rows[2][c]]
        n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        if n < 1e-12:
            return None
        cols.append([x / n for x in v])
    return cols


def _rotation_angle(cols_a, cols_b) -> float:
    # trace(A^T B) via column dot products. Acos clamped against fp drift.
    trace = 0.0
    for c in range(3):
        trace += (cols_a[c][0] * cols_b[c][0]
                  + cols_a[c][1] * cols_b[c][1]
                  + cols_a[c][2] * cols_b[c][2])
    return math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0)))


# ── Scene-frame estimation ──────────────────────────────────────────────
# The manifest is Z-up SolidWorks global. The importer may have rotated the
# geometry (CADder's up-axis option, or any foreign importer's convention).
# Comparing manifest transforms against object transforms directly would
# then fail everywhere, and a rig built in the manifest frame would not
# touch the geometry. The orientation-independent matches (steps 0–2, name
# and path based) anchor an estimate of the frame transform F with
# obj.matrix_world ≈ F @ component_transform. The transform step and the
# rig builder both work under F afterwards.

def _normalized_rot(rows):
    cols = _rotation_columns(rows)
    if cols is None:
        return None
    return [[cols[c][r] for c in range(3)] for r in range(3)]


def _rot_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rot_t(a):
    return [[a[j][i] for j in range(3)] for i in range(3)]


def _rot_apply(a, v):
    return [sum(a[i][k] * v[k] for k in range(3)) for i in range(3)]


def _snap_frame_rot(r):
    """Up-axis conversions are exact ±90°/180° rotations. Snapping removes
    the fp noise the anchor pair injected. Anything that does not snap to a
    legal right-handed axis swap comes back unchanged."""
    snapped = []
    for i in range(3):
        row = []
        for j in range(3):
            v = r[i][j]
            if abs(v) < 0.02:
                row.append(0.0)
            elif abs(v - 1.0) < 0.02:
                row.append(1.0)
            elif abs(v + 1.0) < 0.02:
                row.append(-1.0)
            else:
                return r
        snapped.append(row)
    for i in range(3):
        if sum(abs(x) for x in snapped[i]) != 1.0:
            return r
        if sum(abs(snapped[j][i]) for j in range(3)) != 1.0:
            return r
    det = (snapped[0][0] * (snapped[1][1] * snapped[2][2] - snapped[1][2] * snapped[2][1])
           - snapped[0][1] * (snapped[1][0] * snapped[2][2] - snapped[1][2] * snapped[2][0])
           + snapped[0][2] * (snapped[1][0] * snapped[2][1] - snapped[1][1] * snapped[2][0]))
    if abs(det - 1.0) > 1e-9:
        return r
    return snapped


def _frame_from_rot(rot, t=(0.0, 0.0, 0.0)):
    return [
        [rot[0][0], rot[0][1], rot[0][2], t[0]],
        [rot[1][0], rot[1][1], rot[1][2], t[1]],
        [rot[2][0], rot[2][1], rot[2][2], t[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def identity_frame():
    return _frame_from_rot([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])


# The up-axis conversions an importer plausibly applied, tried when no
# name/path anchor exists: Z-up kept, Y-up both ways, and upside down.
_CANDIDATE_ROTS = (
    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]],   # +90° about X
    [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]],   # −90° about X
    [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]],  # 180° about X
)


def apply_frame(frame, rows):
    out = []
    for i in range(4):
        out.append([sum(frame[i][k] * rows[k][j] for k in range(4)) for j in range(4)])
    return out


def _component_rows_units(component: Component, unit_scale: float):
    rows = [list(component.transform[i]) for i in range(4)]
    for i in range(3):
        rows[i][3] *= unit_scale
    return rows


def _frame_from_anchor(obj_rows, comp_rows):
    r_obj = _normalized_rot(obj_rows)
    r_comp = _normalized_rot(comp_rows)
    if r_obj is None or r_comp is None:
        return None
    rot = _snap_frame_rot(_rot_mul(r_obj, _rot_t(r_comp)))
    rt = _rot_apply(rot, [comp_rows[i][3] for i in range(3)])
    t = [obj_rows[i][3] - rt[i] for i in range(3)]
    t = [0.0 if abs(v) < 1e-7 else v for v in t]
    return _frame_from_rot(rot, t)


def _frame_agrees(frame, obj_rows, comp_rows, scene_scale,
                  t_tol=_TRANSLATION_TOL, r_tol=_ROTATION_TOL) -> bool:
    pred = apply_frame(frame, comp_rows)
    for i in range(3):
        if abs(obj_rows[i][3] - pred[i][3]) * scene_scale > t_tol:
            return False
    a = _rotation_columns(obj_rows)
    b = _rotation_columns(pred)
    if a is None or b is None:
        return False
    return _rotation_angle(a, b) <= r_tol


def estimate_frame(anchor_pairs, scene_scale):
    """anchor_pairs: (obj_rows, comp_rows_units) from orientation-independent
    matches. Each anchor proposes a frame. The one most anchors agree with
    wins: a single mis-tagged object cannot hijack the frame. Returns
    (frame_rows, agree_count). Consensus tolerances are looser than the
    match tolerances: anchors vote on the frame, they are not re-matched."""
    best = None
    best_score = -1
    for obj_rows, comp_rows in anchor_pairs:
        f = _frame_from_anchor(obj_rows, comp_rows)
        if f is None:
            continue
        score = sum(1 for o, c in anchor_pairs
                    if _frame_agrees(f, o, c, scene_scale, t_tol=1e-3, r_tol=5e-3))
        if score > best_score:
            best, best_score = f, score
        if score == len(anchor_pairs):
            break
    if best is None:
        return identity_frame(), 0
    return best, best_score


def describe_frame(frame) -> str:
    if frame is None:
        return "unknown"
    rot = [row[:3] for row in frame[:3]]
    if rot == _CANDIDATE_ROTS[0]:
        label = "aligned (Z-up)"
    elif rot == _CANDIDATE_ROTS[1]:
        label = "rotated +90° about X (Y-up import)"
    elif rot == _CANDIDATE_ROTS[2]:
        label = "rotated −90° about X (Y-up import)"
    elif rot == _CANDIDATE_ROTS[3]:
        label = "rotated 180° about X"
    else:
        label = "non-identity frame"
    if any(abs(frame[i][3]) > 1e-6 for i in range(3)):
        label += ", offset from origin"
    return label


def _component_dims(component: Component) -> Optional[List[float]]:
    if component.bbox_min is None or component.bbox_max is None:
        return None
    return sorted(abs(component.bbox_max[i] - component.bbox_min[i]) for i in range(3))


def _object_dims(obj, scene_scale: float) -> Optional[List[float]]:
    try:
        dims = [float(d) * scene_scale for d in obj.dimensions]
    except (AttributeError, TypeError):
        return None
    if max(dims) <= 0.0:
        return None
    return sorted(dims)


def _dims_agree(a: List[float], b: List[float]) -> bool:
    for x, y in zip(a, b):
        # Tessellation shifts bounding boxes, hence relative not absolute.
        if abs(x - y) > _BBOX_REL_TOL * max(x, y, 1e-9):
            return False
    return True


def collect_candidates(objects=None):
    """Scene objects that may carry manifest geometry. INSTANCES-mode
    prototypes (STEP_name without STEP_uuid, identity-placed in the hidden
    components collection) are excluded: the occurrence empties are the
    things that sit at the manifest transforms."""
    if objects is None:
        if bpy is None:
            return []
        objects = list(bpy.context.scene.objects)
    out = []
    for obj in objects:
        try:
            if obj.type == "ARMATURE":
                continue
            if "RIG_group_empty" in obj.keys() or "RIG_helper" in obj.keys():
                continue
        except (AttributeError, TypeError):
            continue
        key = get_step_key(obj)
        if key.name is not None and key.uuid is None:
            continue
        out.append(obj)
    return out


def _basename(path) -> str:
    if not path:
        return ""
    return str(path).replace("\\", "/").rsplit("/", 1)[-1].casefold()


def match(manifest: Manifest, objects=None, collections=None) -> MatchReport:
    report = MatchReport()
    candidates = collect_candidates(objects)
    collections = collect_collections(collections)
    scene_scale = _scene_scale()

    # A long-lived test scene accumulates imports of OTHER step files. their
    # objects must never compete for this manifest's components. Objects from
    # the manifest's own file are identified by the STEP_file tag, but only
    # when at least one exists, so foreign importers (no tag at all) keep
    # their fuzzy-step chance in an otherwise empty scene.
    want_file = _basename(getattr(manifest, "step_file", None))
    if want_file:
        same_file = [o for o in candidates
                     if _basename(get_step_key(o).file) == want_file]
        if same_file and len(same_file) != len(candidates):
            print("[CADLink match] restricting to %d object(s) imported from %r "
                  "(%d other object(s) in the scene ignored)"
                  % (len(same_file), want_file, len(candidates) - len(same_file)))
            candidates = same_file

    group_of = {}
    for g in manifest.rigid_groups:
        for cid in g.components:
            group_of[cid] = g.id
    comps = {c.id: c for c in manifest.components
             if c.id in group_of and not c.suppressed}

    by_uuid = {}
    for obj in candidates:
        key = get_step_key(obj)
        if key.uuid is not None:
            by_uuid[(key.file, key.uuid)] = obj

    # A component may BE a subassembly occurrence: a rigid subassembly is one
    # body, so the manifest names the node, not its parts. That node carries no
    # shape of its own, so it becomes an object only in the import modes that
    # build empties. "Tree collection" and "Flat collection" build collections
    # instead, and a collection is not something a bone can carry: every such
    # component has nothing to match, and every leaf's rebuilt occurrence path
    # collapses to its own name because the ancestors are missing too. Counting
    # the objects whose parent is not in the pool identifies that at a glance.
    # (Live ClampRig, 2026-08-24: imported as a tree collection, 53 of
    # 122 components matched and only the loose parts moved with the rig. the
    # same file and manifest under "Parented empties" matched 122 of 122.)
    orphaned = 0
    for obj in candidates:
        key = get_step_key(obj)
        if key.uuid is None or key.parent is None or key.parent == -1:
            continue
        if (key.file, key.parent) not in by_uuid:
            orphaned += 1

    todo = dict(comps)
    pool = list(candidates)
    claimed = {}

    # The collection hierarchy, which in a "Tree collection" import is the
    # ONLY place the assembly structure survives.
    wanted_paths = {_norm_path(c.step_occurrence_path)
                    for c in comps.values() if c.step_occurrence_path}
    # Objects are already restricted to this manifest's own STEP file.
    # Collections must be too, or a scene holding two imports counts the
    # other one's occurrences and no bucket ever balances.
    if want_file:
        mine = []
        for col in collections:
            objs = _subtree_objects(col)
            if not objs:
                mine.append(col)         # a pure container: judged by its kids
                continue
            if any(_basename(get_step_key(o).file) == want_file for o in objs):
                mine.append(col)
        collections = mine
    col_parent, col_paths = _collection_paths(collections)
    col_depth = _container_depth(col_paths, wanted_paths)
    col_of_object = _object_collections(collections, col_paths)
    occurrence_of_collection = {}
    for name, segs in col_paths.items():
        if len(segs) > col_depth:
            occurrence_of_collection[name] = "/".join(segs[col_depth:])

    def claim(component_id, obj, step, members=None, collection=None):
        """One entry per COMPONENT, however many objects it owns.

        `claimed` stays a 1:1 map of the objects that carry a component's own
        pose (the scene-frame vote and the stale-tag sweep both index it),
        so a component resolved to a subassembly's parts is not in it: no
        member part sits at the assembly occurrence's transform, and letting
        one vote would tilt the frame every bone is placed through.

        Members carry RIG_group, which is all parenting needs (it attaches
        by group, and _rig_maps is already many-objects-per-group), and
        RIG_component_of rather than RIG_component_id: the retessellate path
        keys on RIG_component_id and would hand each part of a subassembly
        the whole subassembly's mesh."""
        owned = list(members) if members is not None else [obj]
        gid = group_of[component_id]
        # An occurrence every one of whose parts belongs to a component
        # NESTED inside it owns no geometry of its own. That is a real and
        # correct outcome, not a failure: the parts are already attached,
        # through the components that do own them.
        for member in owned:
            if member is obj and collection is None:
                member["RIG_component_id"] = component_id
            else:
                member["RIG_component_of"] = component_id
            member["RIG_group"] = gid
            # Which manifest claimed it. Group ids restart at g000 for every
            # assembly, so without this a second rig in the same scene
            # parents the first one's parts to its own bones.
            member["RIG_source"] = manifest.source_path or ""
            if member in pool:
                pool.remove(member)
        report.matched.append(MatchEntry(
            component_id=component_id,
            object_name=collection.name if collection is not None else obj.name,
            step=step,
            confidence=_METHOD_NAMES[step],
            object_names=[o.name for o in owned],
            collection_name=None if collection is None else collection.name,
        ))
        if collection is None:
            claimed[component_id] = obj
        todo.pop(component_id, None)

    # Ambiguity is NOT terminal: identical occurrence paths (two instances of
    # the same subassembly rebuild the same product-name path) are routine,
    # and the transform step downstream tells the twins apart. A component is
    # reported ambiguous only when it reaches the end still unresolved.
    ambiguous_seen = {}

    def note_ambiguous(component_id, objs):
        ambiguous_seen[component_id] = [o.name for o in objs]

    def path_steps():
        """Steps 1 and 2, over whatever is in the pool right now. Run once
        after the pre-seed and again after a stale pre-seed is reverted,
        so an object handed back to the pool gets its exact chance rather
        than only the transform steps below."""
        # Step 1: exact STEP_name plus rebuilt occurrence path. The uuid
        # chain is preferred. Where it dead-ends because the ancestors were
        # built as collections rather than objects, the collection ancestry
        # supplies the same path.
        paths = {}
        for obj in pool:
            p = _occurrence_path(obj, by_uuid)
            if p is None or "/" not in p:
                via_collection = _collection_occurrence_path(
                    obj, col_of_object, col_paths, col_depth)
                if via_collection is not None:
                    p = via_collection
            if p is not None:
                paths[id(obj)] = _norm_path(p)
        for cid in sorted(todo):
            comp = todo[cid]
            if comp.step_occurrence_path is None:
                continue
            want = _norm_path(comp.step_occurrence_path)
            hits = [o for o in pool
                    if get_step_key(o).name == comp.step_name
                    and paths.get(id(o)) == want]
            if len(hits) == 1:
                claim(cid, hits[0], 1)
            elif len(hits) > 1:
                note_ambiguous(cid, hits)

        # Step 2: path equality with instance suffixes and separators
        # normalized.
        for cid in sorted(todo):
            comp = todo[cid]
            if comp.step_occurrence_path is None:
                continue
            want = _loose_path(comp.step_occurrence_path)
            hits = [o for o in pool
                    if id(o) in paths and _loose_path(paths[id(o)]) == want]
            if len(hits) == 1:
                claim(cid, hits[0], 2)
            elif len(hits) > 1:
                note_ambiguous(cid, hits)

    path_steps()

    # Pre-seed from a previous run, AFTER the exact steps: a tag is history
    # and history goes stale, an occurrence path is identity. Component ids
    # are positional: a re-export that walks the assembly in another order
    # renumbers every one of them, and then EVERY tag in the scene names a
    # component its object never was (live TongRig, 2026-09-14: the
    # manifest re-sent against the standing import matched nothing and
    # parented nothing, because the wrong pre-seeds emptied the pool before
    # the path steps ran and then voted the frame). Run first, a stale
    # twin's tag would also take one twin out of the pool and hand the
    # other twin's path a false uniqueness. So the tags only fill what the
    # paths left (twins, flat imports, foreign files), and only when the
    # object still carries the component's STEP name. A tag naming an id
    # this manifest does not know, has already placed, or calls by another
    # name, is cleared, and the object goes through the cascade like any
    # other.
    for obj in list(pool):
        try:
            tagged = obj.get("RIG_component_id")
        except (AttributeError, TypeError):
            tagged = None
        if tagged is None:
            continue
        comp = todo.get(tagged)
        if comp is not None and get_step_key(obj).name == comp.step_name:
            claim(tagged, obj, 0)
            continue
        for tag in ("RIG_component_id", "RIG_group"):
            try:
                del obj[tag]
            except (KeyError, TypeError):
                pass
        if comp is not None:
            print("[CADLink match] stale tag: %s was tagged %s, which this "
                  "manifest calls %r. Tag cleared, re-matching"
                  % (obj.name, tagged, comp.step_name))
        elif tagged in claimed:
            print("[CADLink match] stale tag: %s was tagged %s, which the "
                  "occurrence path gives to %s. Tag cleared, re-matching"
                  % (obj.name, tagged, claimed[tagged].name))

    # The scene frame: estimated from the name/path anchors when there are
    # any. Without them (FLAT imports, foreign importers), uniquely-named
    # components still anchor a FULL frame: rotation and translation, so
    # an import dropped at the 3D cursor is found, not only an up-axis
    # swap. Only when every name is duplicated do the canonical up-axis
    # conversions compete on how many components each one names-and-places
    # (they carry no translation: transform agreement is the only signal
    # that can disambiguate identical names).
    unit_scale = 1.0 / scene_scale
    anchor_pairs = [(_matrix_rows(claimed[e.component_id]),
                     _component_rows_units(comps[e.component_id], unit_scale))
                    for e in report.matched]
    if anchor_pairs:
        frame, agree = estimate_frame(anchor_pairs, scene_scale)
    else:
        weak = []
        for cid in sorted(todo):
            comp = todo[cid]
            hits = [o for o in pool if get_step_key(o).name == comp.step_name]
            if len(hits) == 1:
                weak.append((_matrix_rows(hits[0]),
                             _component_rows_units(comp, unit_scale)))
        if weak:
            frame, agree = estimate_frame(weak, scene_scale)
        else:
            frame, agree = identity_frame(), 0
        if agree == 0:
            for rot in _CANDIDATE_ROTS:
                cand = _frame_from_rot(rot)
                score = 0
                for cid in todo:
                    comp = todo[cid]
                    crows = _component_rows_units(comp, unit_scale)
                    hits = [o for o in pool
                            if get_step_key(o).name == comp.step_name
                            and _frame_agrees(cand, _matrix_rows(o), crows, scene_scale)]
                    if len(hits) == 1:
                        score += 1
                if score > agree:
                    frame, agree = cand, score
    report.frame_rows = frame
    report.frame_agree = agree

    # Pre-seeded claims are trusted history, and history goes stale: a tag
    # written by an earlier manifest can sit on an object this manifest's
    # component no longer describes (rounds of re-import and re-export in one
    # scene). Now that a frame exists, any step-0 claim whose object is not
    # where the component says gets reverted and re-matched from scratch:
    # the frame vote is majority-based, so a minority of stale tags cannot
    # defend themselves by defining the frame.
    stale = [e for e in report.matched
             if e.step == 0 and not _frame_agrees(
                 frame, _matrix_rows(claimed[e.component_id]),
                 _component_rows_units(comps[e.component_id], unit_scale),
                 scene_scale, t_tol=1e-3, r_tol=5e-3)]
    for e in stale:
        obj = claimed.pop(e.component_id)
        report.matched.remove(e)
        todo[e.component_id] = comps[e.component_id]
        pool.append(obj)
        for tag in ("RIG_component_id", "RIG_group"):
            try:
                del obj[tag]
            except (KeyError, TypeError):
                pass
        print("[CADLink match] stale tag: %s no longer sits at %s's transform. "
              "Re-matching both" % (obj.name, e.component_id))
    if stale:
        path_steps()

    # Step 3: same STEP_name plus transform agreement under the scene frame.
    for cid in sorted(todo):
        comp = todo[cid]
        crows = _component_rows_units(comp, unit_scale)
        hits = [o for o in pool
                if get_step_key(o).name == comp.step_name
                and _frame_agrees(frame, _matrix_rows(o), crows, scene_scale)]
        if len(hits) == 1:
            claim(cid, hits[0], 3)
        elif len(hits) > 1:
            note_ambiguous(cid, hits)

    # Step 4: bounding-box dimensions tiebreaker.
    for cid in sorted(todo):
        comp = todo[cid]
        want = _component_dims(comp)
        if want is None:
            continue
        hits = []
        for o in pool:
            name = get_step_key(o).name
            if name is not None and name != comp.step_name:
                continue
            dims = _object_dims(o, scene_scale)
            if dims is not None and _dims_agree(dims, want):
                hits.append(o)
        if len(hits) == 1:
            claim(cid, hits[0], 4)
        elif len(hits) > 1:
            note_ambiguous(cid, hits)

    # Step 5: fuzzy label match, unique in both directions only.
    fuzzy_comps = {}
    for cid in sorted(todo):
        fuzzy_comps.setdefault(_fuzzy_label(todo[cid].step_name), []).append(cid)
    fuzzy_objs = {}
    for o in pool:
        label = get_step_key(o).name
        if label is None:
            label = o.name
        fuzzy_objs.setdefault(_fuzzy_label(label), []).append(o)
    for label, cids in sorted(fuzzy_comps.items()):
        objs = fuzzy_objs.get(label, [])
        if len(cids) == 1 and len(objs) == 1 and objs[0] in pool and cids[0] in todo:
            claim(cids[0], objs[0], 5)

    # Step 6: components that ARE a subassembly occurrence.
    #
    # The exporter's rule is that a rigid subassembly is ONE body, so the
    # manifest names the assembly occurrence and not the parts inside it.
    # That occurrence node carries no shape of its own, so an import that
    # builds collections rather than empties gives it no object to match:
    # it is a COLLECTION, and the body it stands for is every part below it.
    #
    # Deepest first: the manifest nests components inside one another (live
    # ClampRig, 2026-08-24: the ram rod sits inside the ram barrel and
    # belongs to a different rigid group), so the inner component must take
    # its parts out of the pool before the outer one sweeps up what is left.
    collection_bodies = {}
    for name, path in occurrence_of_collection.items():
        collection_bodies.setdefault(path, []).append(name)
    named_collections = {}
    for col in collections:
        named_collections.setdefault(_collection_label(col), []).append(col.name)

    # Only a component that IS a subassembly occurrence belongs here, and the
    # manifest says which those are: `subassembly_solving` is set for a
    # subassembly and absent for a part. Without that gate a part sharing an
    # occurrence path with a subassembly joins the same bucket and the counts
    # never line up (live corpus 07 flexible-sub2, 2026-08-24: the baseplate
    # sat in the hinge's bucket and neither hinge resolved).
    by_collection_path = {}
    for cid in todo:
        comp = todo[cid]
        if comp.subassembly_solving is None or not comp.step_occurrence_path:
            continue
        by_collection_path.setdefault(
            _norm_path(comp.step_occurrence_path), []).append(cid)

    # One bucket per set of occurrences that have to be told apart. Normally
    # the occurrence path names them. Where the STEP file carried no product
    # names it does not: every component of live corpus 07 flexible-sub2
    # reads "flexible-sub2/ ", and the occurrence's own name, which the
    # collection still carries, is the only join left.
    buckets = []
    for path in by_collection_path:
        cids = by_collection_path[path]
        found = collection_bodies.get(path)
        if found:
            buckets.append((path, path.count("/"), sorted(cids), sorted(found)))
            continue
        by_step_name = {}
        for cid in cids:
            by_step_name.setdefault(todo[cid].step_name or "", []).append(cid)
        for step_name, group in by_step_name.items():
            named = named_collections.get(_strip_dedup(step_name))
            if named:
                buckets.append((path + " [by name]", path.count("/"),
                                sorted(group), sorted(named)))

    col_by_name = {c.name: c for c in collections}
    spent = set()
    for path, _, bucket_cids, names in sorted(
            buckets, key=lambda b: (-b[1], b[0])):
        cids = sorted(c for c in bucket_cids if c in todo)
        names = [n for n in names if n not in spent]
        # Pairing and verification look at the WHOLE occurrence, claiming
        # looks at what is left: by the time an outer subassembly is
        # reached, the components nested inside it have taken their parts,
        # and different occurrences lose different parts to their own
        # nested components. Comparing those leftovers would say two
        # occurrences of one product are laid out differently when they
        # are not.
        remaining = set(pool)
        bodies = []
        for name in sorted(names):
            col = col_by_name[name]
            whole = _subtree_objects(col)
            if whole:
                bodies.append((col, whole))
        if not cids or not bodies:
            continue
        if len(bodies) != len(cids):
            report.notes.append(
                "%s: the manifest has %d occurrence(s) of this subassembly "
                "but the scene has %d collection(s) for it, so neither was "
                "claimed" % (path, len(cids), len(bodies)))
            continue

        # Which occurrence is which.
        #
        # The evidence that settles it is not position but LAYOUT: a STEP
        # file stores a product's internal arrangement once, so applying an
        # occurrence's true frame in reverse always gives that same
        # arrangement. Take the pairing whose layouts all agree, and position
        # only decides between pairings that are equally consistent, which
        # happens whenever the occurrences are related by a symmetry, and is
        # exactly where position is decisive.
        #
        # Position alone would swap a pair as soon as the offset between an
        # occurrence's origin and its parts grew past half the spacing
        # between two of them. Layout does not care where anything is.
        want_at = {}
        layout_of = {}
        for cid in cids:
            rows = apply_frame(frame, _component_rows_units(comps[cid],
                                                            unit_scale))
            want_at[cid] = [rows[i][3] * scene_scale for i in range(3)]
            for col, objs in bodies:
                layout_of[(col.name, cid)] = _internal_layout(
                    objs, rows, scene_scale)
        at = {col.name: _centroid(objs, scene_scale) for col, objs in bodies}

        def gap(col_name, cid):
            a, b = at[col_name], want_at[cid]
            if a is None:
                return float("inf")
            return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5

        def cost(assignment):
            return sum(gap(col.name, cid) for col, cid in assignment)

        consistent = []
        if len(bodies) > 1:
            first = bodies[0][0].name
            for anchor in cids:
                reference = layout_of[(first, anchor)]
                assignment, used, ok = [], set(), True
                for col, _objs in bodies:
                    hits = [c for c in cids if c not in used
                            and _layouts_agree(layout_of[(col.name, c)],
                                               reference)]
                    if len(hits) != 1:
                        ok = False
                        break
                    assignment.append((col, hits[0]))
                    used.add(hits[0])
                if ok:
                    consistent.append(assignment)

        if consistent:
            chosen = min(consistent, key=cost)
        else:
            # Either a single occurrence, or the STEP and the manifest
            # disagree about the arrangement itself, which is what a
            # flexible subassembly inserted more than once does. Position is
            # then all there is, and the report says so.
            order = sorted(bodies,
                           key=lambda item: min(gap(item[0].name, c)
                                                for c in cids))
            taken, chosen = set(), []
            for col, _objs in order:
                free = [c for c in cids if c not in taken]
                best = min(free, key=lambda c: gap(col.name, c))
                taken.add(best)
                chosen.append((col, best))
            if len(bodies) > 1:
                report.notes.append(
                    "%s: the parts do not sit the same way inside each "
                    "occurrence, so this pairing rests on position alone (a "
                    "flexible subassembly inserted more than once does that)"
                    % path)

        objs_of = {col.name: objs for col, objs in bodies}
        pairing = [(col, objs_of[col.name], cid) for col, cid in chosen]
        for col, objs, cid in pairing:
            spent.add(col.name)
            # Only a RIGID subassembly is one body. A flexible one is walked,
            # so every part inside it is a component in its own right and
            # usually in another rigid group. Expanding it would claim parts
            # that belong to those components, and put a moving part in the
            # grounded group when they had not matched yet (live corpus 07
            # flexible-sub1, 2026-08-24). It is still PAIRED, because that is
            # what tells its rigid twin's collection from its own.
            mine = ([] if todo[cid].subassembly_solving == "flexible"
                    else [o for o in objs if o in remaining])
            claim(cid, None, 6, members=mine, collection=col)

    # The "import as empties" advice is only about components that ARE
    # subassembly occurrences: those are the ones no collection import gives
    # an object of their own. `orphaned` says the scene was built that way,
    # but step 6 now resolves such components, so it stays large for a
    # perfectly healthy import: a leftover PART (renamed in the STEP, say)
    # must not draw this.
    stuck_subs = [cid for cid, comp in todo.items()
                  if comp.subassembly_solving is not None]
    if stuck_subs and orphaned:
        report.hint = (
            "%d object(s) name a parent that is not in the scene, so this "
            "STEP was imported as a collection hierarchy. %d subassembly "
            "occurrence(s) still did not resolve to one. Importing with Tree "
            "hierarchy = \"Parented empties\" gives every subassembly an "
            "object of its own and needs none of this guesswork."
            % (orphaned, len(stuck_subs)))
        print("[CADLink match] %s" % report.hint)
    for note in report.notes:
        print("[CADLink match] %s" % note)

    report.ambiguous = sorted(
        (cid, names) for cid, names in ambiguous_seen.items() if cid in todo)
    still_ambiguous = {cid for cid, _ in report.ambiguous}
    report.unmatched = sorted(cid for cid in todo if cid not in still_ambiguous)
    # A member of a subassembly body carries RIG_group but no
    # RIG_component_id, so it has neither a pre-seed nor a stale-tag path and
    # nothing above can ever take that group back. Group ids are positional:
    # a re-export that drops a component renumbers them, and a surviving tag
    # then names a DIFFERENT body: parenting reads the raw tag, so the part
    # would silently ride that body's bone. A member this run did not
    # re-claim loses its tags here. Out of the rig is visible and
    # recoverable. In the wrong rigid group is neither.
    # The same goes for a component tag that survived the pre-seed (its id
    # was already taken by another object) and was never re-claimed: left
    # in place, its group tag would parent the object to a body it is not.
    for obj in pool:
        try:
            was_member = obj.get("RIG_component_of") is not None
            was_tagged = obj.get("RIG_component_id") is not None
        except (AttributeError, TypeError):
            was_member = was_tagged = False
        if not was_member and not was_tagged:
            continue
        for tag in ("RIG_component_of", "RIG_component_id", "RIG_group"):
            try:
                del obj[tag]
            except (KeyError, TypeError):
                pass
        if was_member:
            print("[CADLink match] stale member tag: %s no longer belongs to a "
                  "matched subassembly body. Group tag cleared" % obj.name)
        else:
            print("[CADLink match] stale tag: %s was not re-claimed by this "
                  "manifest. Tags cleared" % obj.name)

    report.unclaimed_objects = [o.name for o in pool]

    # Say WHY, per failure: the console line is what turns the next
    # "unmatched: c004" report into a one-look diagnosis.
    for cid, names in report.ambiguous:
        print("[CADLink match] ambiguous: %s (%s), candidates %s"
              % (cid, comps[cid].step_name, ", ".join(names)))
    for cid in report.unmatched:
        comp = comps[cid]
        crows = _component_rows_units(comp, unit_scale)
        pred = apply_frame(frame, crows)
        name_hits = [o for o in pool if get_step_key(o).name == comp.step_name]
        if not name_hits:
            print("[CADLink match] unmatched: %s, no unclaimed object carries "
                  "STEP_name %r" % (cid, comp.step_name))
            continue
        nearest = []
        for o in name_hits:
            rows = _matrix_rows(o)
            d = sum((rows[i][3] - pred[i][3]) ** 2 for i in range(3)) ** 0.5
            nearest.append((d * scene_scale, o.name))
        nearest.sort()
        detail = ", ".join("%s at %.4f m off" % (n, d) for d, n in nearest[:3])
        print("[CADLink match] unmatched: %s (%s), same-name candidates rejected "
              "on transform: %s" % (cid, comp.step_name, detail))
    return report
