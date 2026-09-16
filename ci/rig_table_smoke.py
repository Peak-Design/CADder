"""Headless smoke for the table coupling: a cam.

    blender -b --factory-startup -P ci/rig_table_smoke.py

A cam hinged on the ground about +Z, a follower sliding along +X on the
ground, and the relation the exporter's probe read off the model: a lift
of 10 mm, v(x) = 0.01 (1 - cos x), one revolution in 24 samples. Turning
the cam bone has to move the follower by the table, linearly interpolated
between samples, and a turn and a bit reads like the bit (the table is
periodic).
"""
import json
import math
import os
import sys
import tempfile

import bpy
from mathutils import Matrix

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from STEPper_NEXT.rig import graph, manifest as man_mod, rig_build  # noqa: E402


def _t(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


TWO_PI = 2.0 * math.pi
SAMPLES = [[TWO_PI * k / 24, 0.01 * (1.0 - math.cos(TWO_PI * k / 24))]
           for k in range(25)]
SAMPLES[-1][1] = 0.0

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.SwToBlender", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "table-smoke.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "ground-1", "step_name": "ground",
         "step_occurrence_path": None, "transform": _t(0, 0, 0)},
        {"id": "c002", "sw_path": "cam-1", "step_name": "cam",
         "step_occurrence_path": None, "transform": _t(0, 0, 0)},
        {"id": "c003", "sw_path": "follower-1", "step_name": "follower",
         "step_occurrence_path": None, "transform": _t(0.05, 0, 0)},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "ground", "components": ["c001"],
         "grounded": True, "frame": None, "bbox_diag": 0.2},
        {"id": "g001", "name": "cam", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.06},
        {"id": "g002", "name": "follower", "components": ["c003"],
         "grounded": False, "frame": None, "bbox_diag": 0.04},
    ],
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [0, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
        {"id": "j002", "type": "prismatic", "parent_group": "g000",
         "child_group": "g002", "origin": [0.05, 0, 0], "axis": [1, 0, 0],
         "secondary_axis": [0, 0, 1], "limits": None,
         "coupling": {"kind": "table", "driver_joint": "j001",
                      "samples": SAMPLES, "periodic": True, "period": TWO_PI}},
    ],
    "loops": [],
    "warnings": [],
}


def table(x):
    x = x % TWO_PI
    for (x0, y0), (x1, y1) in zip(SAMPLES, SAMPLES[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return SAMPLES[-1][1]


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json",
                                     delete=False) as fh:
        json.dump(MANIFEST, fh)
        path = fh.name
    try:
        m = man_mod.load(path)
    finally:
        os.unlink(path)
    c = m.joints[1].coupling
    assert c.kind == "table" and c.periodic and len(c.samples) == 25, c

    plan = graph.build(m)
    result = rig_build.build(bpy.context, m, plan, None)
    assert not result.warnings, result.warnings
    arm = result.armature_object
    pose = arm.pose
    cam = pose.bones[result.bone_names["g001"]]
    follower = pose.bones[result.bone_names["g002"]]
    fcurves = [fc for fc in (arm.animation_data.drivers if arm.animation_data else [])
               if fc.data_path.endswith("location") and follower.name in fc.data_path]
    assert fcurves, "no driver on the follower's location"
    fc = fcurves[0]
    assert len(fc.keyframe_points) == 25, len(fc.keyframe_points)
    assert any(mod.type == "CYCLES" for mod in fc.modifiers), "a periodic table needs a cycles modifier"

    rest3 = arm.data.bones[cam.name].matrix_local.to_3x3()
    bpy.context.view_layer.update()
    x0 = (arm.matrix_world @ follower.matrix).translation.x
    worst = 0.0
    for theta in (0.0, 0.3, 1.0, math.pi, 4.5, TWO_PI + 0.7, -0.7):
        cam.matrix_basis = (rest3.inverted() @ Matrix.Rotation(theta, 3, "Z")
                            @ rest3).to_4x4()
        bpy.context.view_layer.update()
        got = (arm.matrix_world @ follower.matrix).translation.x - x0
        want = table(theta)
        err = abs(got - want)
        worst = max(worst, err)
        assert err < 1e-6, "theta %.2f: follower moved %.6f, the table says %.6f" % (
            theta, got, want)
    print("rig_table_smoke: OK: a cam table drives the follower through 7 poses, "
          "worst error %.1e m, periodic past one turn" % worst)


if __name__ == "__main__":
    main()
