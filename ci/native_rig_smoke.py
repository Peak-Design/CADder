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

import json
import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from STEPper_NEXT.rig import (  # noqa: E402
    graph, manifest as man_mod, native_import, parenting, rig_build, swmesh)

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.SwToBlender", "version": "smoke"},
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


def write_mesh(path):
    """One triangle per component, placed at the manifest's transforms."""
    body = struct.pack("<III", swmesh.MAGIC, swmesh.VERSION, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<III", 1, 1, 2)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0) + _text("")
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.1, 0, 0, 0, 0.1, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, name, tx in (("c001", "base-1", 0.0), ("c002", "arm-1", 0.2)):
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

    print("native_rig_smoke: OK: %d parts bone-parented with no drift, "
          "and a %.2f rad bone pose turns its part by the same"
          % (parent_report.bone_parented, turned))


main()
