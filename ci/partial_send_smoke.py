# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a send of ONLY THE SELECTED COMPONENTS.

The add-in filters the geometry by the selection, and the manifest still
describes the whole assembly, exactly as a STEP export does. The import
therefore sees a manifest with components that have no geometry, and it
must still build the rig, attach what did arrive, and leave the rest
alone rather than fail.

Run:  blender -b --factory-startup -P partial_send_smoke.py
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
    "step_export": {"file": "partial-smoke.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "base-1", "step_name": "base",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
        {"id": "c002", "sw_path": "arm-1", "step_name": "arm",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0.2], [0, 1, 0, 0], [0, 0, 1, 0],
                       [0, 0, 0, 1]]},
        {"id": "c003", "sw_path": "tip-1", "step_name": "tip",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0.4], [0, 1, 0, 0], [0, 0, 1, 0],
                       [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.3},
        {"id": "g001", "name": "arm", "components": ["c002"], "grounded": False,
         "frame": None, "bbox_diag": 0.2},
        {"id": "g002", "name": "tip", "components": ["c003"], "grounded": False,
         "frame": None, "bbox_diag": 0.2},
    ],
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [0.2, 0, 0],
         "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None},
        {"id": "j002", "type": "revolute", "parent_group": "g001",
         "child_group": "g002", "origin": [0.4, 0, 0],
         "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None},
    ],
    "loops": [],
    "warnings": [],
}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, components):
    """One triangle per component that the selection kept."""
    body = struct.pack("<III", swmesh.MAGIC, 1, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<III", 1, 1, len(components))
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

    # The selection kept the base and the tip. The arm between them is in
    # the manifest, drives the tip, and has no geometry at all.
    mesh_path = write_mesh(
        os.path.join(tempfile.gettempdir(), "partial_send_smoke.swmesh"),
        components=(("c001", "base-1", 0.0), ("c003", "tip-1", 0.4)))
    objects, report = native_import.build(bpy.context, mesh_path, manifest=m)
    assert len(objects) == 2, [o.name for o in objects]
    assert len(report.matched) == 2, report.matched
    assert report.frame_agree == 2, report.frame_agree

    by_component = {o["RIG_component_id"]: o for o in objects}
    assert set(by_component) == {"c001", "c003"}, sorted(by_component)
    assert by_component["c003"].get("RIG_group") == "g002"

    before = {cid: o.matrix_world.copy() for cid, o in by_component.items()}

    # The rig is built from the whole manifest, so the missing arm still
    # gets its bone: the tip hangs off it and would otherwise lose its
    # place in the chain.
    plan = graph.build(m)
    result = rig_build.build(bpy.context, m, plan, report.frame_rows)
    arm_obj = result.armature_object
    assert arm_obj is not None
    for gid in ("g000", "g001", "g002"):
        assert gid in result.bone_names, "group %s has no bone" % gid

    parent_report = parenting.relink(bpy.context, arm_obj)
    assert parent_report.bone_parented == 2, \
        "re-link attached %d of 2 parts" % parent_report.bone_parented

    bpy.context.view_layer.update()
    for cid, obj in by_component.items():
        assert obj.parent is arm_obj, "%s is not parented to the rig" % cid
        drift = (obj.matrix_world.translation - before[cid].translation).length
        assert drift < 1e-6, "%s moved %.3g m while being parented" % (cid, drift)

    # The chain still drives: turning the empty middle bone carries the tip
    # that DID arrive.
    pb = arm_obj.pose.bones[result.bone_names["g001"]]
    pb.rotation_mode = "XYZ"
    pb.rotation_euler[1] = 0.4
    bpy.context.view_layer.update()
    turned = (before["c003"].to_quaternion().rotation_difference(
        by_component["c003"].matrix_world.to_quaternion()).angle)
    assert abs(turned - 0.4) < 1e-4, \
        "the tip turned %.4f rad, not 0.4, through the bone with no geometry" % turned

    print("partial send smoke: OK (2 of 3 components, %d bones, chain drives)"
          % len(result.bone_names))


main()
