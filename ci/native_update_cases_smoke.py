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
from mathutils import Matrix

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


_BOLT_DEFS = [(1, "base", 0.1, None), (2, "bolt", 0.02, None)]
_BOLT_INST = [(1, "c001", "base", "base-1", 0.0, None),
              (2, "c002", "bolt", "bolt-1", 0.3, None)]


def _bolt_manifest():
    return manifest([("c001", "base-1", "pbase", 0.0, False),
                     ("c002", "bolt-1", "pbolt", 0.3, False)],
                    [("g000", ["c001"]), ("g001", ["c002"])])


def case_a_copy_made_in_blender(linked):
    """Shift+D and Alt+D copy the tags with the object. The copy and the
    part then had one identity, the update could pair neither, and it
    deleted both and built the part again: the user's modifier went, and
    the copy too."""
    fresh()
    m = _bolt_manifest()
    mesh = write_mesh("bolts", _BOLT_DEFS, _BOLT_INST)
    native_import.build(bpy.context, mesh, manifest=m, hierarchy="TREE")
    bolt = by_path("bolt-1")
    bolt.modifiers.new("Bevel", "BEVEL")
    copy = bolt.copy()
    if not linked:
        copy.data = bolt.data.copy()
    for col in bolt.users_collection:
        col.objects.link(copy)
    copy.location.x += 0.2
    bpy.context.view_layer.update()
    names = (bolt.name, copy.name)

    for _round in range(2):
        _objects, _report, out = native_import.update(
            bpy.context, mesh, manifest=m, hierarchy="TREE")
        check(not out.removed, "removed: %s" % out.removed)
        check(not out.added, "added: %s" % out.added)
        for name in names:
            check(bpy.data.objects.get(name) is not None,
                  "the update deleted %s" % name)
    bolt, copy = (bpy.data.objects[n] for n in names)
    check([md.type for md in bolt.modifiers] == ["BEVEL"],
          "the part lost its modifier")
    check(abs(bolt.matrix_world.translation.x - 0.3) < 1e-6,
          "the part is not on its CAD pose")
    check(abs(copy.matrix_world.translation.x - 0.5) < 1e-6,
          "the copy moved to x=%.4f" % copy.matrix_world.translation.x)
    check(bolt.get("SWMESH_path") == "bolt-1", "the part lost its tags")
    check(copy.get("SWMESH_file") is None and copy.get("SWMESH_path") is None
          and copy.get("RIG_component_id") is None,
          "the copy still says it is the part")


def case_a_copy_of_the_scene():
    """A Full Copy of the scene copies every part with its tags. The update
    of one scene must not touch the other."""
    fresh()
    m = _bolt_manifest()
    mesh = write_mesh("bolts", _BOLT_DEFS, _BOLT_INST)
    native_import.build(bpy.context, mesh, manifest=m, hierarchy="TREE")
    here = bpy.context.scene
    bpy.ops.scene.new(type="FULL_COPY")
    there = [s for s in bpy.data.scenes if s is not here][0]
    bpy.context.window.scene = here
    theirs = sorted(o.name for o in there.objects)
    check(len(theirs) == 2, "the scene copy holds %s" % theirs)

    _objects, _report, out = native_import.update(
        bpy.context, mesh, manifest=m, hierarchy="TREE")
    check(not out.removed and not out.added,
          "added %s, removed %s" % (out.added, out.removed))
    check(sorted(o.name for o in there.objects) == theirs,
          "the other scene changed: %s" % sorted(o.name for o in there.objects))
    check(by_path("bolt-1", here) is not None, "this scene lost its bolt")


def _send(mesh, man_json, update=False):
    """A send or a Refresh through the bridge job, with a rig."""
    import json
    from CADder import bridge
    man = os.path.join(TMP, "bolts.rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(man_json, fh)
    payload = {
        "step": None, "mesh": mesh, "manifest": man,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": "FLAT", "up_as": "ZPOS"},
    }
    if update:
        payload["rig_mode"] = "KEEP"
    result = bridge._run_job(payload)
    check(result.get("ok"), "the job failed: %s" % result.get("error"))
    return result


def case_a_copy_on_the_rig():
    """A copy of a part on the rig is on the part's bone. Refresh Model
    must leave it there, where the user put it."""
    from CADder import rig
    fresh()
    try:
        rig.register()
    except ValueError:
        pass                                 # registered by an earlier case
    joint = {"id": "j001", "type": "revolute", "parent_group": "g000",
             "child_group": "g001", "origin": [0.3, 0, 0], "axis": [0, 0, 1],
             "secondary_axis": [1, 0, 0], "limits": None}
    raw = {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "bolts.step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": "c001", "sw_path": "base-1", "step_name": "base",
             "step_occurrence_path": None, "sw_persistent_id": "pbase",
             "transform": _t(0.0)},
            {"id": "c002", "sw_path": "bolt-1", "step_name": "bolt",
             "step_occurrence_path": None, "sw_persistent_id": "pbolt",
             "transform": _t(0.3)}],
        "rigid_groups": [
            {"id": "g000", "name": "base", "components": ["c001"],
             "grounded": True, "frame": None, "bbox_diag": 0.2},
            {"id": "g001", "name": "bolt", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": 0.2}],
        "joints": [joint], "loops": [], "warnings": [],
    }
    mesh = write_mesh("bolts", _BOLT_DEFS, _BOLT_INST)
    _send(mesh, raw)
    bolt = by_path("bolt-1")
    check(bolt.parent is not None and bolt.parent_type == "BONE",
          "the bolt is not on the rig")
    copy = bolt.copy()
    for col in bolt.users_collection:
        col.objects.link(copy)
    copy.matrix_world = Matrix.Translation((0.0, 0.2, 0.0)) @ bolt.matrix_world
    bpy.context.view_layer.update()
    placed = copy.matrix_world.copy()
    name, bone = copy.name, bolt.parent_bone

    _send(mesh, raw, update=True)
    copy = bpy.data.objects.get(name)
    check(copy is not None, "the refresh deleted the copy")
    check(copy.parent is not None and copy.parent_type == "BONE"
          and copy.parent_bone == bone,
          "the copy is off its bone: %s %s" % (copy.parent, copy.parent_bone))
    check((copy.matrix_world.translation - placed.translation).length < 1e-6,
          "the copy moved")
    check(by_path("bolt-1") is not None, "the refresh lost the bolt")


CASES = [
    ("FLAT: a copy of a part on the rig stays on its bone",
     case_a_copy_on_the_rig),
    ("TREE: a copy made with Shift+D is left alone",
     lambda: case_a_copy_made_in_blender(False)),
    ("TREE: a copy made with Alt+D is left alone",
     lambda: case_a_copy_made_in_blender(True)),
    ("TREE: a Full Copy of the scene is left alone",
     case_a_copy_of_the_scene),
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
