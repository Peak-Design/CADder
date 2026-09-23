# SPDX-License-Identifier: GPL-3.0-or-later
"""The inputs a mechanism offers do not change when the rig is planned.
No bpy.

graph.build turns a universal joint into table couplings on the loop's
current input (hooke.py). Those couplings are the rig's own drive, not a
relation the CAD application sent, so they must not hide the other yoke:
after a send the dropdown offered only the yoke that was already the
input, and the user could not drive the joint from the other shaft."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import graph, inputs, manifest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LOCKED = os.path.join(HERE, "fixtures", "universal_joint_2_pins_locked.rig.json")


class TestUniversalJointInputs(unittest.TestCase):

    def test_both_yokes_are_offered_after_a_build(self):
        m = manifest.load(LOCKED)
        loops = inputs.mechanisms(m)[0]
        before = inputs.candidates(m, loops)
        self.assertEqual(len(before), 2)
        graph.build(m)
        self.assertEqual(inputs.candidates(m, loops), before)

    def test_the_other_yoke_can_be_taken_and_given_back(self):
        m = manifest.load(LOCKED)
        loops = inputs.mechanisms(m)[0]
        first, second = inputs.candidates(m, loops)
        graph.build(m)
        self.assertTrue(inputs.apply(m, loops, second))
        graph.build(m)
        self.assertEqual(inputs.current(m, loops), second)
        self.assertEqual(inputs.candidates(m, loops), [first, second])
        self.assertTrue(inputs.apply(m, loops, first))
        graph.build(m)
        self.assertEqual(inputs.current(m, loops), first)


if __name__ == "__main__":
    unittest.main()
