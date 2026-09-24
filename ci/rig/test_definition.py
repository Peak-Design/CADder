# SPDX-License-Identifier: GPL-3.0-or-later
"""Which joints a mechanism defines: the color of a control bone. Blue for
a joint in a closed chain, red for a joint that nothing else moves. No bpy.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import definition as D, manifest  # noqa: E402

from test_manifest import four_bar_manifest, hinge_manifest  # noqa: E402


def of(data):
    return D.of_manifest(manifest.parse(data))


class Bridges(unittest.TestCase):
    def test_a_chain_is_all_bridges_and_a_ring_has_none(self):
        chain = [("a", "b", 1), ("b", "c", 2)]
        self.assertEqual(D.bridges(chain), {1, 2})
        self.assertEqual(D.bridges(chain + [("c", "a", 3)]), set())

    def test_two_edges_between_two_nodes_are_a_ring(self):
        self.assertEqual(D.bridges([("a", "b", 1), ("a", "b", 2)]), set())

    def test_a_tail_on_a_ring(self):
        edges = [("a", "b", 1), ("b", "c", 2), ("c", "a", 3), ("c", "d", 4),
                 ("d", "e", 5)]
        self.assertEqual(D.bridges(edges), {4, 5})

    def test_an_edge_with_no_key_is_never_reported(self):
        self.assertEqual(D.bridges([("a", "b", None), ("b", "c", 7)]), {7})

    def test_a_long_chain_does_not_recurse(self):
        edges = [(i, i + 1, i) for i in range(5000)]
        self.assertEqual(len(D.bridges(edges)), 5000)


class OfManifest(unittest.TestCase):
    def test_a_hinge_is_free(self):
        self.assertEqual(of(hinge_manifest()), {"j001": D.FREE})

    def test_every_joint_of_a_four_bar_is_defined(self):
        got = of(four_bar_manifest())
        self.assertEqual(set(got.values()), {D.DEFINED})
        self.assertEqual(len(got), 4)

    def test_a_joint_off_the_loop_is_free(self):
        data = four_bar_manifest()
        data["rigid_groups"].append(
            {"id": "g004", "name": "wheel", "components": [], "grounded": False,
             "frame": None, "bbox_diag": 0.1})
        data["joints"].append(
            {"id": "j005", "type": "revolute", "parent_group": "g003",
             "child_group": "g004", "origin": [0.1, 0.1, 0.0],
             "axis": [0.0, 0.0, 1.0], "secondary_axis": [1.0, 0.0, 0.0]})
        got = of(data)
        self.assertEqual(got["j005"], D.FREE)
        self.assertEqual({got[k] for k in ("j001", "j002", "j003", "j004")},
                         {D.DEFINED})

    def test_a_gear_pair_closes_a_chain_through_its_teeth(self):
        data = hinge_manifest()
        data["rigid_groups"].append(
            {"id": "g002", "name": "gear", "components": [], "grounded": False,
             "frame": None, "bbox_diag": 0.1})
        gear = copy.deepcopy(data["joints"][0])
        gear.update(id="j002", child_group="g002", limits=None,
                    coupling={"kind": "gear", "driver_joint": "j001",
                              "ratio": -1.0})
        data["joints"].append(gear)
        self.assertEqual(of(data), {"j001": D.DEFINED, "j002": D.DEFINED})

    def test_a_screw_alone_is_free(self):
        data = hinge_manifest()
        j = data["joints"][0]
        j.update(type="screw", limits=None,
                 coupling={"kind": "screw", "driver_joint": None,
                           "lead_m_per_rev": 0.004})
        self.assertEqual(of(data), {"j001": D.FREE})

    def test_grounded_groups_are_one_world(self):
        # A link pinned to two fixed brackets closes a loop through the
        # world. A free joint is an under-mated part.
        data = hinge_manifest()
        data["rigid_groups"] += [
            {"id": "g002", "name": "bracket", "components": [], "grounded": True,
             "frame": None, "bbox_diag": 0.1},
            {"id": "g003", "name": "flap", "components": [], "grounded": False,
             "frame": None, "bbox_diag": 0.1}]
        second = copy.deepcopy(data["joints"][0])
        second.update(id="j002", parent_group="g002", limits=None)
        data["joints"] += [
            second,
            {"id": "j003", "type": "fixed", "parent_group": "g000",
             "child_group": "g003", "origin": [0, 0, 0], "axis": None,
             "secondary_axis": None},
            {"id": "j004", "type": "free", "parent_group": "g000",
             "child_group": "g003"}]
        self.assertEqual(of(data), {"j001": D.DEFINED, "j002": D.DEFINED,
                                    "j003": D.DEFINED, "j004": D.FREE})


class ControlColour(unittest.TestCase):
    """The color the rig paints each control bone (rig_build.py)."""

    def colours(self, data):
        from CADder.rig import graph, rig_build
        m = manifest.parse(data)
        plan = graph.build(m)
        status = D.of_manifest(m)
        children = {j.child_group for j in m.joints}
        return {bp.group.id: rig_build._control_colour(bp, status, children)
                for bp in plan.bones}

    def test_the_input_of_a_four_bar_is_blue(self):
        got = self.colours(four_bar_manifest())
        self.assertEqual(got["g001"], "THEME04")
        self.assertEqual(got["g002"], "THEME04")

    def test_a_hinge_is_red(self):
        self.assertEqual(self.colours(hinge_manifest())["g001"], "THEME01")

    def test_a_root_that_no_joint_ends_on_is_a_gray_ground(self):
        # a ground of the Joints module: a root the user can pose
        data = hinge_manifest()
        data["rigid_groups"][0]["grounded"] = False
        self.assertEqual(self.colours(data)["g000"], "THEME13")

    def test_an_under_mated_part_is_red(self):
        data = hinge_manifest()
        data["rigid_groups"].append(
            {"id": "g002", "name": "loose", "components": [], "grounded": False,
             "frame": None, "bbox_diag": 0.1})
        data["joints"].append({"id": "j002", "type": "free",
                               "parent_group": "g000", "child_group": "g002"})
        self.assertEqual(self.colours(data)["g002"], "THEME01")


if __name__ == "__main__":
    unittest.main()
