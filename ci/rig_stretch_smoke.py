# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a slide INSIDE an IK-solved chain: the crank-driven
slider-crank.

    blender -b --factory-startup -P ci/rig_stretch_smoke.py

Seen down the pin axis (+Z), in metres:

    O = (0,0,0)          crank pivot on the ground        <- the input
    C = (1,0,0)          crank pin, rod's far end
    S = (SX,SY,0)        rod's pin on the slider, the rod 30 degrees up
    the slider itself sits at (4,0,0) and runs along X on the ground

Crank radius 1, rod length 3, SX = 1 + 3 cos 30, SY = 3 sin 30. The rest
pose is bent on purpose: a straight crank-rod line is singular for a
rotational solver (no rotation can shorten a straight arm), and a real
mechanism never rests there. Turn the crank by theta and the slider's pin
has to sit where the circle of radius 3 about C meets the line y = SY:

    x(theta) = cos(theta) + sqrt(9 - (SY - sin(theta))^2)

Blender's IK cannot translate a bone, so before the stretch bone existed
the slider stayed put and the rod merely aimed at the crank pin (live
actuator.sldasm, 2026-09-14). The tree here is the exporter's: crank and
slider both hang on the ground, the rod hangs on the slider, the cut is the
crank pin, the crank drives.
"""

import json
import math
import os
import sys
import tempfile

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from STEPper_NEXT.rig import graph, inputs, manifest as man_mod, rig_build  # noqa: E402


def _t(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


SX = 1.0 + 3.0 * math.cos(math.radians(30))
SY = 3.0 * math.sin(math.radians(30))


MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.SwToBlender", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "stretch-smoke.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "ground-1", "step_name": "ground",
         "step_occurrence_path": None, "transform": _t(0, 0, 0)},
        {"id": "c002", "sw_path": "crank-1", "step_name": "crank",
         "step_occurrence_path": None, "transform": _t(0, 0, 0)},
        {"id": "c003", "sw_path": "slider-1", "step_name": "slider",
         "step_occurrence_path": None, "transform": _t(4, 0, 0)},
        {"id": "c004", "sw_path": "rod-1", "step_name": "rod",
         "step_occurrence_path": None, "transform": _t(SX, SY, 0)},
        {"id": "c005", "sw_path": "rod-2", "step_name": "rod",
         "step_occurrence_path": None, "transform": _t(SX, SY, 0.1)},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "ground", "components": ["c001"],
         "grounded": True, "frame": None, "bbox_diag": 6.0},
        {"id": "g001", "name": "crank", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 1.0},
        {"id": "g002", "name": "slider", "components": ["c003"],
         "grounded": False, "frame": None, "bbox_diag": 0.5},
        {"id": "g003", "name": "rod", "components": ["c004"],
         "grounded": False, "frame": None, "bbox_diag": 3.0},
        {"id": "g004", "name": "rod2", "components": ["c005"],
         "grounded": False, "frame": None, "bbox_diag": 3.0},
    ],
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [0, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
        {"id": "j002", "type": "prismatic", "parent_group": "g000",
         "child_group": "g002", "origin": [4, 0, 0], "axis": [1, 0, 0],
         "secondary_axis": [0, 0, 1], "limits": None},
        {"id": "j003", "type": "revolute", "parent_group": "g002",
         "child_group": "g003", "origin": [SX, SY, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
        {"id": "j004", "type": "revolute", "parent_group": "g001",
         "child_group": "g003", "origin": [1, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
        # A second, parallel rod on the other side of the slider: the same
        # loop again, closing through the same slide.
        {"id": "j005", "type": "revolute", "parent_group": "g002",
         "child_group": "g004", "origin": [SX, SY, 0.1], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
        {"id": "j006", "type": "revolute", "parent_group": "g001",
         "child_group": "g004", "origin": [1, 0, 0.1], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
    ],
    "loops": [
        {"id": "loop001", "member_joints": ["j001", "j002", "j003", "j004"],
         "closure_joint": "j004", "closure_kind": "ik",
         "suggested_driver_joint": "j001", "planar": True,
         "plane_normal": [0, 0, 1],
         "driver_candidates": [
             {"joint": "j001", "closure_joint": "j004", "closure_kind": "ik"},
             {"joint": "j002", "closure_joint": "j003", "closure_kind": "ik"}]},
        {"id": "loop002", "member_joints": ["j001", "j002", "j005", "j006"],
         "closure_joint": "j006", "closure_kind": "ik",
         "suggested_driver_joint": "j001", "planar": True,
         "plane_normal": [0, 0, 1],
         "driver_candidates": [
             {"joint": "j001", "closure_joint": "j006", "closure_kind": "ik"},
             {"joint": "j002", "closure_joint": "j005", "closure_kind": "ik"}]},
    ],
    "warnings": [],
}


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

    plan = graph.build(m)
    slider_plan = plan.bone_by_group["g002"]
    assert slider_plan.slide_name, "the slide inside the chain got no stretch bone"
    assert "g002" in plan.slide_rest and plan.slide_rest["g002"] > 6.0, plan.slide_rest
    lplan = plan.loops[0]
    assert lplan.driven_chain == ["g003", "g002"], lplan.driven_chain
    assert lplan.chain_count == 3, lplan.chain_count   # rod, slider, stretch
    # The second loop through the same slide stops short of it: two IK
    # targets on one stretch bone and Blender stretches nothing.
    lplan2 = plan.loops[1]
    assert lplan2.driven_chain == ["g004"], lplan2.driven_chain
    assert lplan2.chain_count == 1, lplan2.chain_count

    result = rig_build.build(bpy.context, m, plan, None)
    arm = result.armature_object
    assert not result.warnings, result.warnings
    stretch = result.slide_names["g002"]
    pose = arm.pose
    spb = pose.bones[stretch]
    assert spb.ik_stretch == 1.0 and spb.lock_ik_x and spb.lock_ik_y and spb.lock_ik_z
    body = pose.bones[result.bone_names["g002"]]
    assert body.bone.use_connect and body.bone.inherit_scale == "NONE"
    assert body.bone.parent.name == stretch
    con = [c for c in pose.bones[result.effector_names["loop001"]].constraints
           if c.type == "IK"][0]
    assert con.use_stretch and con.chain_count == 4, (con.use_stretch, con.chain_count)
    con2 = [c for c in pose.bones[result.effector_names["loop002"]].constraints
            if c.type == "IK"][0]
    assert not con2.use_stretch and con2.chain_count == 2, (con2.use_stretch, con2.chain_count)
    assert body.lock_location == (True, True, True) or list(body.lock_location) == [True, True, True]
    # The stretch bone is scaffolding, hidden with the other helpers.
    assert stretch in [b.name for b in arm.data.collections["SW_helpers"].bones]

    crank = pose.bones[result.bone_names["g001"]]
    rest3 = arm.data.bones[crank.name].matrix_local.to_3x3()
    worst = 0.0
    for theta in (0.0, 0.3, 0.8, 1.5, -0.6, 2.4):
        crank.matrix_basis = (rest3.inverted() @ Matrix.Rotation(theta, 3, "Z")
                              @ rest3).to_4x4()
        bpy.context.view_layer.update()
        head = (arm.matrix_world @ body.matrix).translation
        pin_x = math.cos(theta) + math.sqrt(9.0 - (SY - math.sin(theta)) ** 2)
        expect = 4.0 + (pin_x - SX)      # the slider body carries the pin
        err = abs(head.x - expect)
        worst = max(worst, err, abs(head.y), abs(head.z))
        assert err < 1e-3, "theta %.2f: slider at x=%.4f, expected %.4f" % (
            theta, head.x, expect)
        assert abs(head.y) < 1e-6 and abs(head.z) < 1e-6, \
            "theta %.2f: slider left its line: %s" % (theta, tuple(head))
        for name in (result.bone_names["g002"], result.bone_names["g003"],
                     result.bone_names["g004"]):
            sc = pose.bones[name].matrix.to_scale()
            assert max(abs(sc.x - 1), abs(sc.y - 1), abs(sc.z - 1)) < 1e-6, \
                "theta %.2f: %s scaled %s" % (theta, name, tuple(sc))
        # Both rods meet the crank: the second one by turning on the pin
        # the first loop's slide put where it belongs.
        for lid in ("loop001", "loop002"):
            eff = pose.bones[result.effector_names[lid]]
            hlp = pose.bones[result.helper_names[lid]]
            tail = (arm.matrix_world @ eff.matrix
                    @ Matrix.Translation((0, eff.length, 0))).translation
            miss = (tail - (arm.matrix_world @ hlp.matrix).translation).length
            assert miss < 1e-4, "theta %.2f: %s closes %.2e m short" % (theta, lid, miss)
    # The other input: drive the SLIDER. Applying that candidate moves each
    # loop's cut to the pin between the slider and its rod, so the crank
    # and the rods are what gets solved, with rotations only and no
    # stretch bone at all. Pushing the slider so its pin sits at (x, SY)
    # must turn the crank to a theta with |C - S| = 3, C on the unit
    # circle:  x cos(theta) + SY sin(theta) = (x^2 + SY^2 - 8) / 2, which
    # has two roots either side of atan2(SY, x); the solver keeps the one
    # its motion reaches, so either is accepted.
    mech = inputs.mechanisms(m)
    assert mech == [["loop001", "loop002"]], mech
    assert inputs.candidates(m, mech[0]) == ["j001", "j002"]
    assert inputs.apply(m, mech[0], "j002") == ["loop001", "loop002"]
    plan2 = graph.build(m)
    assert not plan2.slide_rest, plan2.slide_rest
    assert plan2.loops[0].driven_chain == ["g003", "g001"], plan2.loops[0].driven_chain
    result2 = rig_build.build(bpy.context, m, plan2, None)
    assert not result2.warnings, result2.warnings
    arm2 = result2.armature_object
    slider = arm2.pose.bones[result2.bone_names["g002"]]
    assert list(slider.lock_location) == [True, False, True], \
        "the slider is the input now and must be posable along its slide"
    crank2 = arm2.pose.bones[result2.bone_names["g001"]]
    worst2 = 0.0
    # Within reach: the pin can sit at most 4 m from O, so x <= 3.708.
    for dx in (0.0, -0.5, -1.0, -1.5, 0.1):
        slider.location[1] = dx
        bpy.context.view_layer.update()
        x = SX + dx
        c = (x * x + SY * SY - 8.0) / 2.0
        mid = math.atan2(SY, x)
        half = math.acos(c / math.hypot(x, SY))
        roots = (mid - half, mid + half)
        # The crank rests along its pin (+Z); its rotation about that pin
        # is the angle its local X axis has turned in the plane.
        xaxis = (arm2.matrix_world @ crank2.matrix).to_3x3() @ Vector((1, 0, 0))
        rest_x = arm2.data.bones[crank2.name].matrix_local.to_3x3() @ Vector((1, 0, 0))
        turned = math.atan2(rest_x.x * xaxis.y - rest_x.y * xaxis.x,
                            rest_x.x * xaxis.x + rest_x.y * xaxis.y)
        err = min(abs(turned - r) for r in roots)
        worst2 = max(worst2, err)
        assert err < 2e-3, "slider at x=%.2f: crank turned %.4f rad, expected %s" % (
            x, turned, tuple(round(r, 4) for r in roots))
    slider.location[1] = 0.0

    print("rig_stretch_smoke: OK: the crank drives the slider through the "
          "rod, worst error %.2e m over six crank angles, nothing scaled, "
          "the parallel second rod closes on the same slide, and driven "
          "from the slider instead the crank follows to %.1e rad"
          % (worst, worst2))


main()
