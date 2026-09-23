# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Rebuild from CAD > Poses after a part was renamed.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_pose_rename_smoke.py

Pose sync found each part by the name it had when the send ran. A part the
user renamed in the outliner since then was not found, stayed where it
was, and relink bound it there. The report said nothing about it. The poses
here come in as the CAD application sends them, and go through the same
path as the Poses button.
"""

import json
import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import swmesh, ui as rig_ui  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "rig_pose_rename_smoke")


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


PARTS = [("c001", "base-1", "pbase", 0.0), ("c002", "rod-2", "prod", 0.3)]


def export():
    os.makedirs(TMP, exist_ok=True)
    man = os.path.join(TMP, "piston.rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump({
            "manifest_version": "1.0.0",
            "generator": {"name": "Peak.Cadder", "version": "smoke"},
            "units": {"length": "meter", "angle": "radian"},
            "frame": {"handedness": "right", "up_axis": "Z",
                      "transform_convention": "row_major_4x4_global"},
            "step_export": {"file": "piston.step", "ap": "AP214",
                            "sha1": None, "occurrence_matching": None},
            "components": [
                {"id": cid, "sw_path": path, "step_name": path,
                 "step_occurrence_path": None, "sw_persistent_id": pid,
                 "transform": _t(x)} for cid, path, pid, x in PARTS],
            "rigid_groups": [
                {"id": "g000", "name": "base", "components": ["c001"],
                 "grounded": True, "frame": None, "bbox_diag": 0.2},
                {"id": "g001", "name": "rod", "components": ["c002"],
                 "grounded": False, "frame": None, "bbox_diag": 0.2}],
            "joints": [
                {"id": "j001", "type": "prismatic", "parent_group": "g000",
                 "child_group": "g001", "origin": [0.3, 0, 0],
                 "axis": [1, 0, 0], "secondary_axis": [0, 1, 0],
                 "limits": None}],
            "loops": [], "warnings": [],
        }, fh)
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, 1, len(PARTS), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.05, 0, 0, 0, 0.05, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, path, _pid, x in PARTS:
        body += (struct.pack("<i", 1) + _text(cid) + _text(path)
                 + _text(path) + struct.pack("<16d", *_flat(_t(x)))
                 + struct.pack("<B", 0))
    mesh = os.path.join(TMP, "piston.swmesh")
    with open(mesh, "wb") as fh:
        fh.write(body)
    return mesh, man


def fail(msg):
    raise SystemExit("rig_pose_rename_smoke: FAIL: " + msg)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    mesh, man = export()
    result = bridge._run_job({
        "step": None, "mesh": mesh, "manifest": man,
        "steps": {"import": False, "replace": True, "match": True,
                  "sync_poses": True, "build_rig": True, "relink": True,
                  "cleanup": True},
        "import_options": {"hierarchy_types": "FLAT", "up_as": "ZPOS"},
    })
    if not result.get("ok"):
        fail("the send failed: %s" % result.get("error"))
    rod = next(o for o in bpy.data.objects if o.get("SWMESH_path") == "rod-2")
    rod.name = "Piston Rod"
    # Something else takes the old name.
    bpy.data.objects.new("rod-2", None)

    # The rod moved in SolidWorks, and the user presses Poses.
    moved = rig_ui._apply_poses(bpy.context, {"components": [
        {"id": "c001", "transform": _flat(_t(0.0))},
        {"id": "c002", "transform": _flat(_t(0.45))}]})
    bpy.context.view_layer.update()
    rod = bpy.data.objects["Piston Rod"]
    x = rod.matrix_world.translation.x
    if abs(x - 0.45) > 1e-5:
        fail("the renamed rod is at x=%.4f, the CAD has it at 0.45" % x)
    if moved < 1:
        fail("the poses moved %d part(s)" % moved)
    other = bpy.data.objects["rod-2"]
    if other.matrix_world.translation.length > 1e-9:
        fail("the object that took the old name was moved")
    print("rig_pose_rename_smoke: OK: a renamed part took its CAD pose, and "
          "the object that took its old name stayed where it was")


main()
