# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a rack drives a pinion that is held by a concentric
mate alone.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_rack_cylinder_smoke.py

The exporter prefers a hinge for the pinion, but a pinion held only by a
concentric mate arrives as a cylindrical joint. rig_rack_smoke covers the
hinge. Here the user takes the RACK as the input of the pair: the coupling
turns round onto the cylindrical pinion, and sliding the rack has to turn
the pinion. It wrote the pinion's own slide from the rack's (locked) turn
instead, so the pinion never turned.

Pitch radius 12.7 mm, so 12.7 mm of rack is one radian of pinion.
"""

import math
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, inputs, manifest as man_mod, rig_build  # noqa: E402

METRES_PER_RADIAN = 0.0127
I4 = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "rackcyl.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "rack-1", "step_name": "rack",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0.05], [0, 0, 1, 0],
                       [0, 0, 0, 1]]},
        {"id": "c002", "sw_path": "pinion-1", "step_name": "pinion",
         "step_occurrence_path": None, "transform": I4},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "assembly", "components": [], "grounded": True,
         "frame": I4, "bbox_diag": 0.3},
        {"id": "g001", "name": "rack", "components": ["c001"],
         "grounded": False, "frame": None, "bbox_diag": 0.15},
        {"id": "g002", "name": "pinion", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.03},
    ],
    "joints": [
        # The pinion turns about Z, and a concentric mate alone also lets
        # it slide along Z.
        {"id": "j002", "type": "cylindrical", "parent_group": "g000",
         "child_group": "g002", "origin": [0, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None, "coupling": None},
        {"id": "j001", "type": "prismatic", "parent_group": "g000",
         "child_group": "g001", "origin": [0, 0.0127, 0], "axis": [0, 1, 0],
         "secondary_axis": [1, 0, 0], "limits": None,
         "coupling": {"kind": "rack_pinion", "driver_joint": "j002",
                      "meters_per_radian": METRES_PER_RADIAN}},
    ],
    "loops": [],
    "mechanisms": [{
        "id": "mech001",
        "loops": [],
        "inputs": [{"joint": "j002", "loops": [], "flipped_joints": [],
                    "joint_limits": []},
                   {"joint": "j001", "loops": [], "flipped_joints": [],
                    "joint_limits": []}],
    }],
    "warnings": [],
}


def _check(cond, msg):
    if not cond:
        raise SystemExit("rig_rack_cylinder_smoke: FAIL: " + msg)


def build(m):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    result = rig_build.build(bpy.context, m, graph.build(m))
    arm = result.armature_object
    return (arm, arm.pose.bones[result.bone_names["g001"]],
            arm.pose.bones[result.bone_names["g002"]])


def main():
    m = man_mod.parse(MANIFEST)

    # As exported: the pinion drives, and the rack slides.
    arm, rack, pinion = build(m)
    start = rack.matrix.translation.copy()
    pinion.rotation_mode = "XYZ"
    pinion.rotation_euler[1] = 0.5
    bpy.context.view_layer.update()
    moved = (rack.matrix.translation - start).length
    _check(abs(moved - 0.5 * METRES_PER_RADIAN) < 1e-6,
           "a 0.5 rad turn of the pinion slid the rack %.6f m" % moved)

    # Turned round: the rack drives, and the pinion turns.
    mech = inputs.mechanisms(m)[0]
    _check(inputs.apply(m, mech, "j001"), "taking the rack changed nothing")
    arm, rack, pinion = build(m)
    paths = [fc.data_path for fc in arm.animation_data.drivers]
    _check(any(pinion.name in p and "rotation" in p for p in paths),
           "no driver on the pinion's turn: %s" % paths)
    _check(not any(pinion.name in p and "location" in p for p in paths),
           "a driver holds the pinion's own slide: %s" % paths)

    start = pinion.matrix.to_quaternion()
    for slide in (0.0127, -0.0127):
        rack.location[1] = slide
        bpy.context.view_layer.update()
        turned = start.rotation_difference(pinion.matrix.to_quaternion()).angle
        _check(abs(turned - 1.0) < 1e-4,
               "%.4f m of rack turned the pinion %.6f rad, want 1"
               % (slide, turned))

    print("rig_rack_cylinder_smoke: OK: the rack turns a pinion that is "
          "held by a concentric mate alone, one radian per %.4f m"
          % METRES_PER_RADIAN)


main()
