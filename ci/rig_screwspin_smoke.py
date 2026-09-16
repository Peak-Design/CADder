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

The rig answers by putting the turn on a bone of its OWN, a child of the
body's bone (graph.py BonePlan.spin_name). The geometry rides that child,
so the screw still turns on screen, and a body pinned across the axis
parents to the body's bone like any other child and takes the slide alone.

The same bone fixes a second fault. A driver that reads the slide off the
bone it writes is a depsgraph cycle: Blender reports it and then breaks it
with the last evaluation's value, so the turn lagged the slide by a whole
step and stayed lagged once the user let go (14.2 degrees out on this
mechanism, and the loop it closes 2.2 mm open). A child reading its parent
is an ordinary dependency. So this measures the turn against the lead as
well as the linkage against its plane.
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
_check(plan.bone_by_group["g004"].spin_name != "",
       "the screw got no bone to turn on, so its own bone must be turning")
_check(not plan.bone_by_group["g002"].parent_spin,
       "the centerlink is pinned to the screw about another axis and was "
       "still put on the bone that turns")

result = rig_build.build(bpy.context, m, plan)
arm = result.armature_object
_check(result.spin_names.get("g004"), "the turning bone was never built")
screw = arm.pose.bones[result.bone_names["g004"]]
spinner = arm.pose.bones[result.spin_names["g004"]]
_check(spinner.parent is not None and spinner.parent.name == screw.name,
       "the turning bone hangs off %s rather than off the screw's own bone"
       % (spinner.parent.name if spinner.parent else "nothing"))

LEAD = 0.0254


def turned():
    """The screw's turn about its axis, off the evaluated pose."""
    dg = bpy.context.evaluated_depsgraph_get()
    pb = arm.evaluated_get(dg).pose.bones[result.spin_names["g004"]]
    return (pb.bone.matrix_local.inverted() @ pb.matrix).to_euler().y


worst_lean = 0.0
worst_lag = 0.0
spun = 0.0
slid = 0.0
# Out and back: a lagging driver reads right on the way out and wrong the
# moment the slide turns round, which is what a user doing this by hand
# sees first.
for slide in ([0.001 * s for s in range(11)]
              + [0.001 * s for s in range(9, -1, -1)]):
    screw.location[1] = slide
    bpy.context.view_layer.update()
    want = slide * 2.0 * math.pi / LEAD
    worst_lag = max(worst_lag, abs(math.degrees(
        ((turned() - want + math.pi) % (2.0 * math.pi)) - math.pi)))
    spun = max(spun, abs(turned()))
    slid = max(slid, (spinner.head - screw.head).length)
    for gid in ("g001", "g002"):
        y = arm.pose.bones[result.bone_names[gid]].matrix.to_3x3() @ Vector(
            (0.0, 1.0, 0.0))
        worst_lean = max(worst_lean, math.degrees(y.angle(Vector(Z))))

print("rig_screwspin_smoke: %.3f rad of screw leaves the linkage %.4f deg "
      "out of its plane; the turning bone held the axis to %.7f m; the turn "
      "is within %.4f deg of the lead" % (spun, worst_lean, slid, worst_lag))

# Without a real turn the fault cannot show and the test proves nothing.
_check(spun > 1.5, "the screw barely turned (%.3f rad): check the lead" % spun)
_check(slid < 1e-6,
       "the turning bone sat %.6f m off the screw's own head, so it is not "
       "on the same axis" % slid)
_check(worst_lean < 0.01,
       "the linkage leaned %.4f deg out of a loop that is flat by "
       "construction: the screw's spin is still being inherited" % worst_lean)
_check(worst_lag < 0.01,
       "the screw's turn was out by %.4f deg against its own lead, so the "
       "driver is reading a pose it is itself deciding" % worst_lag)

print("rig_screwspin_smoke: OK: the screw turns by its own lead, on a bone "
      "of its own, and the bodies pinned across its axis stay in the plane "
      "of the mechanism")
