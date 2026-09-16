# SPDX-License-Identifier: GPL-3.0-or-later
"""The driver choice: mechanisms, candidates, labels, applying a choice."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import inputs, manifest as man_mod  # noqa: E402


def _t(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


def _joint(jid, jtype, parent, child, origin):
    return {"id": jid, "type": jtype, "parent_group": parent,
            "child_group": child, "origin": origin, "axis": [0, 0, 1],
            "secondary_axis": [1, 0, 0], "limits": None}


def _manifest():
    """A crank-driven slider-crank with a parallel second rod (two loops,
    one mechanism) and an unrelated hinged lid (a loop of its own)."""
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "test", "version": "0"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "x.step", "ap": "AP214", "sha1": None,
                        "occurrence_matching": None},
        "components": [
            {"id": "c%03d" % i, "sw_path": name + "-1", "step_name": name,
             "step_occurrence_path": None, "transform": _t(0, 0, 0)}
            for i, name in enumerate(
                ["ground", "crank", "slider", "rod", "rod2", "lid", "prop"], 1)
        ],
        "rigid_groups": [
            {"id": "g%03d" % i, "name": name, "components": ["c%03d" % (i + 1)],
             "grounded": i == 0, "frame": None, "bbox_diag": 1.0}
            for i, name in enumerate(
                ["ground", "crank", "slider", "rod", "rod2", "lid", "prop"])
        ],
        "joints": [
            _joint("j001", "revolute", "g000", "g001", [0, 0, 0]),
            _joint("j002", "prismatic", "g000", "g002", [4, 0, 0]),
            _joint("j003", "revolute", "g002", "g003", [4, 0, 0]),
            _joint("j004", "revolute", "g001", "g003", [1, 0, 0]),
            _joint("j005", "revolute", "g002", "g004", [4, 0, 0.1]),
            _joint("j006", "revolute", "g001", "g004", [1, 0, 0.1]),
            _joint("j007", "revolute", "g000", "g005", [9, 0, 0]),
            _joint("j008", "revolute", "g005", "g006", [9, 1, 0]),
            _joint("j009", "revolute", "g000", "g006", [10, 0, 0]),
        ],
        "loops": [
            {"id": "loop001", "member_joints": ["j001", "j002", "j003", "j004"],
             "closure_joint": "j004", "closure_kind": "ik",
             "suggested_driver_joint": "j001", "planar": True,
             "plane_normal": [0, 0, 1],
             "driver_candidates": [
                 {"joint": "j001", "closure_joint": "j004", "closure_kind": "ik"},
                 {"joint": "j002", "closure_joint": "j003", "closure_kind": "ik"}]},
            {"id": "loop002", "member_joints": ["j001", "j002", "j005", "j006"],
             "closure_joint": "j006", "closure_kind": "ik",
             "suggested_driver_joint": "j001", "planar": True,
             "plane_normal": [0, 0, 1],
             "driver_candidates": [
                 {"joint": "j001", "closure_joint": "j006", "closure_kind": "ik"},
                 {"joint": "j002", "closure_joint": "j005", "closure_kind": "ik"}]},
            {"id": "loop003", "member_joints": ["j007", "j008", "j009"],
             "closure_joint": "j008", "closure_kind": "ik",
             "suggested_driver_joint": "j007", "planar": True,
             "plane_normal": [0, 0, 1],
             "driver_candidates": [
                 {"joint": "j007", "closure_joint": "j008", "closure_kind": "ik"},
                 {"joint": "j009", "closure_joint": "j008", "closure_kind": "ik"}]},
        ],
        "warnings": [],
    }


class InputsTest(unittest.TestCase):
    def setUp(self):
        self.m = man_mod.parse(_manifest())

    def test_loops_sharing_joints_are_one_mechanism(self):
        self.assertEqual(inputs.mechanisms(self.m),
                         [["loop001", "loop002"], ["loop003"]])

    def test_candidates_in_first_seen_order_and_labelled(self):
        mech = ["loop001", "loop002"]
        self.assertEqual(inputs.candidates(self.m, mech), ["j001", "j002"])
        self.assertEqual(inputs.current(self.m, mech), "j001")
        self.assertEqual(inputs.label(self.m, "j001"), "crank - hinge on ground")
        self.assertEqual(inputs.label(self.m, "j002"), "slider - slide on ground")

    def test_applying_a_choice_moves_the_cut_with_the_driver(self):
        mech = ["loop001", "loop002"]
        changed = inputs.apply(self.m, mech, "j002")
        self.assertEqual(changed, ["loop001", "loop002"])
        by_id = {lp.id: lp for lp in self.m.loops}
        self.assertEqual(by_id["loop001"].suggested_driver_joint, "j002")
        self.assertEqual(by_id["loop001"].closure_joint, "j003")
        self.assertEqual(by_id["loop002"].closure_joint, "j005")
        # The lid's loop is another mechanism and is not touched.
        self.assertEqual(by_id["loop003"].suggested_driver_joint, "j007")
        self.assertEqual(inputs.current(self.m, mech), "j002")
        # Applying the same choice again changes nothing; switching back
        # restores the exporter's cut.
        self.assertEqual(inputs.apply(self.m, mech, "j002"), [])
        inputs.apply(self.m, mech, "j001")
        self.assertEqual(by_id["loop001"].closure_joint, "j004")

    def test_a_joint_not_offered_by_the_mechanism_changes_nothing(self):
        self.assertEqual(inputs.apply(self.m, ["loop001", "loop002"], "j007"), [])

    def test_keys_survive_renumbering(self):
        k = inputs.key(self.m, "j002")
        self.assertEqual(k, "ground-1|slider-1|prismatic")
        # Another export with the joints numbered the other way round.
        data = _manifest()
        for j in data["joints"]:
            j["id"] = "j%03d" % (10 - int(j["id"][1:]))
        for lp in data["loops"]:
            lp["member_joints"] = ["j%03d" % (10 - int(x[1:])) for x in lp["member_joints"]]
            lp["closure_joint"] = "j%03d" % (10 - int(lp["closure_joint"][1:]))
            lp["suggested_driver_joint"] = "j%03d" % (10 - int(lp["suggested_driver_joint"][1:]))
            for c in lp["driver_candidates"]:
                c["joint"] = "j%03d" % (10 - int(c["joint"][1:]))
                c["closure_joint"] = "j%03d" % (10 - int(c["closure_joint"][1:]))
        other = man_mod.parse(data)
        self.assertEqual(inputs.find_by_key(other, k), "j008")

    def test_manifest_without_candidates_offers_nothing(self):
        data = _manifest()
        for lp in data["loops"]:
            del lp["driver_candidates"]
        m = man_mod.parse(data)
        self.assertEqual(inputs.candidates(m, ["loop001", "loop002"]), [])
        self.assertEqual(inputs.apply(m, ["loop001", "loop002"], "j002"), [])

    def test_candidate_must_name_a_member(self):
        data = _manifest()
        data["loops"][0]["driver_candidates"][0]["joint"] = "j009"
        with self.assertRaises(man_mod.ManifestError):
            man_mod.parse(data)


def _with_mechanisms():
    """The same crank-driven slider-crank, with the exporter's mechanism
    block: the slider input re-cuts both rod loops and, in this fixture,
    reaches j003 from the other side."""
    data = _manifest()
    # The crank carries a limit derived from the slider's stroke in the
    # crank-driven configuration; the slider option drops it and limits
    # the slide instead.
    data["joints"][0]["limits"] = {
        "rotation": {"min": -1.0, "max": 1.0, "value_at_rest": 0.0},
        "translation": None}

    def loop(lid, members, closure, driver):
        return {"id": lid, "member_joints": members, "closure_joint": closure,
                "closure_kind": "ik", "suggested_driver_joint": driver,
                "planar": True, "plane_normal": [0, 0, 1], "driver_candidates": []}

    data["mechanisms"] = [
        {"id": "mech001", "loops": ["loop001", "loop002"],
         "inputs": [
             {"joint": "j001", "flipped_joints": [],
              "loops": [loop("loop001", ["j001", "j002", "j003", "j004"], "j004", "j001"),
                        loop("loop002", ["j001", "j002", "j005", "j006"], "j006", "j001")]},
             {"joint": "j002", "flipped_joints": ["j003"],
              "joint_limits": [
                  {"joint": "j001", "limits": None},
                  {"joint": "j002", "limits": {
                      "rotation": None,
                      "translation": {"min": -0.5, "max": 0.5, "value_at_rest": 0.0}}}],
              "loops": [loop("loop001", ["j001", "j002", "j003", "j004"], "j003", "j002"),
                        loop("loop002", ["j001", "j002", "j005", "j006"], "j005", "j002")]}]},
        {"id": "mech002", "loops": ["loop003"],
         "inputs": [
             {"joint": "j007", "flipped_joints": [],
              "loops": [loop("loop003", ["j007", "j008", "j009"], "j008", "j007")]},
             {"joint": "j009", "flipped_joints": [],
              "loops": [loop("loop003", ["j007", "j008", "j009"], "j008", "j009")]}]},
    ]
    return data


class MechanismInputsTest(unittest.TestCase):
    def setUp(self):
        self.m = man_mod.parse(_with_mechanisms())

    def test_mechanisms_and_inputs_come_from_the_block(self):
        self.assertEqual(inputs.mechanisms(self.m),
                         [["loop001", "loop002"], ["loop003"]])
        self.assertEqual(inputs.candidates(self.m, ["loop001", "loop002"]), ["j001", "j002"])
        self.assertEqual(inputs.current(self.m, ["loop001", "loop002"]), "j001")

    def test_a_joint_a_coupling_writes_is_not_offered(self):
        # A driver is one-way: pushing a cam's follower never turns the
        # cam (live cam-follower, 2026-09-15). An exporter from before the
        # pruning still lists it, so the consumer hides it too.
        data = _with_mechanisms()
        data["joints"][1]["coupling"] = {
            "kind": "table", "driver_joint": "j001",
            "samples": [[0.0, 0.0], [1.0, 0.1]], "periodic": False}
        m = man_mod.parse(data)
        self.assertEqual(inputs.candidates(m, ["loop001", "loop002"]), ["j001"])

    def test_an_option_is_applied_whole_and_undone_whole(self):
        mech = ["loop001", "loop002"]
        j003 = self.m.joint_by_id()["j003"]
        self.assertEqual((j003.parent_group, j003.child_group), ("g002", "g003"))

        self.assertEqual(inputs.apply(self.m, mech, "j002"), ["loop001", "loop002"])
        by_id = {lp.id: lp for lp in self.m.loops}
        self.assertEqual(by_id["loop001"].suggested_driver_joint, "j002")
        self.assertEqual(by_id["loop001"].closure_joint, "j003")
        self.assertEqual(by_id["loop002"].closure_joint, "j005")
        self.assertEqual(by_id["loop003"].suggested_driver_joint, "j007")
        # The flipped joint turned round, once.
        self.assertEqual((j003.parent_group, j003.child_group), ("g003", "g002"))
        self.assertEqual(inputs.current(self.m, mech), "j002")
        self.assertEqual(inputs.apply(self.m, mech, "j002"), [])
        self.assertEqual((j003.parent_group, j003.child_group), ("g003", "g002"))

        # Back to the exporter's choice: loops and orientation restored.
        self.assertEqual(inputs.apply(self.m, mech, "j001"), ["loop001", "loop002"])
        by_id = {lp.id: lp for lp in self.m.loops}
        self.assertEqual(by_id["loop001"].closure_joint, "j004")
        self.assertEqual(by_id["loop002"].closure_joint, "j006")
        self.assertEqual((j003.parent_group, j003.child_group), ("g002", "g003"))
        self.assertEqual(len(self.m.loops), 3)

    def test_an_option_takes_its_limits_and_gives_them_back(self):
        mech = ["loop001", "loop002"]
        crank = self.m.joint_by_id()["j001"]
        slider = self.m.joint_by_id()["j002"]
        self.assertEqual((crank.rotation_limit.min, crank.rotation_limit.max), (-1.0, 1.0))
        self.assertIsNone(slider.translation_limit)

        inputs.apply(self.m, mech, "j002")
        self.assertIsNone(crank.rotation_limit)
        self.assertEqual((slider.translation_limit.min, slider.translation_limit.max),
                         (-0.5, 0.5))

        inputs.apply(self.m, mech, "j001")
        self.assertEqual((crank.rotation_limit.min, crank.rotation_limit.max), (-1.0, 1.0))
        self.assertIsNone(slider.translation_limit)

    def test_the_applied_loops_are_copies(self):
        mech = ["loop001", "loop002"]
        inputs.apply(self.m, mech, "j002")
        applied = next(lp for lp in self.m.loops if lp.id == "loop001")
        applied.closure_joint = "j999"
        inputs.apply(self.m, mech, "j001")
        inputs.apply(self.m, mech, "j002")
        again = next(lp for lp in self.m.loops if lp.id == "loop001")
        self.assertEqual(again.closure_joint, "j003")

    def test_a_flipped_joint_keeps_its_key(self):
        k = inputs.key(self.m, "j003")
        inputs.apply(self.m, ["loop001", "loop002"], "j002")
        self.assertEqual(inputs.key(self.m, "j003"), k)

    def test_option_loops_must_match_the_mechanism(self):
        data = _with_mechanisms()
        data["mechanisms"][0]["inputs"][1]["loops"].pop()
        with self.assertRaises(man_mod.ManifestError):
            man_mod.parse(data)
        data = _with_mechanisms()
        data["mechanisms"][0]["inputs"][1]["flipped_joints"] = ["j999"]
        with self.assertRaises(man_mod.ManifestError):
            man_mod.parse(data)


def _rack_pinion():
    """A rack and a pinion, offered as a mechanism of their own: one degree
    of freedom held from either end."""
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "test", "version": "0"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "x.step", "ap": "AP214", "sha1": None,
                        "occurrence_matching": None},
        "components": [
            {"id": "c%03d" % i, "sw_path": n + "-1", "step_name": n,
             "step_occurrence_path": None, "transform": _t(0, 0, 0)}
            for i, n in enumerate(["frame", "rack", "pinion"], 1)
        ],
        "rigid_groups": [
            {"id": "g000", "name": "frame", "components": ["c001"],
             "grounded": True, "frame": _t(0, 0, 0), "bbox_diag": 0.3},
            {"id": "g001", "name": "rack", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": 0.15},
            {"id": "g002", "name": "pinion", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.03},
        ],
        "joints": [
            dict(_joint("j001", "prismatic", "g000", "g001", [0, 0, 0]),
                 coupling={"kind": "rack_pinion", "driver_joint": "j002",
                           "meters_per_radian": 0.0127}),
            _joint("j002", "revolute", "g000", "g002", [0, 0, 0]),
        ],
        "loops": [],
        "mechanisms": [{
            "id": "mech001", "loops": [],
            "inputs": [{"joint": "j002", "loops": [], "flipped_joints": [],
                        "joint_limits": []},
                       {"joint": "j001", "loops": [], "flipped_joints": [],
                        "joint_limits": []}],
        }],
        "warnings": [],
    }


class CoupledPairTest(unittest.TestCase):
    """A coupled pair is a mechanism with no loops. Taking its other input
    turns the coupling round instead of leaving the user to pose a channel
    a driver writes (Oscar, 2026-09-16, on the rack and pinion)."""

    def setUp(self):
        self.m = man_mod.parse(_rack_pinion())
        self.mech = inputs.mechanisms(self.m)[0]

    def test_a_pair_with_no_loops_is_named_by_its_own_id(self):
        self.assertEqual(self.mech, ["mech001"])

    def test_both_halves_are_offered(self):
        self.assertEqual(inputs.candidates(self.m, self.mech), ["j002", "j001"])
        self.assertEqual(inputs.current(self.m, self.mech), "j002")

    def test_taking_the_rack_turns_the_coupling_round(self):
        self.assertEqual(inputs.apply(self.m, self.mech, "j001"), ["mech001"])
        by_id = self.m.joint_by_id()
        self.assertIsNone(by_id["j001"].coupling)
        self.assertEqual(by_id["j002"].coupling.kind, "rack_pinion")
        self.assertEqual(by_id["j002"].coupling.driver_joint, "j001")
        # The mate's number is the same fact either way round.
        self.assertAlmostEqual(by_id["j002"].coupling.meters_per_radian, 0.0127)
        self.assertEqual(inputs.current(self.m, self.mech), "j001")

    def test_taking_the_same_input_again_changes_nothing(self):
        self.assertEqual(inputs.apply(self.m, self.mech, "j002"), [])

    def test_a_shape_coupling_is_not_a_pair(self):
        # A cam is not a ratio: pushing the follower never turns the cam.
        data = _rack_pinion()
        data["joints"][0]["coupling"] = {
            "kind": "table", "driver_joint": "j002",
            "samples": [[0.0, 0.0], [1.0, 0.01]]}
        m = man_mod.parse(data)
        mech = inputs.mechanisms(m)[0]
        self.assertEqual(inputs.candidates(m, mech), ["j002"])
        self.assertEqual(inputs.apply(m, mech, "j001"), [])


if __name__ == "__main__":
    unittest.main()
