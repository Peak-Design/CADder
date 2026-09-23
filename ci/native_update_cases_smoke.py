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
import traceback

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


def _proto_mesh(obj):
    """The mesh a collection instance draws."""
    col = obj.instance_collection
    check(col is not None, "%s instances nothing" % obj.name)
    meshes = [o.data for o in col.objects if o.type == "MESH"]
    check(len(meshes) == 1, "%s draws %d mesh(es)" % (obj.name, len(meshes)))
    return meshes[0]


def _width(me):
    return max(v.co.x for v in me.vertices) - min(v.co.x for v in me.vertices)


def _orphans():
    return sorted(me.name for me in bpy.data.meshes if me.users == 0)


def case_instances_find_their_prototype(older=False):
    """The export numbers its definitions in walk order, so a part added
    in front of another shifts the numbers. The update found prototypes by
    the old number: the new guard drew the arm, a second arm drew nothing,
    and the meshes built for them were left with no users."""
    fresh()
    first = write_mesh(
        "inst", [(0, "base", 0.1, None), (1, "arm", 0.04, None)],
        [(0, "c001", "base", "base-1", 0.0, None),
         (1, "c002", "arm", "arm-1", 0.3, None)])
    native_import.build(bpy.context, first, hierarchy="COLLECTION_INSTANCES")
    arm_col = by_path("arm-1").instance_collection
    if older:
        # The prototypes of an older send carry no geometry tag.
        for obj in bpy.data.objects:
            if obj.get("SWMESH_prototype") and "SWMESH_geometry" in obj.keys():
                del obj["SWMESH_geometry"]

    second = write_mesh(
        "inst", [(0, "base", 0.1, None), (1, "guard", 0.07, None),
                 (2, "arm", 0.04, None)],
        [(0, "c001", "base", "base-1", 0.0, None),
         (1, "c002", "guard", "guard-1", 0.2, None),
         (2, "c003", "arm", "arm-1", 0.3, None),
         (2, "c004", "arm", "arm-2", 0.5, None)])
    _objects, _report, out = native_import.update(
        bpy.context, second, hierarchy="COLLECTION_INSTANCES")
    check(sorted(out.added) == ["arm", "guard"] or
          sorted(n.split(".")[0] for n in out.added) == ["arm", "guard"],
          "added: %s" % out.added)
    check(not out.reshaped, "re-tessellated: %s" % out.reshaped)
    guard = by_path("guard-1")
    check(abs(_width(_proto_mesh(guard)) - 0.07) < 1e-6,
          "the guard draws a part %.3f wide" % _width(_proto_mesh(guard)))
    for path in ("arm-1", "arm-2"):
        arm = by_path(path)
        check(arm.instance_collection is arm_col,
              "%s does not draw the arm prototype" % path)
    check(not _orphans(), "meshes with no users: %s" % _orphans())

    # And once more, unchanged: nothing moves between prototypes.
    _objects, _report, out = native_import.update(
        bpy.context, second, hierarchy="COLLECTION_INSTANCES")
    check(not out.added and not out.removed and not out.reshaped,
          "an unchanged export changed: %s" % out.describe())


def case_one_instance_takes_new_geometry():
    """Two placements of one part, and the CAD application sends new
    geometry for one of them only. The update put it into the prototype
    the two share, so the other one changed too."""
    fresh()
    first = write_mesh(
        "inst", [(0, "bolt", 0.02, None)],
        [(0, "c001", "bolt", "bolt-1", 0.0, None),
         (0, "c002", "bolt", "bolt-2", 0.3, None)])
    native_import.build(bpy.context, first, hierarchy="COLLECTION_INSTANCES")
    second = write_mesh(
        "inst", [(0, "bolt", 0.02, None), (1, "bolt", 0.03, None)],
        [(0, "c001", "bolt", "bolt-1", 0.0, None),
         (1, "c002", "bolt", "bolt-2", 0.3, None)])
    _objects, _report, out = native_import.update(
        bpy.context, second, hierarchy="COLLECTION_INSTANCES")
    one, two = by_path("bolt-1"), by_path("bolt-2")
    check(abs(_width(_proto_mesh(one)) - 0.02) < 1e-6,
          "bolt-1 took the other placement's geometry")
    check(abs(_width(_proto_mesh(two)) - 0.03) < 1e-6,
          "bolt-2 did not take its new geometry")
    check(out.reshaped == [two.name], "re-tessellated: %s" % out.reshaped)
    check(not _orphans(), "meshes with no users: %s" % _orphans())


