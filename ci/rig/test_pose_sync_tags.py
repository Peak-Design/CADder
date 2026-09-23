# SPDX-License-Identifier: GPL-3.0-or-later
"""Pose sync finds each part by its tags, not only by the name the match
report holds.

The report keeps the names the parts had when the send ran. A part the
user renamed since then was not found and was not moved, and nothing said
so. An object that took the old name was moved in its place. No bpy.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import matching, pose_sync  # noqa: E402
from CADder.ci.rig.test_matching import make_manifest, translated  # noqa: E402
from CADder.ci.rig.test_pose_sync import PoseObj, close  # noqa: E402


def part(name, component, path, matrix):
    return PoseObj(name, {"RIG_component_id": component, "SWMESH_path": path},
                   matrix)


def report(*entries):
    return matching.MatchReport(matched=list(entries),
                                frame_rows=matching.identity_frame())


def entry(component, name, path=None):
    return matching.MatchEntry(component, name, 0, "exact", object_path=path)


class RenamedPartTest(unittest.TestCase):

    def test_a_renamed_part_is_found_by_its_tags(self):
        target = translated(0.3, 0.0, 0.0)
        m = make_manifest([("c001", "rod", None, target)])
        rod = part("Piston Rod", "c001", "rod-2", translated(0.1, 0.0, 0.0))
        pr = pose_sync.sync(m, report(entry("c001", "rod-2", "rod-2")),
                            objects=[rod])
        self.assertEqual([n for n, _ in pr.moved], ["Piston Rod"])
        self.assertTrue(close(rod.matrix_basis, target))

    def test_an_object_that_took_the_old_name_is_not_moved(self):
        target = translated(0.3, 0.0, 0.0)
        m = make_manifest([("c001", "rod", None, target),
                           ("c009", "cap", None, translated(0.9, 0.0, 0.0))])
        rod = part("Piston Rod", "c001", "rod-2", translated(0.1, 0.0, 0.0))
        other = part("rod-2", "c009", "cap-1", translated(0.5, 0.0, 0.0))
        pr = pose_sync.sync(m, report(entry("c001", "rod-2", "rod-2")),
                            objects=[rod, other])
        self.assertIsNone(other.matrix_basis)
        self.assertTrue(close(rod.matrix_basis, target))
        self.assertEqual([n for n, _ in pr.moved], ["Piston Rod"])

    def test_a_part_of_a_rigid_subassembly_is_found_by_its_path(self):
        # Two parts of one component: the component id alone names both.
        m = make_manifest([("c002", "lifter", None, translated(0.5, 0.0, 0.0))])
        rod = part("my rod", "c002", "lifter-1/rod-1", translated(0.0, 0.0, 0.0))
        block = part("block", "c002", "lifter-1/block-1",
                     translated(0.5, 0.0, 0.0))
        pr = pose_sync.sync(
            m, report(entry("c002", "rod", "lifter-1/rod-1"),
                      entry("c002", "block", "lifter-1/block-1")),
            objects=[rod, block])
        self.assertEqual([n for n, _ in pr.moved], ["my rod"])
        self.assertEqual(pr.already_ok, 1)

    def test_a_part_that_is_gone_is_reported(self):
        m = make_manifest([("c001", "rod", None, translated(0.3, 0.0, 0.0))])
        pr = pose_sync.sync(m, report(entry("c001", "rod-2", "rod-2")),
                            objects=[], report_missing=True)
        self.assertEqual([n for n, _ in pr.skipped], ["rod-2"])

    def test_a_part_outside_the_callers_list_is_not_reported(self):
        # Refresh puts back only the parts that were on the rig. A free
        # part is not in its list, and is left alone without a word.
        m = make_manifest([("c001", "rod", None, translated(0.3, 0.0, 0.0))])
        pr = pose_sync.sync(m, report(entry("c001", "rod-2", "rod-2")),
                            objects=[])
        self.assertEqual(pr.skipped, [])

    def test_an_entry_without_a_path_still_resolves_by_name(self):
        # The STEP route: the name, and a component id that agrees.
        target = translated(0.3, 0.0, 0.0)
        m = make_manifest([("c001", "rod", None, target)])
        rod = PoseObj("rod", {"RIG_component_id": "c001"},
                      translated(0.1, 0.0, 0.0))
        pr = pose_sync.sync(m, report(entry("c001", "rod")), objects=[rod])
        self.assertEqual([n for n, _ in pr.moved], ["rod"])


if __name__ == "__main__":
    unittest.main()
