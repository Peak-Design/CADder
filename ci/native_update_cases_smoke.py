# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the cases an update of a direct send got wrong.

    blender -b --factory-startup --python-exit-code 1 -P ci/native_update_cases_smoke.py

ci/native_update_smoke.py covers one assembly in the TREE mode. The cases
here are the ones it did not reach: the modes that make an empty for each
subassembly, copies the user made in Blender, and parts that come back
with new definition and material numbers. In each case the export changes
little or nothing, and the update must change no more than that.
"""

import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import manifest as man_mod, native_import, swmesh  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "native_update_cases_smoke")


def _t(x, y=0.0, z=0.0):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(name, definitions, instances, nodes=(), materials=None):
    """A version 3 .swmesh.

    definitions: (id, name, size, material index per triangle or None).
    instances: (definition id, component id, name, path, x, local x or None).
    nodes: (path, name, component id, x).
    materials: (name, rgba) in the order of the export's table.
    """
    materials = materials or [("grey", (0.8, 0.8, 0.8, 1.0))]
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", len(materials), len(definitions),
                        len(instances), len(nodes))
    for mname, rgba in materials:
        body += _text(mname) + struct.pack("<6f", *rgba, 0.5, 0.0)
        body += _text("") + struct.pack("<I", 0)
    for did, dname, size, mats in definitions:
        mats = list(mats) if mats else [0]
        count = len(mats)
        verts = [0.0, 0.0, 0.0]
        for i in range(count + 1):
            verts.extend([size, size * i, 0.0])
        tris = []
        for i in range(count):
            tris.extend([0, i + 1, i + 2])
        body += struct.pack("<i", did) + _text(dname)
        body += struct.pack("<II", count + 2, count)
        body += struct.pack("<%df" % len(verts), *verts)
        body += struct.pack("<%di" % len(tris), *tris)
        body += struct.pack("<%di" % count, *mats)
    for did, cid, iname, where, x, local in instances:
        body += (struct.pack("<i", did) + _text(cid) + _text(iname)
                 + _text(where) + struct.pack("<16d", *_flat(_t(x))))
        body += struct.pack("<B", 0 if local is None else 1)
        if local is not None:
            body += struct.pack("<16d", *_flat(_t(local)))
    for where, nname, cid, x in nodes:
        body += (_text(where) + _text(nname) + _text(cid)
                 + struct.pack("<16d", *_flat(_t(x))))
    os.makedirs(TMP, exist_ok=True)
    path = os.path.join(TMP, name + ".swmesh")
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def manifest(components, groups, step_file="asm.step"):
    """components: (id, path, persistent id, x, rigid subassembly).
    groups: (id, [component ids])."""
    return man_mod.parse({
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": step_file, "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            dict({"id": cid, "sw_path": where,
                  "step_name": where.rpartition("/")[2],
                  "step_occurrence_path": None, "sw_persistent_id": pid,
                  "transform": _t(x)},
                 **({"subassembly_solving": "rigid"} if rigid else {}))
            for cid, where, pid, x, rigid in components],
        "rigid_groups": [
            {"id": gid, "name": gid, "components": cids,
             "grounded": i == 0, "frame": None, "bbox_diag": 0.2}
            for i, (gid, cids) in enumerate(groups)],
        "joints": [], "loops": [], "warnings": [],
    })


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def by_path(path, scene=None):
    objects = scene.objects if scene is not None else bpy.data.objects
    for obj in objects:
        if obj.get("SWMESH_path") == path \
                and not obj.get("SWMESH_prototype"):
            return obj
    return None


def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)


# ── the cases ───────────────────────────────────────────────────────────

# A loose base, and a rigid subassembly lifter-1 that holds a rod and a
# block. The subassembly is ONE component (c002) of three parts.
_LIFTER_DEFS = [(1, "base", 0.1, None), (2, "rod", 0.05, None),
                (3, "block", 0.08, None)]
_LIFTER_INST = [(1, "c001", "base", "base-1", 0.0, None),
                (2, "c002", "rod", "lifter-1/rod-1", 0.6, 0.1),
                (3, "c002", "block", "lifter-1/block-1", 0.5, 0.0)]
_LIFTER_NODES = [("lifter-1", "lifter", "c002", 0.5)]


def _lifter_manifest():
    return manifest([("c001", "base-1", "pbase", 0.0, False),
                     ("c002", "lifter-1", "plifter", 0.5, True)],
                    [("g000", ["c001"]), ("g001", ["c002"])])


def case_branch_empties_survive(hierarchy):
    """An unchanged export must leave the subassembly's empty alone.

    The empty carried the import's tags, so the update read it as a part
    that the new export did not have, and deleted it."""
    fresh()
    m = _lifter_manifest()
    mesh = write_mesh("lifter", _LIFTER_DEFS, _LIFTER_INST, _LIFTER_NODES)
    native_import.build(bpy.context, mesh, manifest=m, hierarchy=hierarchy)
    lifter = bpy.data.objects.get("lifter")
    check(lifter is not None and lifter.type == "EMPTY",
          "the send made no empty for the subassembly")
    check(lifter.get("RIG_component_id") is None,
          "the subassembly empty carries a component id, as if it were a part")
    lifter.location.x += 1.0            # the user moved the whole branch
    bpy.context.view_layer.update()
    mine = bpy.data.objects.new("mine", None)
    bpy.data.collections["lifter"].objects.link(mine)
    mine.parent = lifter

    _objects, _report, out = native_import.update(
        bpy.context, mesh, manifest=m, hierarchy=hierarchy)
    check(not out.removed, "removed: %s" % out.removed)
    check(not out.added, "added: %s" % out.added)
    check(not out.structural, "an unchanged export read as structural")
    lifter = bpy.data.objects.get("lifter")
    check(lifter is not None, "the update deleted the subassembly empty")
    check(bpy.data.objects["mine"].parent is lifter,
          "the user's object lost its parent")
    for path in ("lifter-1/rod-1", "lifter-1/block-1"):
        part = by_path(path)
        check(part is not None and part.parent is lifter,
              "%s is not under the subassembly empty" % path)


def case_branch_empties_of_an_older_send(hierarchy):
    """The empties an older build made carry the component id of a rigid
    subassembly and no role. They are still branches, not parts."""
    fresh()
    m = _lifter_manifest()
    mesh = write_mesh("lifter", _LIFTER_DEFS, _LIFTER_INST, _LIFTER_NODES)
    native_import.build(bpy.context, mesh, manifest=m, hierarchy=hierarchy)
    lifter = bpy.data.objects["lifter"]
    lifter["RIG_component_id"] = "c002"
    if "SWMESH_role" in lifter.keys():
        del lifter["SWMESH_role"]
    _objects, _report, out = native_import.update(
        bpy.context, mesh, manifest=m, hierarchy=hierarchy)
    check(not out.removed, "removed: %s" % out.removed)
    check(bpy.data.objects.get("lifter") is not None,
          "the update deleted the subassembly empty")


CASES = [
    ("EMPTIES: the subassembly empty survives an update",
     lambda: case_branch_empties_survive("EMPTIES")),
    ("COLLECTION_INSTANCES: the subassembly empty survives an update",
     lambda: case_branch_empties_survive("COLLECTION_INSTANCES")),
    ("EMPTIES: an empty of an older send is a branch",
     lambda: case_branch_empties_of_an_older_send("EMPTIES")),
]


def main():
    only = os.environ.get("CASES_ONLY")
    failed = []
    ran = 0
    for name, fn in CASES:
        if only and only not in name:
            continue
        ran += 1
        try:
            fn()
            print("native_update_cases_smoke: pass: %s" % name)
        except Fail as exc:
            failed.append(name)
            print("native_update_cases_smoke: FAIL: %s: %s" % (name, exc))
    if failed:
        raise SystemExit("native_update_cases_smoke: FAIL: %d of %d case(s)"
                         % (len(failed), ran))
    print("native_update_cases_smoke: OK: %d case(s)" % ran)


main()
