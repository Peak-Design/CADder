# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a chain must stay on the branch the assembly rests on.

    blender -b --factory-startup -P ci/rig_branch_smoke.py

Two links reaching one point have TWO answers, mirrored in the line
through the ends of the chain. Blender solves the chain from its REST pose
every time and takes whichever answer its iteration lands on, so a
mechanism driven far enough comes out on the other one and turns itself
inside out between one frame and the next.

Live wrench.sldasm (2026-09-16, Oscar): "at some point secondgrip and
centerlink instantly flip to the wrong position/direction ... as soon as
this triangle flips direction it starts giving problems". The elbow went
from -115.7 to +119.4 degrees between two neighbouring poses and the
closure tore open by 25 mm.

The rig answers with two things together: graph.py's _branch_limits stops
each elbow on the two poses where the branches meet, and loops.py's elbow
stiffness makes the solver walk to its answer along the mechanism rather
than cut across them. This measures the result against the answer the
cosine rule gives, over the whole workspace, and then takes both away to
prove the fault is still reproducible.

The numbers are that assembly: vice grips, a 1/4 inch screw at one turn
per inch, and a toggle resting at 97 per cent of its own reach.
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

J001 = [0.08537448, 0.00127, 0.0]                                   # arm1/clamp2
J003 = [0.06403553461456069, 0.023316006043544453, 0.0]             # arm2/centerlink
J004 = [0.08410264671205457, 0.023411716931482175, 0.0]             # clamp2/arm2
J005 = [0.019335655874822597, 0.0, 0.0]                             # screw/centerlink
Z = [0.0, 0.0, 1.0]


def pin(jid, parent, child, origin):
    return {"id": jid, "type": "revolute", "parent_group": parent,
            "child_group": child, "origin": list(origin), "axis": Z,
            "secondary_axis": [1.0, 0.0, 0.0], "limits": None, "coupling": None}


def manifest():
    names = ["arm1", "arm2", "centerlink", "clamp2", "screw"]
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "wrench.step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": "c%03d" % i, "sw_path": n + "-1", "step_name": n,
             "step_occurrence_path": None,
             "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}
            for i, n in enumerate(names, 1)
        ],
        "rigid_groups": [
            {"id": "g000", "name": "arm1", "components": ["c001"],
             "grounded": True,
             "frame": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
             "bbox_diag": 0.2},
        ] + [
            {"id": "g%03d" % i, "name": n, "components": ["c%03d" % (i + 1)],
             "grounded": False, "frame": None, "bbox_diag": 0.05}
            for i, n in enumerate(names[1:], 1)
        ],
        "joints": [
            pin("j001", "g000", "g003", J001),
            {"id": "j002", "type": "screw", "parent_group": "g000",
             "child_group": "g004", "origin": [0.00508, 0.0, 0.0],
             "axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 0.0, 1.0],
             "limits": None,
             "coupling": {"kind": "screw", "driver_joint": None,
                          "lead_m_per_rev": 0.0254}},
            pin("j003", "g001", "g002", J003),
            pin("j004", "g003", "g001", J004),
            pin("j005", "g004", "g002", J005),
        ],
        "loops": [{
            "id": "loop001",
            "member_joints": ["j001", "j002", "j003", "j004", "j005"],
            "closure_joint": "j005", "closure_kind": "ik",
            "suggested_driver_joint": "j002",
            "mobility": 2, "planar": True, "plane_normal": Z,
            "driver_candidates": [],
        }],
        "warnings": [],
    }


def _check(cond, msg):
    if not cond:
        raise SystemExit("rig_branch_smoke: FAIL: " + msg)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# The two links of the chain the solver bends: clamp2's pin to the
# centerlink's pin, and that to the pin on the screw.
L1 = _dist(J004, J003)
L2 = _dist(J003, J005)


def wanted(root, target):
    """The elbow angle the cosine rule gives, on the branch the assembly
    rests on, or None where the chain cannot reach at all."""
    span = (root.xy - target.xy).length
    if not (abs(L1 - L2) <= span <= L1 + L2):
        return None
    cos_inner = (L1 * L1 + L2 * L2 - span * span) / (2.0 * L1 * L2)
    inner = math.degrees(math.acos(max(-1.0, min(1.0, cos_inner))))
    return -(180.0 - inner)


def survey(loosen):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json", delete=False) as fh:
        json.dump(manifest(), fh)
        path = fh.name
    try:
        m = man_mod.load(path)
    finally:
        os.unlink(path)

    plan = graph.build(m)
    result = rig_build.build(bpy.context, m, plan)
    arm = result.armature_object
    jaw = arm.pose.bones[result.bone_names["g003"]]
    jaw.rotation_mode = "XYZ"
    screw = arm.pose.bones[result.bone_names["g004"]]
    helper = arm.pose.bones[result.helper_names["loop001"]]
    effector = arm.pose.bones[result.effector_names["loop001"]]
    arm2 = arm.pose.bones[result.bone_names["g001"]]
    link = arm.pose.bones[result.bone_names["g002"]]
    if loosen:
        for pb in (arm2, link):
            pb.use_ik_limit_y = False
            pb.ik_stiffness_y = 0.0

    seen = wrong = 0
    gap = 0.0
    for step in range(11):
        screw.location[1] = 0.001 * step
        for i in range(-60, 161):                  # -45 .. +120 degrees of jaw
            jaw.rotation_euler[1] = math.radians(i * 0.75)
            bpy.context.view_layer.update()
            want = wanted(arm2.head, helper.head)
            if want is None:
                continue                            # the mechanism binds here
            seen += 1
            u = link.head - arm2.head
            v = effector.tail - link.head
            got = math.degrees(u.xy.angle_signed(v.xy))
            if abs(((got - want + 180.0) % 360.0) - 180.0) > 1.0:
                wrong += 1
            else:
                gap = max(gap, (effector.tail - helper.head).length)
    return seen, wrong, gap


held, off_branch, gap = survey(loosen=False)
loose_seen, loose_off, _ = survey(loosen=True)

print("rig_branch_smoke: of %d poses the five-bar really has, the rig puts "
      "%d on the wrong branch (%d with the limit and the stiffness taken "
      "away), closure held to %.6f m" % (held, off_branch, loose_off, gap))

_check(held > 1500, "only %d poses surveyed: the sweep no longer covers the "
                    "workspace it claims" % held)
_check(loose_off > 20,
       "without the branch limit and the stiffness only %d poses came out on "
       "the wrong branch, so this no longer reproduces the fault it guards"
       % loose_off)
_check(off_branch == 0,
       "%d of %d poses came out on the mirror branch" % (off_branch, held))
_check(gap < 1e-4, "the closure opened by %.6f m on the branch it holds" % gap)

print("rig_branch_smoke: OK: the chain stays on the branch SolidWorks "
      "assembled it on, over the whole workspace")