_GREY = ("grey", (0.8, 0.8, 0.8, 1.0))
_RED = ("red", (0.8, 0.1, 0.1, 1.0))
_GREEN = ("green", (0.1, 0.8, 0.1, 1.0))
_BLUE = ("blue", (0.1, 0.1, 0.8, 1.0))


def _slot_names(obj):
    return [m.get("SWMESH_appearance_name") or m.name for m in obj.data.materials]


def case_material_numbers_shift():
    """The export numbers its materials in the order the walk finds them.
    A new part with a new color, walked first, shifts the numbers of every
    other color. The update read that as new geometry on the parts that
    did not change, and replaced their meshes."""
    fresh()
    first = write_mesh(
        "paint", [(1, "a", 0.05, [1]), (2, "b", 0.06, [2])],
        [(1, "c001", "a", "a-1", 0.0, None),
         (2, "c002", "b", "b-1", 0.2, None)],
        materials=[_GREY, _RED, _BLUE])
    native_import.build(bpy.context, first, hierarchy="FLAT")
    meshes = {p: by_path(p).data.name for p in ("a-1", "b-1")}

    second = write_mesh(
        "paint", [(0, "c", 0.07, [1]), (1, "a", 0.05, [2]),
                  (2, "b", 0.06, [3])],
        [(0, "c003", "c", "c-1", 0.4, None),
         (1, "c001", "a", "a-1", 0.0, None),
         (2, "c002", "b", "b-1", 0.2, None)],
        materials=[_GREY, _GREEN, _RED, _BLUE])
    _objects, _report, out = native_import.update(
        bpy.context, second, hierarchy="FLAT")
    check(not out.reshaped, "re-tessellated: %s" % out.reshaped)
    for path, name in meshes.items():
        check(by_path(path).data.name == name,
              "%s was given a new mesh" % path)


def case_a_colour_takes_the_old_number():
    """The other way round: a part changes color, and the new color takes
    the number the old one had. The update saw the same numbers, reported
    the part as unchanged, and left the old color on it."""
    fresh()
    first = write_mesh(
        "paint", [(1, "a", 0.05, [1]), (2, "b", 0.06, [2])],
        [(1, "c001", "a", "a-1", 0.0, None),
         (2, "c002", "b", "b-1", 0.2, None)],
        materials=[_GREY, _RED, _BLUE])
    native_import.build(bpy.context, first, hierarchy="FLAT")
    b_mesh = by_path("b-1").data.name
    second = write_mesh(
        "paint", [(1, "a", 0.05, [1]), (2, "b", 0.06, [2])],
        [(1, "c001", "a", "a-1", 0.0, None),
         (2, "c002", "b", "b-1", 0.2, None)],
        materials=[_GREY, _GREEN, _BLUE])
    _objects, _report, out = native_import.update(
        bpy.context, second, hierarchy="FLAT")
    a = by_path("a-1")
    check(out.reshaped == [a.name], "re-tessellated: %s" % out.reshaped)
    check(_slot_names(a) == ["SW green"], "a is %s" % _slot_names(a))
    check(by_path("b-1").data.name == b_mesh, "b was given a new mesh")


