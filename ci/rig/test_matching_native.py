# SPDX-License-Identifier: GPL-3.0-or-later
"""Match Geometry on the parts of a direct send.

The direct link tags every part with its component when it builds it, so
there is nothing to match. The matcher kept a tag only where the object's
STEP name agreed with the component's, and a direct-send part has no STEP
name. Match Geometry after a send therefore took the component and group
tags off the parts of a rigid subassembly, the component went unmatched,
and the next Relink left those parts off the rig. No bpy.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import manifest, matching  # noqa: E402
from CADder.ci.rig.test_matching import FakeObj, translated  # noqa: E402


def lifter_manifest():
    return manifest.parse({
        "manifest_version": "1.0.0",
        "generator": {"name": "test", "version": "0"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "asm.step"},
        "components": [
            {"id": "c001", "sw_path": "base-1", "step_name": "base",
             "step_occurrence_path": None,
             "transform": translated(0.0, 0.0, 0.0)},
            {"id": "c002", "sw_path": "lifter-1", "step_name": "lifter",
             "step_occurrence_path": None, "subassembly_solving": "rigid",
             "transform": translated(0.5, 0.0, 0.0)},
        ],
        "rigid_groups": [
            {"id": "g000", "name": "base", "components": ["c001"],
             "grounded": True},
            {"id": "g001", "name": "lifter", "components": ["c002"],
             "grounded": False},
        ],
        "joints": [], "loops": [], "warnings": [],
    })


def native(name, component, group, path, matrix, local=None):
    props = {"SWMESH_file": "asm", "SWMESH_path": path,
             "RIG_component_id": component, "RIG_group": group}
    if local is not None:
        props["SWMESH_local"] = [v for row in local for v in row]
    return FakeObj(name, props, matrix)


def y_up(rows):
    """The frame a Y-up send puts the parts in."""
    frame = [[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, -1.0, 0.0],
             [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
    return matching.apply_frame(frame, rows), frame


class DirectSendTest(unittest.TestCase):

    def scene(self, place=lambda rows: rows):
        base = native("base", "c001", "g000", "base-1",
                      place(translated(0.0, 0.0, 0.0)))
        rod = native("rod", "c002", "g001", "lifter-1/rod-1",
                     place(translated(0.6, 0.0, 0.0)),
                     local=translated(0.1, 0.0, 0.0))
        barrel = native("barrel", "c002", "g001", "lifter-1/barrel-1",
                        place(translated(0.5, 0.0, 0.0)),
                        local=translated(0.0, 0.0, 0.0))
        return [base, rod, barrel]

    def test_the_tags_of_the_send_are_kept(self):
        objs = self.scene()
        report = matching.match(lifter_manifest(), objects=objs,
                                collections=[])
        for obj, cid, gid in zip(objs, ("c001", "c002", "c002"),
                                 ("g000", "g001", "g001")):
            self.assertEqual(obj.get("RIG_component_id"), cid, obj.name)
            self.assertEqual(obj.get("RIG_group"), gid, obj.name)
        self.assertEqual(report.unmatched, [])
        got = sorted((e.component_id, e.object_name) for e in report.matched)
        self.assertEqual(got, [("c001", "base"), ("c002", "barrel"),
                               ("c002", "rod")])
        self.assertEqual({e.object_path for e in report.matched},
                         {"base-1", "lifter-1/rod-1", "lifter-1/barrel-1"})
        self.assertGreaterEqual(report.frame_agree, 3)

    def test_the_frame_of_a_y_up_send_is_found(self):
        frame = y_up(translated(0, 0, 0))[1]
        objs = self.scene(place=lambda rows: y_up(rows)[0])
        report = matching.match(lifter_manifest(), objects=objs,
                                collections=[])
        self.assertGreater(report.frame_agree, 0)
        for i in range(4):
            for j in range(4):
                self.assertAlmostEqual(report.frame_rows[i][j], frame[i][j])

    def test_a_part_of_another_assembly_keeps_its_tags(self):
        # A direct-send part whose component this manifest does not know.
        other = native("bolt", "c077", "g009", "bolt-1",
                       translated(2.0, 0.0, 0.0))
        objs = self.scene() + [other]
        matching.match(lifter_manifest(), objects=objs, collections=[])
        self.assertEqual(other.get("RIG_component_id"), "c077")
        self.assertEqual(other.get("RIG_group"), "g009")


if __name__ == "__main__":
    unittest.main()
