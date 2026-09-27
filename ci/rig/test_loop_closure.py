# SPDX-License-Identifier: GPL-3.0-or-later
"""How graph.build closes a loop: on the axis of the closure joint, and
with the right bone left to the user when the loop has a spare input.

    python -m pytest ci/rig/test_loop_closure.py

The corpus rigging test, 2026-09-27: a landing gear and a Cardan joint
made in Blender came apart, because the closure held only a point, and
locking pliers freed the jaw next to the frame, which left the screw about
a millimetre of travel near the locked pose.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import graph, manifest  # noqa: E402


def _t(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


def _joint(jid, jtype, parent, child, origin, axis=(0, 0, 1)):
    return {"id": jid, "type": jtype, "parent_group": parent,
            "child_group": child, "origin": list(origin), "axis": list(axis),
            "secondary_axis": [1, 0, 0] if abs(axis[0]) < 0.9 else [0, 1, 0],
            "limits": {"rotation": None, "translation": None},
            "coupling": None, "confidence": "high", "notes": jid}


def _manifest(n_groups, joints, loop):
    return manifest.parse({
        "manifest_version": "1.0.0",
        "generator": {"name": "test", "version": "0"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "x.step", "ap": "AP214", "sha1": None,
                        "occurrence_matching": None},
        "components": [{"id": "c%03d" % i, "sw_path": "p%d" % i,
                        "step_name": "p%d" % i, "transform": _t(0, 0, 0)}
                       for i in range(n_groups)],
        "rigid_groups": [{"id": "g%d" % i, "name": "g%d" % i,
                          "components": ["c%03d" % i], "grounded": i == 0,
                          "frame": None, "bbox_diag": 0.1}
                         for i in range(n_groups)],
        "joints": joints,
        "loops": [loop],
    }, source_path="test")


def _pliers(mobility=2):
    """A five-bar near its toggle: a jaw on the frame, a handle on the jaw,
    a link on the handle, and a slide on the frame. The handle pin, the
    link pin and the closure lie on one line."""
    joints = [
        _joint("j1", "revolute", "g0", "g1", (0.0, 0.0, 0.0)),
        _joint("j2", "revolute", "g1", "g2", (0.1, 0.05, 0.0)),
        _joint("j3", "revolute", "g2", "g3", (0.2, 0.05, 0.0)),
        _joint("j4", "prismatic", "g0", "g4", (0.4, 0.05, 0.0), axis=(1, 0, 0)),
        _joint("j5", "revolute", "g3", "g4", (0.3, 0.05, 0.0)),
    ]
    loop = {"id": "l1", "member_joints": ["j1", "j2", "j3", "j4", "j5"],
            "closure_joint": "j5", "closure_kind": "ik",
            "suggested_driver_joint": "j4", "planar": True,
            "plane_normal": [0, 0, 1], "mobility": mobility}
    return _manifest(5, joints, loop)


class SpareInput(unittest.TestCase):

    def test_the_bone_left_to_the_user_keeps_the_loop_closed(self):
        plan = graph.build(_pliers())
        (lp,) = plan.loops
        # The jaw next to the frame would leave the handle and the link in
        # line, and they could not move the closure. Another bone is held.
        self.assertNotIn("g1", lp.held)
        self.assertEqual(len(lp.held), 1)
        self.assertIn(lp.held[0], ("g2", "g3"))
        # the chain keeps its three bones: the held one only has its IK
        # locked
        self.assertEqual(lp.driven_chain, ["g3", "g2", "g1"])

    def test_a_loop_that_is_not_marked_planar_holds_the_same_bone(self):
        # The Rigging module left a loop with a ball unmarked: its pins are
        # parallel, so the hold is measured in their plane all the same.
        m = _pliers()
        m.loops[0].planar = False
        m.loops[0].plane_normal = None
        (lp,) = graph.build(m).loops
        self.assertNotIn("g1", lp.held)
        self.assertEqual(len(lp.held), 1)

    def test_a_chain_with_a_slide_gets_no_second_point(self):
        # two targets on a chain stop its stretch bone
        m = _spatial()
        m.joint_by_id()["j3"].type = "prismatic"      # the driven side
        (lp,) = graph.build(m).loops
        self.assertEqual(lp.driven_chain, ["g3"])
        self.assertEqual(lp.axis_arm, 0.0)

    def test_one_input_holds_no_bone(self):
        plan = graph.build(_pliers(mobility=1))
        self.assertEqual(plan.loops[0].held, [])


def _spatial(closure="revolute", planar=False):
    """A loop of four revolutes whose axes are not parallel."""
    joints = [
        _joint("j1", "revolute", "g0", "g1", (0.0, 0.0, 0.0), axis=(0, 0, 1)),
        _joint("j2", "revolute", "g1", "g2", (0.1, 0.0, 0.0), axis=(1, 0, 0)),
        _joint("j3", "revolute", "g0", "g3", (0.1, 0.2, 0.0), axis=(0, 1, 0)),
        _joint("j4", closure, "g2", "g3", (0.1, 0.1, 0.05), axis=(0, 0, 1)),
    ]
    loop = {"id": "l1", "member_joints": ["j1", "j2", "j3", "j4"],
            "closure_joint": "j4", "closure_kind": "ik",
            "suggested_driver_joint": "j1", "planar": planar}
    if planar:
        loop["plane_normal"] = [0, 0, 1]
    return _manifest(4, joints, loop)


class AxisClosure(unittest.TestCase):

    def test_a_spatial_loop_holds_the_axis_of_its_closure(self):
        plan = graph.build(_spatial())
        (lp,) = plan.loops
        self.assertGreater(lp.axis_arm, 0.0)
        self.assertTrue(lp.axis_helper_name.startswith("HLA_"))
        self.assertTrue(lp.axis_effector_name.startswith("EFA_"))

    def test_a_ball_closure_holds_its_point_only(self):
        (lp,) = graph.build(_spatial(closure="ball")).loops
        self.assertEqual(lp.axis_arm, 0.0)
        self.assertEqual(lp.axis_helper_name, "")

    def test_a_planar_loop_needs_no_second_point(self):
        (lp,) = graph.build(_pliers(mobility=1)).loops
        self.assertEqual(lp.axis_arm, 0.0)


if __name__ == "__main__":
    unittest.main()
