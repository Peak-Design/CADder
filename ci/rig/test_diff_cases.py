# SPDX-License-Identifier: GPL-3.0-or-later
"""What an update reads as a part of the scene, and what it does not.

The import tags more objects than its parts. An update that reads one of
the others as a part finds it in no export, and removes it. No bpy: the
objects here carry only what diff.from_objects reads.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import diff  # noqa: E402


class Obj:
    def __init__(self, name, props, type="MESH", instance_type="NONE"):
        self.name = name
        self._props = props
        self.type = type
        self.instance_type = instance_type

    def get(self, key, fallback=None):
        return self._props.get(key, fallback)


def tags(path, component="", **more):
    out = {"SWMESH_file": "asm", "SWMESH_path": path}
    if component:
        out["RIG_component_id"] = component
    out.update(more)
    return out


class BranchTests(unittest.TestCase):

    def test_a_subassembly_empty_is_not_a_part(self):
        objects = [
            Obj("rod", tags("lifter-1/rod-1", "c002")),
            Obj("lifter", tags("lifter-1", SWMESH_role="node"), type="EMPTY"),
        ]
        found = diff.from_objects(objects, "asm")
        self.assertEqual([o.path for o in found], ["lifter-1/rod-1"])

    def test_an_empty_of_an_older_build_is_not_a_part(self):
        # No role, and the component id of a rigid subassembly.
        objects = [Obj("lifter", tags("lifter-1", "c002"), type="EMPTY")]
        self.assertEqual(diff.from_objects(objects, "asm"), [])

    def test_a_collection_instance_is_a_part(self):
        objects = [Obj("rod", tags("rod-1", "c001"), type="EMPTY",
                       instance_type="COLLECTION")]
        self.assertEqual([o.path for o in diff.from_objects(objects, "asm")],
                         ["rod-1"])

    def test_a_prototype_is_not_a_part(self):
        objects = [Obj("rod", {"SWMESH_file": "asm", "SWMESH_definition": 1,
                               "SWMESH_prototype": True,
                               "RIG_component_id": "c001"})]
        self.assertEqual(diff.from_objects(objects, "asm"), [])


def occ(path, persistent, name):
    return diff.Occurrence(path=path, persistent=persistent, name=name,
                           component_id="c001")


class CopyTests(unittest.TestCase):
    """A copy made in Blender has the part's tags. It is the user's object,
    and the part is still the part."""

    def test_a_copy_is_split_off_and_the_part_is_kept(self):
        part = occ("bolt-1", "pbolt", "bolt")
        copy = occ("bolt-1", "pbolt", "bolt.001")
        other = occ("base-1", "pbase", "base")
        kept, copies = diff.split_copies([copy, part, other])
        self.assertEqual([o.name for o in kept], ["bolt", "base"])
        self.assertEqual([o.name for o in copies], ["bolt.001"])
        d = diff.compare(kept, [occ("bolt-1", "pbolt", "bolt"),
                                occ("base-1", "pbase", "base")])
        self.assertEqual((len(d.pairs), d.added, d.removed), (2, [], []))

    def test_the_rank_decides_which_is_the_part(self):
        part = occ("bolt-1", "pbolt", "bolt")
        copy = occ("bolt-1", "pbolt", "bolt.001")
        kept, copies = diff.split_copies(
            [part, copy], rank=lambda o: o.name != "bolt.001")
        self.assertEqual([o.name for o in kept], ["bolt.001"])
        self.assertEqual([o.name for o in copies], ["bolt"])

    def test_parts_without_a_path_are_not_copies(self):
        # An older import: the parts of a rigid subassembly share one
        # component id and carry no path.
        a = diff.Occurrence(component_id="c002", name="rod")
        b = diff.Occurrence(component_id="c002", name="block")
        kept, copies = diff.split_copies([a, b])
        self.assertEqual((len(kept), copies), (2, []))


if __name__ == "__main__":
    unittest.main()
