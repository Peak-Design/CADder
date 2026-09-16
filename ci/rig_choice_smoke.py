# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the panel's driver choice: load a manifest through
the operator, find the mechanism's dropdown filled, pick the other input,
and see the standing rig rebuilt with the choice, remembered on the
armature, and re-applied when the manifest is loaded again.

    blender -b --factory-startup -P ci/rig_choice_smoke.py

The mechanism is rig_stretch_smoke's slider-crank with its parallel second
rod: two loops, one mechanism, two candidate inputs (crank or slider).
"""

import json
import os
import sys
import tempfile

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from CADder import rig  # noqa: E402
from CADder.rig import inputs, ui  # noqa: E402


def _manifest():
    src = open(os.path.join(_HERE, "rig_stretch_smoke.py"), encoding="utf-8").read()
    src = src.replace("\nmain()\n", "\n")
    ns = {"__file__": os.path.join(_HERE, "rig_stretch_smoke.py"), "__name__": "smoke_manifest"}
    exec(compile(src, "rig_stretch_smoke.py", "exec"), ns)
    return ns["MANIFEST"]


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json", delete=False) as fh:
        json.dump(_manifest(), fh)
        path = fh.name
    try:
        scene = bpy.context.scene
        scene.cad_link.manifest_path = path
        assert "FINISHED" in bpy.ops.cadlink.load_manifest()
        entries = scene.cad_link.mechanisms
        assert len(entries) == 1, len(entries)
        entry = entries[0]
        items = [i[0] for i in ui._driver_items(entry, bpy.context)]
        assert items == ["j001", "j002"], items
        assert entry.driver == "j001", entry.driver
        labels = [i[1] for i in ui._driver_items(entry, bpy.context)]
        assert labels == ["crank - hinge on ground", "slider - slide on ground"], labels

        assert "FINISHED" in bpy.ops.cadlink.build_rig()
        arm = ui._find_rig(bpy.context)
        assert arm is not None
        m = ui._STATE["manifest"]
        crank_bone = ui._STATE["build"].bone_names["g001"]
        slider_bone = ui._STATE["build"].bone_names["g002"]
        assert list(arm.pose.bones[crank_bone].lock_rotation) == [True, False, True]
        assert list(arm.pose.bones[slider_bone].lock_location) == [True, True, True], \
            "the slider is solved, not posable, while the crank drives"

        # Geometry riding the crank, and the crank posed: the rebuild must
        # start from the rest pose, or the posed position becomes the new
        # rig's zero (live cam-follower, 2026-09-15: a cam left at 90
        # degrees put every follower out of time with it).
        bpy.ops.mesh.primitive_cube_add(size=0.01)
        cube = bpy.context.active_object
        cube.parent = arm
        cube.parent_type = "BONE"
        cube.parent_bone = crank_bone
        bpy.context.view_layer.update()
        cube.matrix_parent_inverse = (arm.matrix_world @ arm.pose.bones[crank_bone].matrix).inverted()
        bpy.context.view_layer.update()
        at_rest = cube.matrix_world.copy()
        arm.pose.bones[crank_bone].rotation_mode = "XYZ"
        arm.pose.bones[crank_bone].rotation_euler = (0.0, 1.0, 0.0)
        bpy.context.view_layer.update()
        moved = max(abs(a - b) for ra, rb in zip(cube.matrix_world, at_rest) for a, b in zip(ra, rb))
        assert moved > 1e-3, "posing the crank did not move its geometry"

        # The choice: setting the dropdown rebuilds the standing rig.
        entry.driver = "j002"
        bpy.context.view_layer.update()
        drift = max(abs(a - b) for ra, rb in zip(cube.matrix_world, at_rest) for a, b in zip(ra, rb))
        assert drift < 1e-6, "geometry kept the old pose through the rebuild: drift %g" % drift
        assert inputs.current(m, ["loop001", "loop002"]) == "j002"
        arm = ui._find_rig(bpy.context)
        build = ui._STATE["build"]
        assert build is not None and build.armature_object is arm
        slider_bone = build.bone_names["g002"]
        assert list(arm.pose.bones[slider_bone].lock_location) == [True, False, True], \
            "the slider is the input now and must slide by hand"
        assert not build.slide_names, "no stretch bone when the slider drives"
        stored = json.loads(arm.get("RIG_driver_choice"))
        assert stored == ["ground-1|slider-1|prismatic"], stored

        # Loading the manifest again (a re-send) keeps the choice.
        assert "FINISHED" in bpy.ops.cadlink.load_manifest()
        entry = scene.cad_link.mechanisms[0]
        assert entry.driver == "j002", entry.driver
        m2 = ui._STATE["manifest"]
        assert inputs.current(m2, ["loop001", "loop002"]) == "j002"
        assert {lp.closure_joint for lp in m2.loops} == {"j003", "j005"}

        # And back to the crank, which restores the exporter's cuts.
        entry.driver = "j001"
        m3 = ui._STATE["manifest"]
        assert {lp.closure_joint for lp in m3.loops} == {"j004", "j006"}
        assert ui._STATE["build"].slide_names, "the stretch bone is back with the crank driving"
    finally:
        os.unlink(path)
    print("rig_choice_smoke: OK: one mechanism, two labelled inputs, the "
          "rig rebuilt on choice, the choice kept across a reload")


main()
