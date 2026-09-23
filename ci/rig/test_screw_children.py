# SPDX-License-Identifier: GPL-3.0-or-later
"""Children of a screw body that have no axis. No bpy.

A ball with no cone limit and a free joint arrive with axis null. The
planner must not stop on such a child of a screw: a vise spindle with a
swivel pad on a plain ball stopped the whole rig with an IndexError."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from CADder.rig import graph, manifest  # noqa: E402

from test_graph import screw_manifest  # noqa: E402


def axisless_child(kind):
    data = screw_manifest([0.0, 0.0, 1.0])
    child = data["joints"][1]
    child["type"] = kind
    child["axis"] = None
    child["secondary_axis"] = None
    return data


class TestAxislessScrewChild(unittest.TestCase):

    def test_a_plain_ball_on_a_screw_plans(self):
        plan = graph.build(manifest.parse(axisless_child("ball")))
        # No axis, so it cannot be coaxial with the screw: it stays off the
        # turn and takes the slide alone.
        self.assertFalse(plan.bone_by_group["g002"].parent_spin)
        self.assertTrue(plan.bone_by_group["g001"].spin_name)

    def test_a_free_joint_on_a_screw_plans(self):
        plan = graph.build(manifest.parse(axisless_child("free")))
        self.assertFalse(plan.bone_by_group["g002"].parent_spin)


if __name__ == "__main__":
    unittest.main()
