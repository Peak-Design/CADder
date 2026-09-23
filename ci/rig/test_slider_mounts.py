# SPDX-License-Identifier: GPL-3.0-or-later
"""Which half of a slider-crank is locked to a pin. No bpy.

A half on a hinge can only turn about the hinge pin, so its aim is a
Locked Track about that pin. A half on a ball has no pin: the axis a
limited ball carries is its swing cone's axis, and locking a turn about
it holds the ram half in one plane that the mechanism need not keep."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from CADder.rig import graph, manifest, sliders  # noqa: E402

from test_manifest import ram_manifest  # noqa: E402


def ram_on(kind):
    """The ram, with the rod's end on a `kind` joint whose axis is not
    along the ram."""
    data = ram_manifest()
    rod_end = data["joints"][3]
    rod_end["type"] = kind
    rod_end["axis"] = [1.0, 0.0, 0.0]
    rod_end["secondary_axis"] = [0.0, 1.0, 0.0]
    if kind == "ball":
        rod_end["limits"] = {"rotation": {"min": 0.0, "max": 0.5,
                                          "value_at_rest": 0.0},
                             "translation": None}
    return graph.build(manifest.parse(data))


class TestPinnedHalves(unittest.TestCase):

    def test_a_hinge_half_is_pinned(self):
        plan = graph.build(manifest.parse(ram_manifest()))
        self.assertEqual(len(plan.sliders), 1)
        self.assertTrue(sliders._pinned(plan, "g002"))
        self.assertTrue(sliders._pinned(plan, "g003"))

    def test_a_limited_ball_half_is_not_pinned(self):
        plan = ram_on("ball")
        self.assertEqual(len(plan.sliders), 1)
        self.assertTrue(sliders._pinned(plan, "g002"))
        self.assertFalse(sliders._pinned(plan, "g003"))


if __name__ == "__main__":
    unittest.main()
