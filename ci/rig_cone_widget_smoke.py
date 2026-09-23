# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: the cone a limited ball's handle wears is the real cone.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_cone_widget_smoke.py

The limit of a ball is an unsigned band of angles about the socket's cone
axis, and the constraints clamp the stud to that band (constraints.py
apply_ball_cone). A stud can rest tilted inside it. The widget has to show
the same cone: open by the limit itself, about the cone axis, and not by
the distance from the rest tilt to the limit about the rest direction.
"""

import math
import os
import sys

import bpy
from mathutils import Euler, Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, manifest as man_mod, rig_build  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("   FAIL:", msg)
    return cond


def ident4(x=0.0):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def manifest(rest_tilt):
    """A stud in a socket that opens along +Z, limited to 45 degrees and
    resting `rest_tilt` radians over toward +X."""
    u = [math.sin(rest_tilt), 0.0, math.cos(rest_tilt)]
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "cone.step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": "c001", "sw_path": "socket-1", "step_name": "socket",
             "step_occurrence_path": None, "transform": ident4()},
            {"id": "c002", "sw_path": "stud-1", "step_name": "stud",
             "step_occurrence_path": None, "transform": ident4(0.2)},
        ],
        "rigid_groups": [
            {"id": "g000", "name": "socket", "components": ["c001"],
             "grounded": True, "frame": None, "bbox_diag": 0.2},
            {"id": "g001", "name": "stud", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": 0.2},
        ],
        "joints": [
            {"id": "j001", "type": "ball", "parent_group": "g000",
             "child_group": "g001", "origin": [0.2, 0, 0],
             "axis": [0, 0, 1], "secondary_axis": u,
             "limits": {"rotation": {"min": 0.0,
                                     "max": math.radians(45.0),
                                     "value_at_rest": rest_tilt},
                        "translation": None}},
        ],
        "loops": [], "warnings": [],
    }


def cone_of(rest_tilt):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    m = man_mod.parse(manifest(rest_tilt))
    result = rig_build.build(bpy.context, m, graph.build(m))
    arm = result.armature_object
    handle = arm.pose.bones[result.ball_ctrl_names["g001"]]
    shape = handle.custom_shape
    if not check(shape is not None, "the handle wears no widget"):
        return None, None
    # The widget's own +Y is the cone's axis, and its rim is the limit.
    turn = Euler(handle.custom_shape_rotation_euler, "XYZ").to_matrix()
    axis = (handle.bone.matrix_local.to_3x3() @ turn
            @ Vector((0.0, 1.0, 0.0))).normalized()
    rim = [v.co for v in shape.data.vertices if v.co.length > 1e-6]
    half = max(Vector((0.0, 1.0, 0.0)).angle(v) for v in rim)
    return axis, half


def main():
    for tilt in (0.0, math.radians(30.0)):
        print("-- a stud resting %.0f degrees off the cone axis"
              % math.degrees(tilt))
        axis, half = cone_of(tilt)
        if axis is None:
            continue
        check(abs(math.degrees(half) - 45.0) < 0.1,
              "the widget opens %.1f degrees, the limit is 45"
              % math.degrees(half))
        check(axis.angle(Vector((0.0, 0.0, 1.0))) < 1e-4,
              "the widget's axis is %s, the cone axis is +Z" % (tuple(axis),))

    print()
    if FAILS:
        print("rig_cone_widget_smoke: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("rig_cone_widget_smoke: OK")


main()
