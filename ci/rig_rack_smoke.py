# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a rack and pinion: turn the pinion, the rack slides.

    blender -b --factory-startup -P ci/rig_rack_smoke.py

The coupling has been in the manifest and in drivers.py for a while and
had never been driven end to end. The live SolidWorks sample (2026-09-16)
arrived with the pinion on a revolute, the rack FREE, and the coupling
hanging off that free joint: "it creates a revolute for the pinion and a
slider for the rack but they don't interact" (Oscar). The exporter half of
that is pinned in the add-in's RackPinionReplayTests; this is the Blender
half, with the manifest the fixed exporter now writes.

Pitch radius 12.7 mm, so one radian of pinion is 12.7 mm of rack.
"""

import json
import math
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, manifest as man_mod, rig_build  # noqa: E402

METRES_PER_RADIAN = 0.0127

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "rack.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "rack-1", "step_name": "rack",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0.05], [0, 0, 1, 0],
                       [0, 0, 0, 1]]},
        {"id": "c002", "sw_path": "pinion-1", "step_name": "pinion",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0],
                       [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "assembly", "components": [], "grounded": True,
         "frame": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
         "bbox_diag": 0.3},
        {"id": "g001", "name": "rack", "components": ["c001"],
         "grounded": False, "frame": None, "bbox_diag": 0.15},
        {"id": "g002", "name": "pinion", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.03},
    ],
    "joints": [
        # The pinion turns about Z.
        {"id": "j002", "type": "revolute", "parent_group": "g000",
         "child_group": "g002", "origin": [0, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None, "coupling": None},
        # The rack slides along Y, driven by the pinion.
        {"id": "j001", "type": "prismatic", "parent_group": "g000",
         "child_group": "g001", "origin": [0, 0.0127, 0], "axis": [0, 1, 0],
         "secondary_axis": [1, 0, 0], "limits": None,
         "coupling": {"kind": "rack_pinion", "driver_joint": "j002",
                      "meters_per_radian": METRES_PER_RADIAN}},
    ],
    "loops": [], "warnings": [],
}


def _check(cond, msg):
    if not cond:
        raise SystemExit("rig_rack_smoke: FAIL: " + msg)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json", delete=False) as fh:
        json.dump(MANIFEST, fh)
        path = fh.name
    try:
        m = man_mod.load(path)
    finally:
        os.unlink(path)

    plan = graph.build(m)
    result = rig_build.build(bpy.context, m, plan)
    arm = result.armature_object
    rack = arm.pose.bones[result.bone_names["g001"]]
    pinion = arm.pose.bones[result.bone_names["g002"]]

    # The driver is on the rack's own slide channel, reading the pinion.
    animation = arm.animation_data
    _check(animation is not None and len(animation.drivers) > 0,
           "the coupling made no driver")
    paths = [fc.data_path for fc in animation.drivers]
    _check(any(rack.name in p and "location" in p for p in paths),
           "no driver on the rack's location: %s" % paths)

    start = rack.matrix.translation.copy()
    for turn in (0.5, -0.5, math.pi / 4):
        pinion.rotation_mode = "XYZ"
        pinion.rotation_euler[1] = turn        # the bone's own axis is +Y
        bpy.context.view_layer.update()
        moved = (rack.matrix.translation - start).length
        want = abs(turn) * METRES_PER_RADIAN
        _check(abs(moved - want) < 1e-6,
               "a %.3f rad turn slid the rack %.6f m, want %.6f"
               % (turn, moved, want))

    # And the rack is not free to be dragged anywhere else: the two
    # channels across its slide stay locked.
    _check(rack.lock_location[0] and rack.lock_location[2],
           "the rack can be dragged off its slide: %s"
           % list(rack.lock_location))

    print("rig_rack_smoke: OK: one radian of pinion slides the rack %.4f m, "
          "both ways, and the rack stays on its line" % METRES_PER_RADIAN)


main()
