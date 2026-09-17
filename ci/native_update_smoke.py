# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for an UPDATE of a scene that is already there.

    blender -b --factory-startup -P ci/native_update_smoke.py

A send replaces the import, which is right the first time and wrong every
time after it: everything done in Blender since goes with it. An update
compares the two assemblies part by part and changes only what changed, so
a part that did not move and did not change shape keeps its object, its
mesh and the work done on it (Oscar, 2026-09-16).

The assembly here changes in every way one can between two exports: a part
moves, a part is re-tessellated, a part is deleted, a part is added, and
one part is renamed in the tree, which must read as the same part rather
than as one deleted and one added.
"""

import json
import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import manifest as man_mod, native_import, swmesh  # noqa: E402


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


def manifest(parts):
    """parts: (component id, path, persistent id, x). One group per part,
    numbered in the order they come, as an export does."""
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "upd.step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": cid, "sw_path": path, "step_name": path.rpartition("/")[2],
             "step_occurrence_path": None, "sw_persistent_id": pid,
             "transform": _t(x)}
            for cid, path, pid, x in parts],
        "rigid_groups": [
            {"id": "g%03d" % i, "name": path.rpartition("/")[2],
             "components": [cid], "grounded": i == 0, "frame": None,
             "bbox_diag": 0.2}
            for i, (cid, path, pid, x) in enumerate(parts)],
        "joints": [], "loops": [], "warnings": [],
    }


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, definitions, instances):
    """definitions: (id, name, triangles). instances: (definition id,
    component id, name, path, x)."""
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, len(definitions), len(instances), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    for did, name, triangles in definitions:
        n = triangles + 2
        verts = []
        for i in range(n):
            verts.extend([float(i) * 0.01, float(i * i % 3) * 0.01, 0.0])
        tris = []
        for i in range(triangles):
            tris.extend([0, i + 1, i + 2])
        body += struct.pack("<i", did) + _text(name)
        body += struct.pack("<II", n, triangles)
        body += struct.pack("<%df" % len(verts), *verts)
        body += struct.pack("<%di" % len(tris), *tris)
        body += struct.pack("<%di" % triangles, *([0] * triangles))
    for did, cid, name, where, x in instances:
        body += (struct.pack("<i", did) + _text(cid) + _text(name)
                 + _text(where) + struct.pack("<16d", *_flat(_t(x)))
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def _check(cond, msg):
    if not cond:
        raise SystemExit("native_update_smoke: FAIL: " + msg)


def by_path(path):
    for obj in bpy.data.objects:
        if obj.get("SWMESH_path") == path:
            return obj
    return None


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    tmp = tempfile.gettempdir()

    # ── The first send: four parts, one of them inside a subassembly ────
    before = [("c001", "base-1", "pbase", 0.0),
              ("c002", "arm-1", "parm", 0.2),
              ("c003", "clip-1", "pclip", 0.4),
              ("c004", "lift-1", "plift", 0.6),
              ("c005", "cam-1", "pcam", 1.0)]
    m1 = man_mod.parse(manifest(before))
    mesh1 = write_mesh(
        os.path.join(tmp, "upd.swmesh"),
        [(1, "base", 1), (2, "arm", 1), (3, "clip", 1), (4, "lift", 1),
         (5, "cam", 1)],
        [(1, "c001", "base", "base-1", 0.0),
         (2, "c002", "arm", "arm-1", 0.2),
         (3, "c003", "clip", "clip-1", 0.4),
         (4, "c004", "lift", "lift-1", 0.6),
         (5, "c005", "cam", "cam-1", 1.0)])
    objects, report = native_import.build(
        bpy.context, mesh1, manifest=m1, hierarchy="TREE")
    _check(len(objects) == 5, "the first send built %d part(s)" % len(objects))

    # ── The work a user does afterwards, which an update must not lose ──
    arm = by_path("arm-1")
    arm.modifiers.new("Bevel", "BEVEL")
    arm.name = "arm I renamed"
    arm_mesh = arm.data.name
    base = by_path("base-1")
    base_mesh = base.data.name
    mine = bpy.data.objects.new("mine", None)
    bpy.data.collections["upd"].objects.link(mine)

    # ── The second export ───────────────────────────────────────────────
    #
    # base: unchanged. arm: moved, same shape. cam: re-tessellated, same
    # place. clip: gone. lift: renamed in the tree (same persistent id).
    # guard: new. The component ids all shift, as they do whenever a part
    # is added or removed.
    after = [("c001", "base-1", "pbase", 0.0),
             ("c002", "arm-1", "parm", 0.25),
             ("c003", "guard-1", "pguard", 0.8),
             ("c004", "lift-4", "plift", 0.6),
             ("c005", "cam-1", "pcam", 1.0)]
    m2 = man_mod.parse(manifest(after))
    mesh2 = write_mesh(
        os.path.join(tmp, "upd2.swmesh"),
        [(1, "base", 1), (2, "arm", 1), (3, "guard", 1), (4, "lift", 1),
         (5, "cam", 4)],
        [(1, "c001", "base", "base-1", 0.0),
         (2, "c002", "arm", "arm-1", 0.25),
         (3, "c003", "guard", "guard-1", 0.8),
         (4, "c004", "lift", "lift-4", 0.6),
         (5, "c005", "cam", "cam-1", 1.0)])
    # A NEW file name, because the assembly was renamed: a revision was
    # cut and a letter went on the end. The update has to find the import
    # that is standing all the same, or it rebuilds the scene and throws
    # away the work above (Oscar, 2026-09-17).

    objects, report, out = native_import.update(
        bpy.context, mesh2, manifest=m2, hierarchy="TREE")

    # ── What changed, and only what changed ─────────────────────────────
    _check(sorted(out.added) == ["guard"], "added: %s" % out.added)
    _check(len(out.removed) == 1 and out.removed[0].startswith("clip"),
           "removed: %s" % out.removed)
    _check(by_path("clip-1") is None, "the deleted part is still in the scene")
    guard = by_path("guard-1")
    _check(guard is not None, "the new part was not built")
    _check(guard.get("RIG_group") == "g002",
           "the new part is in group %s" % guard.get("RIG_group"))

    base = by_path("base-1")
    _check(base is not None and base.data.name == base_mesh,
           "a part that did not change was given a new mesh")
    _check(out.kept >= 2, "only %d part(s) were left alone" % out.kept)

    arm = by_path("arm-1")
    _check(arm is not None, "the moved part went missing")
    _check(arm.name == "arm I renamed", "the object was renamed by the update")
    _check([m.type for m in arm.modifiers] == ["BEVEL"],
           "the modifier was lost: %s" % [m.type for m in arm.modifiers])
    _check(arm.data.name == arm_mesh,
           "the moved part's mesh was replaced although only its pose moved")
    _check(round(arm.matrix_world.translation.x, 4) == 0.25,
           "the part did not move: %.4f" % arm.matrix_world.translation.x)
    _check(arm.name in out.moved, "the move was not reported: %s" % out.moved)
    _check(arm.name not in out.reshaped,
           "a part that only moved was reported as re-tessellated")

    # The re-tessellated part took the new triangles on the SAME object,
    # and did not move.
    cam = by_path("cam-1")
    _check(cam is not None, "the re-tessellated part went missing")
    _check(len(cam.data.polygons) == 4,
           "the new geometry did not arrive: %d triangle(s)"
           % len(cam.data.polygons))
    _check(cam.name in out.reshaped, "the reshape was not reported")
    _check(cam.name not in out.moved, "a part that stood still was moved")

    # The renamed occurrence is the same part, not a delete and an add.
    lift = by_path("lift-4")
    _check(lift is not None, "the renamed occurrence was not found")
    _check(lift.name.startswith("lift"), "the renamed part is %s" % lift.name)
    _check("lift" not in " ".join(out.added) and
           not any(n.startswith("lift") for n in out.removed),
           "the renamed part read as an add and a remove")

    # Every part carries the NEW export's ids, which all shifted.
    _check(by_path("base-1").get("RIG_component_id") == "c001", "base id")
    _check(lift.get("RIG_component_id") == "c004", "lift id")
    _check(lift.get("RIG_group") == "g003", "lift group")

    # And the object that is none of the CAD application's business.
    _check(bpy.data.objects.get("mine") is not None,
           "the update removed an object of the user's own")
    _check(out.structural, "the update did not report a structural change")

    # The import took the new name of the document, so the next update
    # finds it by name again.
    root = bpy.data.collections.get("upd2")
    _check(root is not None and root.get("SWMESH_file") == "upd2",
           "the import kept the old name of the document")
    _check(bpy.data.collections.get("upd") is None,
           "the old name is still on a collection")
    _check(by_path("base-1").get("SWMESH_file") == "upd2",
           "a part kept the old name of the document")

    print("native_update_smoke: OK: %s, a renamed occurrence stayed one "
          "part, the untouched parts kept their meshes and modifiers, and "
          "an object of the user's own was left alone, and the renamed "
          "assembly was recognized" % out.describe())


main()
