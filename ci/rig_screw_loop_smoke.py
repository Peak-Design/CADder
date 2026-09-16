# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a loop closed on a body that SPINS.

    blender -b --factory-startup -P ci/rig_screw_loop_smoke.py

A closure re-joins the two bodies AT the closure joint's origin, so that
origin has to be a point that stands still when each body moves on its own
joint. A rotation axis is a line and any point on it does for the joint
itself, so the exporter is free to slide the origin along it. Slid clear of
the OTHER body's own axis, the closure point orbits that axis.

Live wrench.sldasm (2026-09-16, Oscar): "everything rotates with the screw
but it shouldn't". The centerlink rests on a point on the end of the screw,
on the screw's own axis, so it stands still while the screw turns. The
origin had been slid 8.5 mm down the centerlink's edge, and half a turn of
the screw left the centerlink 12 mm adrift of the screw it rests on.

The numbers here are that assembly: vice grips, a 1/4 inch screw at one
turn per inch, and a toggle resting at 97 per cent of its own reach.
"""

import copy
import json
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, manifest as man_mod, rig_build  # noqa: E402

# Where the closure really is: on the screw's own axis (y = 0, z = 0).
SEATED = [0.019335655874822597, 0.0, 0.0]
# Where a slide along the closure's own axis had put it.
ADRIFT = [0.019335655874822597, 0.0, -0.00851975531100343]

Z = [0.0, 0.0, 1.0]


def pin(jid, parent, child, origin):
    return {"id": jid, "type": "revolute", "parent_group": parent,
            "child_group": child, "origin": origin, "axis": Z,
            "secondary_axis": [1.0, 0.0, 0.0], "limits": None, "coupling": None}


def manifest(closure_origin):
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
            pin("j001", "g000", "g003", [0.08537448, 0.00127, 0.0]),
            # The screw: it advances 25.4 mm per turn and SPINS as it goes.
            {"id": "j002", "type": "screw", "parent_group": "g000",
             "child_group": "g004", "origin": [0.00508, 0.0, 0.0],
             "axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 0.0, 1.0],
             "limits": None,
             "coupling": {"kind": "screw", "driver_joint": None,
                          "lead_m_per_rev": 0.0254}},
            pin("j003", "g001", "g002", [0.06403553461456069, 0.023316006043544453, 0.0]),
            pin("j004", "g003", "g001", [0.08410264671205457, 0.023411716931482175, 0.0]),
            pin("j005", "g004", "g002", list(closure_origin)),
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
        raise SystemExit("rig_screw_loop_smoke: FAIL: " + msg)


def drift(closure_origin):
    """The worst the closure comes apart over a turn and a half of screw."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json", delete=False) as fh:
        json.dump(manifest(closure_origin), fh)
        path = fh.name
    try:
        m = man_mod.load(path)
    finally:
        os.unlink(path)

    plan = graph.build(m)
    result = rig_build.build(bpy.context, m, plan)
    arm = result.armature_object
    screw = arm.pose.bones[result.bone_names["g004"]]
    helper = arm.pose.bones[result.helper_names["loop001"]]
    effector = arm.pose.bones[result.effector_names["loop001"]]

    worst, spun = 0.0, 0.0
    for step in range(11):
        screw.location[1] = 0.001 * step
        bpy.context.view_layer.update()
        worst = max(worst, (effector.tail - helper.head).length)
        spun = max(spun, abs(screw.matrix.to_euler().y + 1.5708))
    return worst, spun


adrift, spun = drift(ADRIFT)
seated, _ = drift(SEATED)
print("rig_screw_loop_smoke: %.3f rad of screw: closure off the screw's axis "
      "comes apart by %.4f m, seated on it by %.6f m" % (spun, adrift, seated))

# The screw really does turn: without the turn the fault cannot show and
# the test proves nothing.
_check(spun > 1.5, "the screw barely turned (%.3f rad): check the lead" % spun)
_check(adrift > 5e-3,
       "a closure 8.5 mm off the screw's axis stayed put (%.6f m), so this "
       "no longer reproduces the fault it guards" % adrift)
_check(seated < 1e-4,
       "a closure seated on the screw's axis still came apart by %.6f m" % seated)

print("rig_screw_loop_smoke: OK: a closure seated on the spinning body's own "
      "axis holds while it turns, and one slid off it does not")
