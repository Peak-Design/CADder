# SPDX-License-Identifier: GPL-3.0-or-later
"""Which row of a poses reply belongs to which part of the scene.

The CAD application answers "poses" under the component ids of its CURRENT
walk, and those ids shift when a part is added or deleted in front of
another. The scene and its manifest still use the ids of the export they
came from. So a row is matched by the persistent id first, which names the
same occurrence after an edit, and by the component id only where one side
has no persistent id. An id match whose persistent ids disagree, or whose
row belongs to another known part, is not a match.

No bpy: the matching is plain Python."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import ui  # noqa: E402
from CADder.rig import manifest as man_mod  # noqa: E402


def at(x):
    return [1.0, 0.0, 0.0, x, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0]


def row(cid, pid, x, requested=None):
    out = {"id": cid, "sw_path": cid + "-1", "transform": at(x)}
    if pid is not None:
        out["sw_persistent_id"] = pid
    if requested is not None:
        out["requested_id"] = requested
    return out


def x_of(rows):
    return rows[0][3]


class MatchTests(unittest.TestCase):

    def test_an_edit_that_renumbers_does_not_swap_poses(self):
        # The export: c002 = D, c003 = X, c004 = Y. D is deleted in CAD, so
        # the walk now calls X c002 and Y c003.
        scene = {"D": ("P_D", "c002"), "X": ("P_X", "c003"),
                 "Y": ("P_Y", "c004")}
        reply = [row("c002", "P_X", 0.5), row("c003", "P_Y", 0.7)]
        got = ui.match_pose_rows(reply, scene)
        self.assertEqual(x_of(got["X"]), 0.5)
        self.assertEqual(x_of(got["Y"]), 0.7)
        self.assertNotIn("D", got)

    def test_the_id_answers_when_one_side_has_no_persistent_id(self):
        got = ui.match_pose_rows([row("c002", None, 0.5)],
                                 {"A": ("P_A", "c002")})
        self.assertEqual(x_of(got["A"]), 0.5)
        got = ui.match_pose_rows([row("c002", "P_A", 0.5)],
                                 {"A": (None, "c002")})
        self.assertEqual(x_of(got["A"]), 0.5)

    def test_an_id_match_of_another_known_part_is_not_taken(self):
        # A has no persistent id, and its old id is now X's. X is in the
        # scene under its persistent id, so the row is X's.
        scene = {"A": (None, "c002"), "X": ("P_X", "c003")}
        got = ui.match_pose_rows([row("c002", "P_X", 0.5)], scene)
        self.assertEqual(x_of(got["X"]), 0.5)
        self.assertNotIn("A", got)

    def test_the_requested_persistent_id_names_the_part(self):
        # The CAD application can write a persistent id of its own for an
        # occurrence it found by the one it was asked for.
        got = ui.match_pose_rows(
            [row("c002", "P_X_new", 0.5, requested="P_X")],
            {"X": ("P_X", "c003")})
        self.assertEqual(x_of(got["X"]), 0.5)

    def test_rows_without_a_whole_transform_are_skipped(self):
        broken = {"id": "c001", "sw_persistent_id": "P_A", "transform": [1.0]}
        self.assertEqual(ui.match_pose_rows([broken], {"A": ("P_A", "c001")}), {})


class ManifestTests(unittest.TestCase):

    def test_the_manifest_takes_each_pose_by_persistent_id(self):
        data = {
            "manifest_version": "1.0.0",
            "generator": {"name": "Peak.Cadder", "version": "test"},
            "units": {"length": "meter", "angle": "radian"},
            "frame": {"handedness": "right", "up_axis": "Z",
                      "transform_convention": "row_major_4x4_global"},
            "step_export": {"file": "a.step", "ap": "AP214", "sha1": None,
                            "occurrence_matching": None},
            "components": [
                {"id": cid, "sw_path": cid + "-1", "step_name": cid,
                 "step_occurrence_path": None, "sw_persistent_id": pid,
                 "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0],
                               [0, 0, 0, 1]]}
                for cid, pid in (("c002", "P_D"), ("c003", "P_X"),
                                 ("c004", "P_Y"))],
            "rigid_groups": [{"id": "g000", "name": "all",
                              "components": ["c002", "c003", "c004"],
                              "grounded": True, "frame": None,
                              "bbox_diag": 0.1}],
            "joints": [], "loops": [], "warnings": [],
        }
        m = man_mod.parse(data)
        ui.poses_into_manifest(
            m, [row("c002", "P_X", 0.5), row("c003", "P_Y", 0.7)])
        by_id = {c.id: c.transform[0][3] for c in m.components}
        self.assertEqual(by_id, {"c002": 0, "c003": 0.5, "c004": 0.7})


if __name__ == "__main__":
    unittest.main()
