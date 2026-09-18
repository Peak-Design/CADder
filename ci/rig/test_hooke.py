# SPDX-License-Identifier: GPL-3.0-or-later
"""A universal joint built from its parts is driven exactly, not by IK.

The fixtures are two live exports of universal joint 2 (two yokes, a
spider and three pins): one with the pins locked into the spider
(2026-09-18) and one with the pins free in their bores (2026-09-16). IK
could not close either, and the output yoke flipped as the input turned.

The kinematic check here does not use hooke.py's own solve. It rebuilds
each body's turn from the tables the way the rig does, down the tree, and
measures whether the loop's cut joint still holds.
"""

import copy
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import graph, hooke, manifest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LOCKED = os.path.join(HERE, "fixtures", "universal_joint_2_pins_locked.rig.json")
FREE = os.path.join(HERE, "fixtures", "universal_joint_2_pins_free.rig.json")


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _rot(axis, angle):
    """3x3 rotation, rows, about a unit axis."""
    x, y, z = axis
    c, s, t = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]


def _mul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)]
            for r in range(3)]


def _apply(m, v):
    return [sum(m[r][k] * v[k] for k in range(3)) for r in range(3)]


def _angle(a, b):
    d = sum(x * y for x, y in zip(_unit(a), _unit(b)))
    return math.degrees(math.acos(max(-1.0, min(1.0, d))))


def _table(samples, x):
    """Linear, periodic over one turn: what the driver F-curve does."""
    x %= 2 * math.pi
    for (x0, y0), (x1, y1) in zip(samples, samples[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return samples[-1][1]


def _poses(m, loop, theta):
    """World turn of every body on the loop's tree path, for an input turn
    theta, built from the couplings the way the rig evaluates them."""
    joints = m.joint_by_id()
    driver = joints[loop.suggested_driver_joint]
    tree = [joints[j] for j in loop.member_joints if j != loop.closure_joint]
    turn = {g.id: [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
            for g in m.rigid_groups if g.grounded}
    pending = list(tree)
    while pending:
        for j in list(pending):
            if j.parent_group not in turn:
                continue
            if j is driver:
                angle = theta
            else:
                assert j.coupling is not None and j.coupling.kind == "table", j.id
                assert j.coupling.driver_joint == driver.id, j.id
                angle = _table(j.coupling.samples, theta)
            turn[j.child_group] = _mul(turn[j.parent_group],
                                       _rot(_unit(j.axis), angle))
            pending.remove(j)
    return turn


def _closure_error(m, loop, theta):
    """How far the cut joint's axis on its two bodies has come apart."""
    cut = m.joint_by_id()[loop.closure_joint]
    turn = _poses(m, loop, theta)
    axis = _unit(cut.axis)
    return _angle(_apply(turn[cut.parent_group], axis),
                  _apply(turn[cut.child_group], axis))


def _hooke_loop(m):
    for lp in m.loops:
        if len(lp.member_joints) == 4 and lp.closure_kind == "none":
            return lp
    raise AssertionError("no universal joint loop")


class HookeTests(unittest.TestCase):

    def test_pins_locked_is_driven_exactly(self):
        m = manifest.load(LOCKED)
        plan = graph.build(m)
        self.assertEqual(len(plan.hooke_notes), 1)
        self.assertEqual(plan.loops, [], "the loop must get no IK")
        loop = _hooke_loop(m)
        worst = max(_closure_error(m, loop, math.radians(d * 0.25))
                    for d in range(-1440, 1441))
        self.assertLess(worst, 1e-3, "the cut joint came apart by %.4f deg" % worst)

    def test_output_speed_is_a_universal_joint(self):
        """The output turns once per input turn, faster and slower within
        it by exactly the bend: between cos(bend) and 1/cos(bend)."""
        m = manifest.load(LOCKED)
        graph.build(m)
        joints = m.joint_by_id()
        driven = [j for j in m.joints if j.coupling is not None
                  and m.group_by_id()[j.parent_group].grounded][0]
        driver = joints[driven.coupling.driver_joint]
        bend = math.acos(abs(sum(a * b for a, b in zip(
            _unit(driver.axis), _unit(driven.axis)))))
        s = driven.coupling.samples
        self.assertAlmostEqual(s[-1][1] - s[0][1], 2 * math.pi, places=6)
        ratios = [(y1 - y0) / (x1 - x0) for (x0, y0), (x1, y1) in zip(s, s[1:])]
        self.assertGreater(min(ratios), math.cos(bend) - 1e-3)
        self.assertLess(max(ratios), 1 / math.cos(bend) + 1e-3)

    def test_pins_free_ride_along(self):
        """With the pins free each pin closes a loop of its own. Those loops
        need no solver: IK there turned the spider off the exact drive."""
        m = manifest.load(FREE)
        plan = graph.build(m)
        self.assertEqual(len(plan.hooke_notes), 3)
        self.assertEqual(plan.loops, [], "no loop may get IK")
        loop = _hooke_loop(m)
        worst = max(_closure_error(m, loop, math.radians(d))
                    for d in range(-360, 361))
        self.assertLess(worst, 1e-3)

    def test_either_yoke_can_be_the_input(self):
        m = manifest.load(LOCKED)
        graph.build(m)
        loop = _hooke_loop(m)
        shafts = [j for j in loop.member_joints
                  if m.group_by_id()[m.joint_by_id()[j].parent_group].grounded]
        other = [j for j in shafts if j != loop.suggested_driver_joint][0]
        loop.suggested_driver_joint = other
        graph.build(m)
        self.assertIsNone(m.joint_by_id()[other].coupling,
                          "the new input must not keep the old drive")
        worst = max(_closure_error(m, loop, math.radians(d))
                    for d in range(0, 361))
        self.assertLess(worst, 1e-3)

    def test_building_twice_changes_nothing(self):
        m = manifest.load(LOCKED)
        graph.build(m)
        first = {j.id: copy.deepcopy(j.coupling) for j in m.joints}
        graph.build(m)
        self.assertEqual(first, {j.id: j.coupling for j in m.joints})

    def test_axes_that_do_not_meet_are_left_alone(self):
        m = manifest.load(LOCKED)
        loop = m.loops[0]
        shaft = [m.joint_by_id()[j] for j in loop.member_joints
                 if m.group_by_id()[m.joint_by_id()[j].parent_group].grounded][0]
        # Move one shaft 20 mm sideways: a four-bar, not a universal joint.
        side = _unit([shaft.axis[1], -shaft.axis[0], 0.0]
                     if abs(shaft.axis[2]) < 0.9 else [1.0, 0.0, 0.0])
        shaft.origin = [o + 0.02 * s for o, s in zip(shaft.origin, side)]
        self.assertEqual(hooke.recognise(m), [])
        self.assertEqual(loop.closure_kind, "ik")


if __name__ == "__main__":
    unittest.main()
