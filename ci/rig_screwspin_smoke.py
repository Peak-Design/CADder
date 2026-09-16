# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a screw's spin must stop at the bodies pinned to it.

    blender -b --factory-startup -P ci/rig_screwspin_smoke.py

A screw SPINS as it advances, and that spin is driven by its own slide: it
happens whether or not anything asks for it. A body pinned to the screw
about some OTHER axis cannot turn back out of that spin, so inheriting it
tips the body out of the plane of the mechanism for good.

Live wrench.sldasm (2026-09-16, Oscar): "rotating the clamp2 bone does
produce correct movement ... the problem is that everything rotates with
the screw but it shouldn't". Driven from the jaw, the centerlink hangs off
the screw in the bone tree, pinned about the mechanism normal while the
screw turns about its own axis at right angles to it. Ten millimetres of
screw leaned the centerlink 15 degrees and arm2 62 degrees out of a loop
that is flat by construction.

The rig answers with a hidden carrier (graph.py BonePlan.nospin_name) that
takes the screw's slide and none of its turn. The screw's own bone still
spins, and its geometry with it.
"""

import json
import math
import os
import sys
import tempfile

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, manifest as man_mod, rig_build  # noqa: E402

Z = [0.0, 0.0, 1.0]


def pin(jid, parent, child, origin):
    return {"id": jid, "type": "revolute", "parent_group": parent,
            "child_group": child, "origin": list(origin), "axis": Z,
            "secondary_axis": [1.0, 0.0, 0.0], "limits": None, "coupling": None}


def manifest():
    """The wrench, driven from the jaw: the cut moves to the jaw's own pin
    on arm2, which leaves the centerlink hanging off the screw."""
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
            {"id": "j002", "type": "screw", "parent_group": "g000",
             "child_group": "g004", "origin": [0.00508, 0.0, 0.0],
             "axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 0.0, 1.0],
             "limits": None,
             "coupling": {"kind": "screw", "driver_joint": None,
                          "lead_m_per_rev": 0.0254}},
            pin("j003", "g002", "g001",
                [0.06403553461456069, 0.023316006043544453, 0.0]),
            pin("j004", "g003", "g001",
                [0.08410264671205457, 0.023411716931482175, 0.0]),
            pin("j005", "g004", "g002", [0.019335655874822597, 0.0, 0.0]),
        ],
        "loops": [{
            "id": "loop001",
            "member_joints": ["j001", "j002", "j003", "j004", "j005"],
            "closure_joint": "j004", "closure_kind": "ik",
            "suggested_driver_joint": "j001",
            "mobility": 2, "planar": True, "plane_normal": Z,
            "driver_candidates": [],
        }],
        "warnings": [],
    }


def _check(cond, msg):
    if not cond:
        raise SystemExit("rig_screwspin_smoke: FAIL: " + msg)


bpy.ops.wm.read_factory_settings(use_empty=True)
with tempfile.NamedTemporaryFile("w", suffix=".rig.json", delete=False) as fh:
    json.dump(manifest(), fh)
    PATH = fh.name
try:
    m = man_mod.load(PATH)
finally:
    os.unlink(PATH)

plan = graph.build(m)
_check(plan.bone_by_group["g004"].nospin_name != "",
       "the screw got no carrier, so whatever hangs off it takes its spin")
_check(plan.bone_by_group["g002"].parent_nospin,
       "the centerlink is pinned to the screw about another axis and still "
       "rides the screw's own bone")
_check(not plan.bone_by_group["g004"].parent_nospin,
       "the screw itself was given a carrier to ride")

result = rig_build.build(bpy.context, m, plan)
arm = result.armature_object
_check(result.nospin_names.get("g004"), "the carrier bone was never built")
screw = arm.pose.bones[result.bone_names["g004"]]
carrier = arm.pose.bones[result.nospin_names["g004"]]

worst_lean = 0.0
spun = 0.0
slid = 0.0
for step in range(11):
    screw.location[1] = 0.001 * step
    bpy.context.view_layer.update()
    spun = max(spun, abs(screw.matrix.to_euler().y + math.pi / 2.0))
    slid = max(slid, (carrier.head - screw.head).length)
    for gid in ("g001", "g002"):
        y = arm.pose.bones[result.bone_names[gid]].matrix.to_3x3() @ Vector(
            (0.0, 1.0, 0.0))
        worst_lean = max(worst_lean, math.degrees(y.angle(Vector(Z))))

print("rig_screwspin_smoke: %.3f rad of screw leaves the linkage %.4f deg "
      "out of its plane; the carrier tracked the screw to %.7f m"
      % (spun, worst_lean, slid))

# Without a real turn the fault cannot show and the test proves nothing.
_check(spun > 1.5, "the screw barely turned (%.3f rad): check the lead" % spun)
_check(slid < 1e-6,
       "the carrier lagged the screw's own bone by %.6f m, so it is not "
       "carrying the slide" % slid)
_check(worst_lean < 0.01,
       "the linkage leaned %.4f deg out of a loop that is flat by "
       "construction: the screw's spin is still being inherited" % worst_lean)

print("rig_screwspin_smoke: OK: the screw turns, its geometry turns with it, "
       "and the bodies pinned to it stay in the plane of the mechanism")
