# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a loop that takes two inputs.

    blender -b --factory-startup -P ci/rig_fivebar_smoke.py

Five pins round one ring hold four moving bodies. Five freedoms, three of
them spent closing the ring, so the ring takes TWO inputs, not one.

Live wrench.sldasm (2026-09-16, Oscar): "the arm2 assembly can rotate
around a pin on clamp2, but the only control bone I am getting in Blender
is the screw which IS correct, but there should be an additional revolute
joint". The solver had every driven body, so the second freedom had
nowhere to go.

The rig must therefore leave one bone of the driven chain OUT of the
solve, and the loop must still close from either input.
"""

import json
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, manifest as man_mod, rig_build  # noqa: E402

# ground -j001- a -j003- b -j004- c -j005- d -j002- ground.
# j002 is the input and j005 the cut, so the driven branch is c, b, a.
PINS = {
    "j001": (0.00, 0.00),
    "j003": (0.10, 0.10),
    "j004": (0.25, 0.15),
    "j005": (0.35, 0.05),
    "j002": (0.40, 0.00),
}


def rev(jid, parent, child):
    x, y = PINS[jid]
    return {"id": jid, "type": "revolute",
            "parent_group": parent, "child_group": child,
            "origin": [x, y, 0.0], "axis": [0.0, 0.0, 1.0],
            "secondary_axis": [1.0, 0.0, 0.0], "limits": None, "coupling": None}


MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "fivebar.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c%03d" % i, "sw_path": n + "-1", "step_name": n,
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}
        for i, n in enumerate(["ground", "a", "b", "c", "d"], 1)
    ],
    "rigid_groups": [
        {"id": "g000", "name": "ground", "components": ["c001"],
         "grounded": True,
         "frame": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
         "bbox_diag": 0.5},
    ] + [
        {"id": "g%03d" % i, "name": n, "components": ["c%03d" % (i + 1)],
         "grounded": False, "frame": None, "bbox_diag": 0.12}
        for i, n in enumerate(["a", "b", "c", "d"], 1)
    ],
    "joints": [
        rev("j001", "g000", "g001"),
        rev("j002", "g000", "g004"),
        rev("j003", "g001", "g002"),
        rev("j004", "g002", "g003"),
        rev("j005", "g003", "g004"),
    ],
    "loops": [{
        "id": "loop001",
        "member_joints": ["j001", "j002", "j003", "j004", "j005"],
        "closure_joint": "j005",
        "closure_kind": "ik",
        "suggested_driver_joint": "j002",
        "mobility": 2,
        "planar": True,
        "plane_normal": [0.0, 0.0, 1.0],
        "driver_candidates": [],
    }],
    "warnings": [],
}


def _check(cond, msg):
    if not cond:
        raise SystemExit("rig_fivebar_smoke: FAIL: " + msg)


def _closure_gap(result, plan):
    """How far apart the two ends of the cut have drifted, in metres.

    The effector's tail IS the closure point and the helper sits on the
    same point on the other side, so their world positions agreeing is the
    loop being closed.
    """
    lplan = plan.loops[0]
    arm_obj = result.armature_object
    helper = arm_obj.pose.bones[_helper_name(result, lplan)]
    effector = arm_obj.pose.bones[_effector_name(result, lplan)]
    return (arm_obj.matrix_world @ effector.tail
            - arm_obj.matrix_world @ helper.head).length


def _helper_name(result, lplan):
    return result.helper_names[lplan.loop.id]


def _effector_name(result, lplan):
    return result.effector_names[lplan.loop.id]


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
    lplan = plan.loops[0]

    # One bone per spare input stays out of the solve, from the root end.
    _check(lplan.driven_chain == ["g003", "g002"],
           "the solved chain is %s, want the tip and one more"
           % lplan.driven_chain)
    _check(lplan.chain_count == 2,
           "chain_count is %d, want 2" % lplan.chain_count)

    result = rig_build.build(bpy.context, m, plan)
    arm = result.armature_object
    driver = arm.pose.bones[result.bone_names["g004"]]
    freed = arm.pose.bones[result.bone_names["g001"]]

    # The freed bone is a bone the user can turn, and it turns about its
    # own pin like any other control.
    _check(not all(freed.lock_rotation),
           "the freed bone is locked in rotation: %s" % list(freed.lock_rotation))
    for pb, name in ((driver, "driver"), (freed, "freed")):
        _check(not any(con.type == "IK" for con in pb.constraints),
               "the %s bone carries an IK constraint of its own" % name)

    gap = _closure_gap(result, plan)
    _check(gap < 1e-4, "the loop is open at rest by %.6f m" % gap)

    # Either input closes the loop, and each one MOVES the mechanism: a
    # second control that changes nothing is the fault this tests for.
    for pb, turn in ((driver, 0.10), (freed, -0.08), (driver, -0.15)):
        pb.rotation_mode = "XYZ"
        # The pin between b and c: it is held by a bone at each end, so it
        # can only move if the whole linkage does.
        pin = arm.pose.bones[result.bone_names["g003"]]
        before = pin.matrix.translation.copy()
        pb.rotation_euler[1] += turn
        bpy.context.view_layer.update()
        after = pin.matrix.translation
        moved = (after - before).length
        gap = _closure_gap(result, plan)
        _check(gap < 1e-3,
               "a %.2f rad turn of %s opened the loop by %.6f m"
               % (turn, pb.name, gap))
        _check(moved > 1e-4,
               "a %.2f rad turn of %s moved the linkage %.7f m: nothing"
               % (turn, pb.name, moved))

    print("rig_fivebar_smoke: OK: two inputs on one ring, the solver takes "
          "two of the three driven bodies, and the ring closes from either "
          "input")


main()
