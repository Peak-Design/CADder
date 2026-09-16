# SPDX-License-Identifier: GPL-3.0-or-later
"""What changed between the assembly in the scene and the one that just
arrived. No bpy: the comparison is plain data, so it is tested here.

The cases that matter are the ones where a wrong answer is silent: a part
that was renamed must not read as one part removed and another added, and
two parts that swapped places must not be paired the wrong way round.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import diff  # noqa: E402


def occ(path, persistent="", group="", definition=1, transform=None,
        component=""):
    return diff.Occurrence(
        path=path, persistent=persistent, component_id=component,
        name=path.rpartition("/")[2], group_id=group,
        definition_id=definition, transform=transform)


def at(x):
    return [1, 0, 0, x, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


class FakeObj:
    """Only what diff.from_objects reads."""

    def __init__(self, name, props):
        self.name = name
        self._props = props

    def get(self, key, fallback=None):
        return self._props.get(key, fallback)


class CompareTests(unittest.TestCase):

    def test_an_unchanged_assembly_pairs_every_part(self):
        old = [occ("a-1", "pa"), occ("b-1", "pb")]
        new = [occ("a-1", "pa"), occ("b-1", "pb")]
        d = diff.compare(old, new)
        self.assertEqual(len(d.pairs), 2)
        self.assertEqual(d.added, [])
        self.assertEqual(d.removed, [])
        self.assertFalse(d.structural)
        self.assertTrue(all(p.how == "identity" for p in d.pairs))

    def test_a_new_part_is_added_and_a_deleted_one_removed(self):
        old = [occ("a-1", "pa"), occ("gone-1", "pg")]
        new = [occ("a-1", "pa"), occ("fresh-1", "pf")]
        d = diff.compare(old, new)
        self.assertEqual([o.path for o in d.added], ["fresh-1"])
        self.assertEqual([o.path for o in d.removed], ["gone-1"])
        self.assertTrue(d.structural)

    def test_a_renamed_instance_is_the_same_part(self):
        # The path changed, the persistent id did not: one part, renamed,
        # not one deleted and one added.
        old = [occ("bracket-1", "pa"), occ("b-1", "pb")]
        new = [occ("bracket-7", "pa"), occ("b-1", "pb")]
        d = diff.compare(old, new)
        self.assertEqual(d.added, [])
        self.assertEqual(d.removed, [])
        renamed = [p for p in d.pairs if p.old.path == "bracket-1"][0]
        self.assertEqual(renamed.new.path, "bracket-7")
        self.assertEqual(renamed.how, "persistent")

    def test_a_part_with_a_new_persistent_id_is_found_by_path(self):
        # What happens when a part is replaced by another of the same name,
        # or when SolidWorks cannot give a reference at all.
        old = [occ("a-1", "pa"), occ("b-1", "")]
        new = [occ("a-1", "pa"), occ("b-1", "pnew")]
        d = diff.compare(old, new)
        self.assertEqual(d.added, [])
        self.assertEqual(d.removed, [])
        found = [p for p in d.pairs if p.old.path == "b-1"][0]
        self.assertEqual(found.how, "path")

    def test_two_parts_of_one_subassembly_are_told_apart_by_path(self):
        # Both carry the SUBASSEMBLY's persistent id, so the id alone is
        # ambiguous and only the pair of id and path names one part.
        old = [occ("lift-1/rod-1", "psub"), occ("lift-1/block-1", "psub")]
        new = [occ("lift-1/rod-1", "psub"), occ("lift-1/block-1", "psub")]
        d = diff.compare(old, new)
        self.assertEqual(len(d.pairs), 2)
        for pair in d.pairs:
            self.assertEqual(pair.old.path, pair.new.path)

    def test_an_ambiguous_id_does_not_pair_the_wrong_parts(self):
        # One of the two parts of the subassembly is gone and the other was
        # renamed. Nothing can say which is which, so the honest answer is
        # one added and one removed, not a silent swap.
        old = [occ("lift-1/rod-1", "psub"), occ("lift-1/block-1", "psub")]
        new = [occ("lift-1/rod-9", "psub")]
        d = diff.compare(old, new)
        self.assertEqual(len(d.pairs), 0)
        self.assertEqual(len(d.added), 1)
        self.assertEqual(len(d.removed), 2)

    def test_a_moved_part_is_paired_and_flagged(self):
        old = [occ("a-1", "pa", transform=at(0.0))]
        new = [occ("a-1", "pa", transform=at(0.25))]
        d = diff.compare(old, new)
        self.assertEqual(len(d.moved), 1)
        self.assertFalse(d.structural)      # a pose change is not structural

    def test_a_part_that_did_not_move_is_not_flagged(self):
        old = [occ("a-1", "pa", transform=at(0.25))]
        new = [occ("a-1", "pa", transform=at(0.25 + 1e-12))]
        d = diff.compare(old, new)
        self.assertEqual(d.moved, [])

    def test_new_geometry_and_a_new_group_are_flagged(self):
        old = [occ("a-1", "pa", group="g000", definition=1)]
        new = [occ("a-1", "pa", group="g003", definition=7)]
        d = diff.compare(old, new)
        pair = d.pairs[0]
        self.assertTrue(pair.reshaped)
        self.assertTrue(pair.regrouped)
        # A part that changed body is a rig change: its bone moved group.
        self.assertTrue(d.structural)

    def test_the_scene_side_reads_the_tags_the_import_wrote(self):
        objects = [
            FakeObj("rod", {"SWMESH_file": "cam", "SWMESH_path": "rod-1",
                            "SWMESH_persistent_id": "pa",
                            "RIG_component_id": "c001", "RIG_group": "g000",
                            "SWMESH_definition": 3}),
            FakeObj("of the user's own", {}),
            FakeObj("another file", {"SWMESH_file": "other",
                                     "SWMESH_path": "x-1",
                                     "RIG_component_id": "c001"}),
        ]
        found = diff.from_objects(objects, stem="cam")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, "rod-1")
        self.assertEqual(found[0].persistent, "pa")
        self.assertEqual(found[0].group_id, "g000")
        self.assertEqual(found[0].definition_id, 3)
        self.assertIs(found[0].payload, objects[0])

    def test_group_identity_is_the_parts_not_the_number(self):
        # The same two parts, in a group the next export numbered
        # differently: the identity is the same, so the bone is the same.
        old = [occ("a-1", "pa", group="g000"), occ("b-1", "pb", group="g000")]
        new = [occ("a-1", "pa", group="g004"), occ("b-1", "pb", group="g004")]
        self.assertEqual(list(diff.group_identity(old).values()),
                         list(diff.group_identity(new).values()))

    def test_group_identity_falls_back_to_the_path(self):
        parts = [occ("a-1", "", group="g000")]
        self.assertEqual(diff.group_identity(parts)["g000"], frozenset({"a-1"}))


if __name__ == "__main__":
    unittest.main()
