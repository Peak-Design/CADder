# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for collapsed contact rigs (tangent decomposition, SW-To-
Blender corpus 11): the component-less carrier chain must fold into ONE
posable puck bone: no carrier bone, a hidden center target riding the
base, a Limit Distance holding the tangency radius, and DRAGGING the puck
bone anywhere must land it back on the orbit circle at the exact radius.

Run:  blender -b --factory-startup -P rig_carrier_smoke.py
"""

import json
import math
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from STEPper_NEXT.rig import graph, manifest as man_mod, matching, rig_build  # noqa: E402


ORBIT_X, ORBIT_Y = 0.046612, 0.045303

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.SwToBlender", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "carrier-smoke.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "base-1", "step_name": "base",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
        {"id": "c002", "sw_path": "puck-1", "step_name": "puck",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, ORBIT_X], [0, 1, 0, ORBIT_Y],
                       [0, 0, 1, 0], [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.14},
        {"id": "g001", "name": "puck", "components": ["c002"], "grounded": False,
         "frame": None, "bbox_diag": 0.043},
        {"id": "g002", "name": "puck_carrier", "components": [], "grounded": False,
         "frame": None, "bbox_diag": 0.02},
    ],
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g002", "origin": [0, 0, 0.01], "axis": [0, 0, -1],
         "secondary_axis": [1, 0, 0], "limits": None},
        {"id": "j002", "type": "revolute", "parent_group": "g002",
         "child_group": "g001", "origin": [ORBIT_X, ORBIT_Y, 0],
         "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None},
    ],
    "loops": [],
    "warnings": [],
}


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json",
                                     delete=False) as f:
        json.dump(MANIFEST, f)
        path = f.name
    try:
        m = man_mod.load(path)
    finally:
        os.unlink(path)

    plan = graph.build(m)
    assert plan.collapsed_carriers == ["g002"], plan.collapsed_carriers
    res = rig_build.build(bpy.context, m, plan, matching.identity_frame())
    arm = res.armature_object
    pose = arm.pose

    base = pose.bones[res.bone_names["g000"]]
    puck = pose.bones[res.bone_names["g001"]]
    assert "g002" not in res.bone_names, "carrier must not get a bone"
    assert puck.parent.name == base.name, "puck must parent straight to base"

    # The hidden center target rides the base.
    target = pose.bones[res.tangent_helper_names["g001"]]
    assert target.parent.name == base.name
    assert target.name.startswith("TGT_")

    # One bone, natural channels: drag in the orbit plane, spin about own
    # axis. No lifting off, no tilting.
    assert list(puck.lock_location) == [False, True, False]
    assert list(puck.lock_rotation) == [True, False, True]
    cons = [c for c in puck.constraints if c.type == "LIMIT_DISTANCE"]
    assert len(cons) == 1 and cons[0].subtarget == target.name
    want = (ORBIT_X ** 2 + ORBIT_Y ** 2) ** 0.5
    assert abs(cons[0].distance - want) < 1e-9
    assert cons[0].limit_mode == "LIMITDIST_ONSURFACE"

    # DRAG the puck somewhere wild in its free channels. The constraint must
    # put it back on the circle around the center, radius exact.
    puck.location = (0.08, 0.0, -0.12)   # local X (in-plane), Z (in-plane)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    head = arm.evaluated_get(dg).pose.bones[puck.name].head
    centre = arm.evaluated_get(dg).pose.bones[target.name].head
    got = (head - centre).length
    assert abs(got - want) < 1e-6, (want, got)
    assert abs(head.z - centre.z) < 1e-6, "puck left the orbit plane"

    print("rig_carrier_smoke: OK: dragged puck held at radius %.6f on the "
          "orbit circle by one bone" % want)


main()
