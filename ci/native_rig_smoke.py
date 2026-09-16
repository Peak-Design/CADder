# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the direct link driving the WHOLE pipeline: manifest,
native geometry, rig, and (the part that broke live on 2026-08-24) the
re-link that attaches parts to bones.

That bug is the reason this exists. The native importer tagged its objects
RIG_rig, which means "part of the rig's own scaffolding", so re-linking
skipped every one of them, and it never wrote RIG_group, which is what
re-linking attaches BY. The parts arrived in exactly the right place and
were attached to nothing, which looks completely correct until you move a
bone.

Run:  blender -b --factory-startup -P native_rig_smoke.py
"""

import copy
import json
import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import (  # noqa: E402
    graph, manifest as man_mod, native_import, parenting, rig_build, swmesh)

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "native-smoke.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "base-1", "step_name": "base",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
        {"id": "c002", "sw_path": "arm-1", "step_name": "arm",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0.2], [0, 1, 0, 0], [0, 0, 1, 0],
                       [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.3},
        {"id": "g001", "name": "arm", "components": ["c002"], "grounded": False,
         "frame": None, "bbox_diag": 0.2},
    ],
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [0.2, 0, 0],
         "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None},
    ],
    "loops": [],
    "warnings": [],
}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, components=(("c001", "base-1", 0.0), ("c002", "arm-1", 0.2))):
    """One triangle per component, placed at the manifest's transforms."""
    body = struct.pack("<III", swmesh.MAGIC, 1, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<III", 1, 1, 2)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0) + _text("")
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.1, 0, 0, 0, 0.1, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, name, tx in components:
        rows = [1, 0, 0, tx, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        body += struct.pack("<i", 1) + _text(cid) + _text(name) \
            + struct.pack("<16d", *rows)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    with tempfile.NamedTemporaryFile("w", suffix=".rig.json",
                                     delete=False) as fh:
        json.dump(MANIFEST, fh)
        manifest_path = fh.name
    try:
        m = man_mod.load(manifest_path)
    finally:
        os.unlink(manifest_path)

    mesh_path = write_mesh(
        os.path.join(tempfile.gettempdir(), "native_rig_smoke.swmesh"))
    objects, report = native_import.build(bpy.context, mesh_path, manifest=m)
    assert len(objects) == 2, [o.name for o in objects]
    assert len(report.matched) == 2

    by_component = {o["RIG_component_id"]: o for o in objects}
    # The two tags the re-link stage depends on, and the one that must NOT
    # be there.
    for cid, gid in (("c001", "g000"), ("c002", "g001")):
        obj = by_component[cid]
        assert obj.get("RIG_group") == gid, (cid, obj.get("RIG_group"))
        assert "RIG_rig" not in obj.keys(), \
            "geometry tagged as rig scaffolding is skipped by re-linking"

    before = {cid: o.matrix_world.copy() for cid, o in by_component.items()}

    plan = graph.build(m)
    result = rig_build.build(bpy.context, m, plan, report.frame_rows)
    arm = result.armature_object
    assert arm is not None

    parent_report = parenting.relink(bpy.context, arm)
    assert parent_report.bone_parented == 2, \
        "re-link attached %d of 2 parts" % parent_report.bone_parented
    assert not parent_report.missing_groups, parent_report.missing_groups

    bpy.context.view_layer.update()
    for cid, obj in by_component.items():
        assert obj.parent is arm, "%s is not parented to the rig" % cid
        assert obj.parent_type == "BONE" and obj.parent_bone
        drift = (obj.matrix_world.translation - before[cid].translation).length
        assert drift < 1e-6, "%s moved %.3g m while being parented" % (cid, drift)

    # And the rig actually drives them: pose the child bone, the arm follows.
    # Measured as ROTATION, not position: the joint origin and the part's
    # origin coincide here, so spinning about the pivot leaves the object's
    # location exactly where it was.
    child_bone = result.bone_names["g001"]
    part = by_component["c002"]
    pb = arm.pose.bones[child_bone]
    pb.rotation_mode = "XYZ"
    pb.rotation_euler[1] = 0.5
    bpy.context.view_layer.update()
    turned = (before["c002"].to_quaternion().rotation_difference(
        part.matrix_world.to_quaternion()).angle)
    assert abs(turned - 0.5) < 1e-4,         "posing the bone turned the part it owns by %.4f rad, not 0.5" % turned

    # A DIFFERENT assembly over the direct link replaces the native import
    # wholesale, so the rig that drove the old parts is left over nothing
    # and must go with them (live 2026-09-14: every send of another
    # assembly stacked one more dead armature). A rig that drives geometry
    # from elsewhere (a STEP import) is not touched.
    keep_col = bpy.data.collections.new("keep_Rig")
    bpy.context.scene.collection.children.link(keep_col)
    keep_arm = bpy.data.objects.new("keep_Rig", bpy.data.armatures.new("keep_Rig"))
    keep_arm["RIG_rig"] = True
    keep_col.objects.link(keep_arm)
    step_part = bpy.data.objects.new("step_part", None)
    step_part["STEP_file"] = "other.step"
    step_part.parent = keep_arm
    bpy.context.scene.collection.objects.link(step_part)

    other_mesh = write_mesh(
        os.path.join(tempfile.gettempdir(), "native_rig_smoke_other.swmesh"),
        components=(("c101", "frame-1", 0.0), ("c102", "lever-1", 0.3)))
    rig_name = arm.name
    rig_collection_names = [c.name for c in arm.users_collection]
    objects2, _ = native_import.build(bpy.context, other_mesh, manifest=None)
    assert len(objects2) == 2
    assert bpy.data.objects.get(rig_name) is None, \
        "the rig of the replaced assembly is still in the scene"
    dead = [o.name for o in bpy.data.objects
            if o.get("RIG_rig") and o.name != keep_arm.name]
    assert not dead, "rig scaffolding left behind: %s" % dead
    for name in rig_collection_names:
        assert bpy.data.collections.get(name) is None, \
            "empty rig collection %s left behind" % name
    assert bpy.data.objects.get("keep_Rig") is not None, \
        "a rig driving geometry from elsewhere was removed"
    assert step_part.parent is keep_arm
    widgets = bpy.data.collections.get("SW_widgets")
    assert widgets is not None and widgets.name in [
        c.name for c in bpy.context.scene.collection.children_recursive], \
        "the shared widget collection left the scene with the dead rig"

    # The SAME assembly sent twice more: each send replaces the previous
    # one's parts AND its rig in place. The rig collection is parked inside
    # the native collection, so the replace must lift it out before that
    # collection goes, or the rebuild finds no rig collection in the scene
    # and stacks a numbered copy beside an armature nobody can see.
    other = copy.deepcopy(MANIFEST)
    other["step_export"]["file"] = "native-other.step"
    for c, cid, path, name in zip(other["components"], ("c101", "c102"),
                                  ("frame-1", "lever-1"), ("frame", "lever")):
        c["id"], c["sw_path"], c["step_name"] = cid, path, name
    other["components"][1]["transform"][0][3] = 0.3
    other["rigid_groups"][0]["components"] = ["c101"]
    other["rigid_groups"][1]["components"] = ["c102"]
    other["joints"][0]["origin"] = [0.3, 0, 0]
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json",
                                     delete=False) as fh:
        json.dump(other, fh)
        other_path = fh.name
    try:
        m2 = man_mod.load(other_path)
    finally:
        os.unlink(other_path)
    for round_no in (1, 2, 3):
        objects2, report2 = native_import.build(bpy.context, other_mesh, manifest=m2)
        plan2 = graph.build(m2)
        result2 = rig_build.build(bpy.context, m2, plan2, report2.frame_rows)
        parenting.relink(bpy.context, result2.armature_object)
        arms = [o.name for o in bpy.data.objects
                if o.type == "ARMATURE" and o.name != "keep_Rig"]
        assert arms == [result2.armature_object.name], \
            "round %d: armatures %s" % (round_no, arms)
        assert "." not in result2.armature_object.name.replace("native-other", ""), \
            "round %d: rig renamed to %s" % (round_no, result2.armature_object.name)
        in_scene = [c.name for c in bpy.context.scene.collection.children_recursive]
        for col in result2.armature_object.users_collection:
            assert col.name in in_scene, \
                "round %d: rig collection %s is not in the scene" % (round_no, col.name)
        rig_cols = [c.name for c in bpy.data.collections if c.name.endswith("_Rig")]
        assert sorted(rig_cols) == sorted({"keep_Rig", "native-other_Rig"}), \
            "round %d: rig collections %s" % (round_no, rig_cols)

    # The direct link sent for an assembly that already stands as a STEP
    # import replaces that import: the bridge matches on the file stem, so
    # native-other.swmesh takes native-other.step's objects away and leaves
    # another file's alone.
    from CADder import bridge
    step_col = bpy.data.collections.new("native-other.hierarchy")
    bpy.context.scene.collection.children.link(step_col)
    for name, file in (("step_lever", "native-other.step"),
                       ("step_frame", "NATIVE-OTHER.STEP"),
                       ("step_stranger", "elsewhere.step")):
        o = bpy.data.objects.new(name, None)
        o["STEP_file"] = r"C:\somewhere\\" + file
        step_col.objects.link(o)
    stages = {}
    bridge._remove_previous_import(
        os.path.join(tempfile.gettempdir(), "native-other.swmesh"), stages, by_stem=True)
    assert stages["replace"]["removed_objects"] == 2, stages
    assert bpy.data.objects.get("step_stranger") is not None
    assert bpy.data.objects.get("step_lever") is None

    print("native_rig_smoke: OK: %d parts bone-parented with no drift, "
          "a %.2f rad bone pose turns its part by the same, a second "
          "assembly takes the first one's rig away with its parts, three "
          "sends of one assembly leave one rig, and a direct send replaces "
          "the STEP import of its own assembly"
          % (parent_report.bone_parented, turned))


main()
