# SPDX-License-Identifier: GPL-3.0-or-later
"""Snap matched geometry to the manifest's SolidWorks poses.

The manifest transform is the authoritative per-instance world pose. The
STEP file is not. STEP stores ONE internal layout per subassembly product,
so a flexed flexible instance and a rigid twin of the same document cannot
both import correctly: the twin lands at the flexible pose. Rather than
rewriting the STEP (a de-instancing surgery the importer would then undo),
this module moves the already-imported objects: every matched object that
does not sit where its component says (under the estimated scene frame)
gets its world transform rewritten to the manifest pose, preserving the
import's own scale.

Runs after matching, before parenting. Objects already bone-parented to a
rig are skipped: unlink first.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .manifest import Manifest
from .matching import (MatchReport, _frame_agrees,
                       _matrix_rows, _rotation_columns, _scene_scale,
                       apply_frame, identity_frame)

try:
    import bpy
    from mathutils import Matrix
except ImportError:
    bpy = None
    Matrix = None


@dataclass
class PoseSyncReport:
    moved: List[Tuple[str, float]] = field(default_factory=list)   # name, metres
    already_ok: int = 0
    skipped: List[Tuple[str, str]] = field(default_factory=list)   # name, reason
    # Rigid subassemblies that resolved to a COLLECTION. These are not
    # declined corrections: no object carries the occurrence's own pose, so
    # there is nothing to compare and: where the STEP and the manifest agree,
    # which the matcher has already checked: nothing to do. Kept out of
    # `skipped` so a healthy TREE-mode sync does not report as a warning.
    collections: List[Tuple[str, str]] = field(default_factory=list)


def _det3(rows) -> float:
    return (rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
            - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
            + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0]))


def _invert_affine(rows):
    """Inverse of a 4x4 with [0,0,0,1] bottom row via 3x3 adjugate: the
    parent chain can carry non-uniform import scale, so no rigid shortcut."""
    d = _det3(rows)
    if abs(d) < 1e-15:
        return None
    cof = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            a = [[rows[r][c] for c in range(3) if c != j]
                 for r in range(3) if r != i]
            m = a[0][0] * a[1][1] - a[0][1] * a[1][0]
            cof[j][i] = ((-1) ** (i + j)) * m / d
    t = [rows[i][3] for i in range(3)]
    it = [-(cof[i][0] * t[0] + cof[i][1] * t[1] + cof[i][2] * t[2])
          for i in range(3)]
    return [
        [cof[0][0], cof[0][1], cof[0][2], it[0]],
        [cof[1][0], cof[1][1], cof[1][2], it[1]],
        [cof[2][0], cof[2][1], cof[2][2], it[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _retarget_rows(pred, cur) -> Optional[List[List[float]]]:
    """The manifest pose's rotation and translation, the object's own scale:
    CADder bakes STEP_applied_scale into the object matrix and overwriting
    it with the manifest's rigid transform would shrink the geometry."""
    pcols = _rotation_columns(pred)
    if pcols is None:
        return None
    out = [[0.0] * 4 for _ in range(4)]
    out[3][3] = 1.0
    for c in range(3):
        n = math.sqrt(sum(cur[r][c] * cur[r][c] for r in range(3)))
        if n < 1e-12:
            return None
        for r in range(3):
            out[r][c] = pcols[c][r] * n
    for r in range(3):
        out[r][3] = pred[r][3]
    return out


def _local_rows(obj):
    """The part's own place inside its component, as native_import recorded
    it: 16 numbers, row-major, in the CAD's own units. None for a part that
    IS its component, which is every part outside a rigid subassembly."""
    raw = obj.get("SWMESH_local") if obj is not None else None
    if raw is None:
        return None
    values = [float(v) for v in raw]
    if len(values) != 16:
        return None
    return [values[i * 4:(i + 1) * 4] for i in range(4)]


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def _target_rows(comp, obj, unit_scale):
    """Where this object belongs when its component is at the pose the
    manifest gives: the component's transform, then the object's own place
    inside it. A rigid subassembly is ONE component holding several parts,
    so without the second step every part of it would land on the
    subassembly's origin."""
    rows = [list(comp.transform[i]) for i in range(4)]
    local = _local_rows(obj)
    if local is not None:
        rows = _mul(rows, local)
    for i in range(3):
        rows[i][3] *= unit_scale
    return rows


def _depth(obj) -> int:
    d = 0
    p = getattr(obj, "parent", None)
    while p is not None and d < 256:
        d += 1
        p = getattr(p, "parent", None)
    return d


def _assign_basis(obj, rows):
    if Matrix is not None:
        obj.matrix_basis = Matrix(rows)
    else:
        obj.matrix_basis = rows


def _carries(obj, entry, strict):
    """True when the object's tags say it is the part this entry means.

    A tag that is missing does not say no, unless `strict`: the named
    object of a report from a route that does not write the tag is still
    the one the report means."""
    component = obj.get("RIG_component_id")
    if component is None:
        component = obj.get("RIG_component_of")
    path = obj.get("SWMESH_path")
    if strict:
        return component == entry.component_id and (
            not entry.object_path or path == entry.object_path)
    if component is not None and component != entry.component_id:
        return False
    if entry.object_path and path is not None and path != entry.object_path:
        return False
    return True