def case_a_geometry_tag_of_an_older_send():
    """A part of an older send carries the geometry hash of the material
    numbers. The first update after the change must not read every part
    as re-tessellated."""
    fresh()
    first = write_mesh(
        "paint", [(1, "a", 0.05, [1]), (2, "b", 0.06, [2])],
        [(1, "c001", "a", "a-1", 0.0, None),
         (2, "c002", "b", "b-1", 0.2, None)],
        materials=[_GREY, _RED, _BLUE])
    native_import.build(bpy.context, first, hierarchy="FLAT")
    export = swmesh.load(first)
    for definition, path in ((export.definitions[0], "a-1"),
                             (export.definitions[1], "b-1")):
        by_path(path)["SWMESH_geometry"] = \
            native_import._legacy_definition_hash(definition)
    meshes = {p: by_path(p).data.name for p in ("a-1", "b-1")}
    _objects, _report, out = native_import.update(
        bpy.context, first, hierarchy="FLAT")
    check(not out.reshaped, "re-tessellated: %s" % out.reshaped)
    for path, name in meshes.items():
        check(by_path(path).data.name == name, "%s was given a new mesh" % path)
        check(str(by_path(path).get("SWMESH_geometry")).startswith("v2:"),
              "%s kept the older hash" % path)


def case_a_renamed_document(hierarchy):
    """A document renamed for a new revision is found by the paths of its
    parts. The count took in the prototypes and the subassembly empties,
    which no export has, so an assembly of mostly unique parts in
    subassemblies was never found: the update built it again and threw
    away the Blender work."""
    fresh()
    defs = [(i, "part%d" % i, 0.01 * (i + 1), None) for i in range(6)]
    inst = [(i, "c%03d" % i, "part%d" % i,
             "sub%d-1/part%d-1" % (i // 3, i), 0.1 * i, None)
            for i in range(6)]
    nodes = [("sub0-1", "sub0", "", 0.0), ("sub1-1", "sub1", "", 0.3)]
    native_import.build(bpy.context,
                        write_mesh("rev_a", defs, inst, nodes),
                        hierarchy=hierarchy)
    mine = by_path("sub0-1/part0-1")
    mine.name = "my part"
    _objects, _report, out = native_import.update(
        bpy.context, write_mesh("rev_b", defs, inst, nodes),
        hierarchy=hierarchy)
    check(not out.added and not out.removed,
          "added %s, removed %s" % (out.added, out.removed))
    check(bpy.data.objects.get("my part") is not None,
          "the update rebuilt the assembly and lost the renamed part")


def case_a_send_into_another_scene():
    """A send into one scene replaces the send that stands in THAT scene.
    It took the parts and collections of every scene in the file."""
    fresh()
    m = _bolt_manifest()
    first = bpy.context.scene
    first.name = "v1"
    native_import.build(bpy.context, write_mesh("bolts", _BOLT_DEFS, _BOLT_INST),
                        manifest=m, hierarchy="TREE")
    theirs = sorted(o.name for o in first.objects)
    second = bpy.data.scenes.new("v2")
    bpy.context.window.scene = second
    native_import.build(bpy.context, write_mesh("other", _BOLT_DEFS, _BOLT_INST),
                        manifest=m, hierarchy="TREE")
    check(sorted(o.name for o in first.objects) == theirs,
          "scene v1 holds %s, it held %s"
          % (sorted(o.name for o in first.objects), theirs))
    # The import collection is in the top collection of the send.
    top = bpy.data.collections.get("bolts_Top_Level")
    check(top is not None and top.name in first.collection.children
          and "bolts" in top.children,
          "scene v1 lost its import collection")
    check(len([o for o in second.objects if o.get("SWMESH_path")]) == 2,
          "the send into v2 did not arrive")


def _keep_aside(obj, shown=False):
    """What an add-on does when it keeps the part a new object was made
    from: into a collection that no scene holds, kept by a fake user.
    `shown` links it back where it was as well, to look at."""
    keep = bpy.data.collections.get("Kept Aside") \
        or bpy.data.collections.new("Kept Aside")
    keep.use_fake_user = True
    home = list(obj.users_collection)
    for col in home:
        col.objects.unlink(obj)
    keep.objects.link(obj)
    if shown:
        for col in home:
            col.objects.link(obj)
    return keep


def case_a_part_kept_aside(shown):
    """A part kept aside by another add-on is that add-on's to keep. A send
    deleted it, and a Refresh linked it into the import again or deleted
    it when CAD removed the part."""
    fresh()
    m = _bolt_manifest()
    mesh = write_mesh("bolts", _BOLT_DEFS, _BOLT_INST)
    native_import.build(bpy.context, mesh, manifest=m, hierarchy="TREE")
    bolt = by_path("bolt-1")
    name = bolt.name
    keep = _keep_aside(bolt, shown)
    homes = sorted(c.name for c in bolt.users_collection)

    # Refresh, with the bolt now inside a new subassembly: its path changes.
    moved = write_mesh("bolts", _BOLT_DEFS, [
        (1, "c001", "base", "base-1", 0.0, None),
        (2, "c002", "bolt", "clamp-1/bolt-1", 0.3, None)],
        nodes=[("clamp-1", "clamp", "", 0.0)])
    m2 = manifest([("c001", "base-1", "pbase", 0.0, False),
                   ("c002", "clamp-1/bolt-1", "pbolt", 0.3, False)],
                  [("g000", ["c001"]), ("g001", ["c002"])])
    _objects, _report, out = native_import.update(
        bpy.context, moved, manifest=m2, hierarchy="TREE")
    bolt = bpy.data.objects.get(name)
    check(bolt is not None, "the refresh deleted the part kept aside")
    check(sorted(c.name for c in bolt.users_collection) == homes,
          "the refresh moved the part kept aside into %s"
          % sorted(c.name for c in bolt.users_collection))
    check(not out.added, "the refresh built the part again: %s" % out.added)

    # Refresh, with the bolt deleted in CAD.
    gone = write_mesh("bolts", _BOLT_DEFS[:1], _BOLT_INST[:1])
    native_import.update(bpy.context, gone, manifest=m, hierarchy="TREE")
    check(bpy.data.objects.get(name) is not None,
          "the refresh deleted the part kept aside when CAD removed it")

    # A new send of the assembly.
    native_import.build(bpy.context, mesh, manifest=m, hierarchy="TREE")
    bolt = bpy.data.objects.get(name)
    check(bolt is not None, "the send deleted the part kept aside")
    check(keep in bolt.users_collection, "the part left the collection it was kept in")


CASES = [
    ("TREE: a send into another scene leaves the first alone",
     case_a_send_into_another_scene),
    ("TREE: a part kept aside is left alone",
     lambda: case_a_part_kept_aside(False)),
    ("TREE: a part kept aside and shown is left alone",
     lambda: case_a_part_kept_aside(True)),
    ("COLLECTION_INSTANCES: a renamed document is found",
     lambda: case_a_renamed_document("COLLECTION_INSTANCES")),
    ("EMPTIES: a renamed document is found",
     lambda: case_a_renamed_document("EMPTIES")),
    ("FLAT: shifted material numbers are not new geometry",
     case_material_numbers_shift),
    ("FLAT: a new color on the old number is seen",
     case_a_colour_takes_the_old_number),
    ("FLAT: the geometry hash of an older send still matches",
     case_a_geometry_tag_of_an_older_send),
    ("COLLECTION_INSTANCES: a shifted definition finds its prototype",
     case_instances_find_their_prototype),
    ("COLLECTION_INSTANCES: a prototype of an older send is found",
     lambda: case_instances_find_their_prototype(older=True)),
    ("COLLECTION_INSTANCES: one placement takes new geometry alone",
     case_one_instance_takes_new_geometry),
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
        except Exception:                       # noqa: BLE001
            failed.append(name)
            traceback.print_exc()
            print("native_update_cases_smoke: FAIL: %s: it raised" % name)
    if failed:
        raise SystemExit("native_update_cases_smoke: FAIL: %d of %d case(s)"
                         % (len(failed), ran))
    print("native_update_cases_smoke: OK: %d case(s)" % ran)


main()
