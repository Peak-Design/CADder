# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a ram half on a ball mount aims freely at the other half.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_ball_mount_smoke.py

A ram half on a hinge can only turn about the hinge pin, so it rests with
local Z on the pin and aims with a Locked Track about it (sliders.py). A
half on a ball has no pin. A limited ball carries an axis, but that axis
is its swing cone's, and a half locked to turn about it rested off the
ram's line and could not reach a pivot that left the plane square to it.
It keeps the Damped Track instead, and rests along the ram.
"""

import math
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, manifest as man_mod, rig_build  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("   FAIL:", msg)
    return cond


def ident4():
    return [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def manifest():
    """A hydraulic ram working a clamp. The clamp pivots at the origin, the
    barrel at (1, 0, 0), and the rod's end sits on a limited ball at
    (0, 1, 0) whose cone axis is +X, across the ram."""
    def comp(cid, name):
        return {"id": cid, "sw_path": name + "-1", "step_name": name,
                "step_occurrence_path": None, "transform": ident4()}

    def rev(jid, parent, child, origin):
        return {"id": jid, "type": "revolute", "parent_group": parent,
                "child_group": child, "origin": origin, "axis": [0, 0, 1],
                "secondary_axis": [1, 0, 0], "limits": None}

    root2 = math.sqrt(2.0)
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "ram.step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [comp("c001", "body"), comp("c002", "clamp"),
                       comp("c003", "barrel"), comp("c004", "rod")],
        "rigid_groups": [
            {"id": "g000", "name": "body", "components": ["c001"],
             "grounded": True, "frame": ident4(), "bbox_diag": 1.0},
            {"id": "g001", "name": "clamp", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": 0.4},
            {"id": "g002", "name": "barrel", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.4},
            {"id": "g003", "name": "rod", "components": ["c004"],
             "grounded": False, "frame": None, "bbox_diag": 0.3},
        ],
        "joints": [
            rev("j001", "g000", "g001", [0.0, 0.0, 0.0]),
            rev("j002", "g000", "g002", [1.0, 0.0, 0.0]),
            {"id": "j003", "type": "prismatic",
             "parent_group": "g002", "child_group": "g003",
             "origin": [0.5, 0.5, 0.0],
             "axis": [-1.0 / root2, 1.0 / root2, 0.0],
             "secondary_axis": [0.0, 0.0, 1.0], "limits": None},
            {"id": "j004", "type": "ball", "parent_group": "g001",
             "child_group": "g003", "origin": [0.0, 1.0, 0.0],
             "axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 1.0, 0.0],
             "limits": {"rotation": {"min": 0.0, "max": 0.5,
                                     "value_at_rest": 0.0},
                        "translation": None}},
        ],
        "loops": [{
            "id": "L1",
            "member_joints": ["j001", "j002", "j003", "j004"],
            "closure_joint": "j003", "closure_kind": "aim_pair",
            "suggested_driver_joint": "j001",
            "planar": True, "plane_normal": [0.0, 0.0, 1.0],
        }],
        "warnings": [],
    }


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    m = man_mod.parse(manifest())
    plan = graph.build(m)
    check(len(plan.sliders) == 1, "the ram is not an aim pair")
    result = rig_build.build(bpy.context, m, plan)
    arm = result.armature_object

    barrel = arm.pose.bones[result.bone_names["g002"]]
    rod = arm.pose.bones[result.bone_names["g003"]]
    tracks = {pb.name: [c.type for c in pb.constraints
                        if c.name.startswith("CADLink Aim ")]
              for pb in (barrel, rod)}
    check(tracks[barrel.name] == ["LOCKED_TRACK"],
          "the barrel on its hinge aims with %s" % tracks[barrel.name])
    check(tracks[rod.name] == ["DAMPED_TRACK"],
          "the rod on its ball aims with %s" % tracks[rod.name])

    # At rest the rod lies along the ram, toward the barrel's pivot.
    along = Vector((1.0, -1.0, 0.0)).normalized()
    for name in {rod.name, result.ball_ctrl_names.get("g003", rod.name)}:
        y = arm.data.bones[name].matrix_local.col[1].to_3d().normalized()
        check(y.angle(along) < 1e-4,
              "%s rests along %s, not along the ram" % (name, tuple(y)))

    print()
    if FAILS:
        print("rig_ball_mount_smoke: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("rig_ball_mount_smoke: OK")


main()