class _Finder:
    """Finds the object an entry of the match report means now.

    By the name first, which is what the report holds, but only when the
    object with that name still carries the entry's tags. The names are
    the ones the parts had when the send ran: a part renamed since then
    was not found and was not moved, and an object that took the old name
    was moved in its place. Otherwise by the tags, when one object alone
    carries them. parenting.relink works from the tags for the same
    reason."""

    def __init__(self, objects):
        self.objects = objects
        self.by_name = {o.name: o for o in objects}
        self._by_tags = None

    def find(self, entry):
        obj = self.by_name.get(entry.object_name)
        if obj is not None and _carries(obj, entry, strict=False):
            return obj
        if self._by_tags is None:
            self._by_tags = {}
            for o in self.objects:
                try:
                    component = o.get("RIG_component_id")
                    if component is None:
                        component = o.get("RIG_component_of")
                except (AttributeError, ReferenceError):
                    continue
                if component is not None:
                    self._by_tags.setdefault(component, []).append(o)
        found = [o for o in self._by_tags.get(entry.component_id, [])
                 if _carries(o, entry, strict=True)]
        return found[0] if len(found) == 1 else None


def sync(manifest: Manifest, report: MatchReport, objects=None,
         report_missing=None) -> PoseSyncReport:
    """Move every matched object whose world pose disagrees with its
    component's manifest transform (under report.frame_rows) onto that
    transform. Returns what moved, in metres, and what was skipped and why.
    The caller owns the depsgraph update afterwards.

    An entry whose part is not found goes to `skipped` when
    `report_missing` is true. By default it is true when `objects` is None
    (the whole scene), and false for a list of the caller's, which may
    hold only some of the parts on purpose."""
    out = PoseSyncReport()
    frame = report.frame_rows if report.frame_rows is not None else identity_frame()
    scene_scale = _scene_scale()
    unit_scale = 1.0 / scene_scale

    if report_missing is None:
        report_missing = objects is None
    if objects is None:
        objects = list(bpy.context.scene.objects) if bpy is not None else []
    finder = _Finder(objects)
    comps = {c.id: c for c in manifest.components}

    targets: Dict[str, List[List[float]]] = {}
    pending = []
    for entry in report.matched:
        if entry.collection_name is not None:
            # A rigid subassembly that resolved to a COLLECTION has no
            # object carrying the occurrence's own pose, so there is nothing
            # to measure the manifest pose against and no rigid delta to
            # apply. Moving its parts onto the component transform one by
            # one would collapse the subassembly onto a point.
            #
            # Nothing is lost where the STEP and the manifest agree: the
            # matcher only accepts such a pairing after checking that the
            # parts sit where the manifest's frame says, which is the same
            # comparison this stage makes. What IS lost is the case pose
            # sync exists for: a flexible subassembly inserted twice, whose
            # STEP layout can only be one of the two poses. That one needs
            # an import with Parented empties.
            out.collections.append((entry.collection_name,
                                    "a subassembly resolved to a collection: "
                                    "no object carries its occurrence pose"))
            continue
        comp = comps.get(entry.component_id)
        if comp is None:
            continue
        obj = finder.find(entry)
        if obj is None:
            if report_missing:
                out.skipped.append((entry.object_name,
                                    "not found: no object carries this part now"))
            continue
        crows = _target_rows(comp, obj, unit_scale)
        cur = _matrix_rows(obj)
        if _frame_agrees(frame, cur, crows, scene_scale):
            out.already_ok += 1
            continue
        if getattr(obj, "parent", None) is not None \
                and getattr(obj, "parent_type", "OBJECT") != "OBJECT":
            out.skipped.append((obj.name, "parented to the rig: unlink first"))
            continue
        if _det3(cur) < 0.0:
            out.skipped.append((obj.name, "mirrored instance: cannot repose "
                                "without flipping the geometry"))
            continue
        target = _retarget_rows(apply_frame(frame, crows), cur)
        if target is None:
            out.skipped.append((obj.name, "degenerate transform"))
            continue
        targets[obj.name] = target
        dist = math.sqrt(sum((cur[i][3] - target[i][3]) ** 2
                             for i in range(3))) * scene_scale
        pending.append((obj, target, dist))

    # Parents before children, and every parent's FINAL pose taken from the
    # target map: no depsgraph round-trips between assignments.
    pending.sort(key=lambda item: _depth(item[0]))
    for obj, target, dist in pending:
        parent = getattr(obj, "parent", None)
        if parent is None:
            basis = target
        else:
            pfinal = targets.get(parent.name, _matrix_rows(parent))
            mpi = [[float(v) for v in row] for row in obj.matrix_parent_inverse]
            inv = _invert_affine(apply_frame(pfinal, mpi))
            if inv is None:
                out.skipped.append((obj.name, "degenerate parent transform"))
                continue
            basis = apply_frame(inv, target)
        _assign_basis(obj, basis)
        out.moved.append((obj.name, dist))

    for name, dist in out.moved:
        print("[CADLink pose] moved %s onto its SolidWorks pose (%.4f m off)"
              % (name, dist))
    for name, reason in out.skipped:
        print("[CADLink pose] skipped %s: %s" % (name, reason))
    for name, reason in out.collections:
        print("[CADLink pose] %s: %s" % (name, reason))
    return out
