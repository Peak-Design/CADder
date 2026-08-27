# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene-frame estimation tests: a STEP import with another up axis rotates
the geometry. Matching must detect that frame from its own matches and the
rig must be built through it. Found live 2026-08-22: a Y-up STEPper import
left the rig lying in the manifest's Z-up frame, nowhere near the parts.

No bpy: matching.py degrades to plain Python and the fake objects below
provide the tiny surface it reads (get/keys/type/name/matrix_world)."""

import math
import os
import sys
import unittest

# The addons directory (the parent of the STEPper_NEXT repo root) makes
# "import STEPper_NEXT.rig" work from any checkout named STEPper_NEXT.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from STEPper_NEXT.rig import manifest, matching  # noqa: E402


ROT_X_POS90 = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]


def identity4():
    return [[1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def translated(x, y, z):
    m = identity4()
    m[0][3], m[1][3], m[2][3] = x, y, z
    return m


def rot_frame(rot, t=(0.0, 0.0, 0.0)):
    return [
        [rot[0][0], rot[0][1], rot[0][2], t[0]],
        [rot[1][0], rot[1][1], rot[1][2], t[1]],
        [rot[2][0], rot[2][1], rot[2][2], t[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


class FakeObj:
    """The exact object surface matching.py touches, nothing more."""

    def __init__(self, name, props, matrix):
        self.name = name
        self.type = "MESH"
        self._props = dict(props)
        self.matrix_world = matrix

    def get(self, key, default=None):
        return self._props.get(key, default)

    def keys(self):
        return self._props.keys()

    def __setitem__(self, key, value):
        self._props[key] = value


def two_part_manifest(with_paths=True):
    def comp(cid, name, transform):
        c = {
            "id": cid,
            "sw_path": "asm/" + name + "-1",
            "step_name": name,
            "step_occurrence_path": name if with_paths else None,
            "transform": transform,
        }
        return c

    return manifest.parse({
        "manifest_version": "1.0.0",
        "generator": {"name": "test", "version": "0"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "asm.step"},
        "components": [
            comp("c001", "base", identity4()),
            comp("c002", "arm", translated(0.1, 0.02, 0.03)),
        ],
        "rigid_groups": [
            {"id": "g000", "name": "base", "components": ["c001"], "grounded": True},
            {"id": "g001", "name": "arm", "components": ["c002"], "grounded": False},
        ],
        "joints": [{
            "id": "j001", "type": "revolute",
            "parent_group": "g000", "child_group": "g001",
            "origin": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0],
            "secondary_axis": [1.0, 0.0, 0.0],
        }],
        "loops": [],
        "warnings": [],
    })


def duplicate_name_manifest():
    """Two components that share one step_name, no occurrence paths: the
    scene where neither path anchors nor unique-name anchors exist."""
    def comp(cid, transform):
        return {
            "id": cid,
            "sw_path": "asm/part-" + cid[-1],
            "step_name": "part",
            "step_occurrence_path": None,
            "transform": transform,
        }

    return manifest.parse({
        "manifest_version": "1.0.0",
        "generator": {"name": "test", "version": "0"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "asm.step"},
        "components": [
            comp("c001", identity4()),
            comp("c002", translated(0.1, 0.02, 0.03)),
        ],
        "rigid_groups": [
            {"id": "g000", "name": "a", "components": ["c001"], "grounded": True},
            {"id": "g001", "name": "b", "components": ["c002"], "grounded": False},
        ],
        "joints": [{
            "id": "j001", "type": "revolute",
            "parent_group": "g000", "child_group": "g001",
            "origin": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0],
            "secondary_axis": [1.0, 0.0, 0.0],
        }],
        "loops": [],
        "warnings": [],
    })


def scene_objects(frame_rows, with_uuid=True, m=None):
    if m is None:
        m = two_part_manifest()
    objs = []
    for i, c in enumerate(m.components):
        placed = matching.apply_frame(frame_rows, [list(r) for r in c.transform])
        props = {"STEP_name": c.step_name, "STEP_file": "asm.step"}
        if with_uuid:
            props["STEP_uuid"] = i + 1
            # No STEP_parent: the occurrence path degrades to the leaf name,
            # which is exactly what the manifest's paths carry here.
        objs.append(FakeObj(c.step_name, props, placed))
    return objs


class TestFrameEstimation(unittest.TestCase):

    def test_aligned_import_gives_identity_frame(self):
        m = two_part_manifest()
        report = matching.match(m, objects=scene_objects(matching.identity_frame()))
        self.assertEqual(len(report.matched), 2)
        self.assertEqual(report.unmatched, [])
        self.assertEqual(report.frame_agree, 2)
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(
                    report.frame_rows[i][j], 1.0 if i == j else 0.0, places=9)
        self.assertIn("aligned", matching.describe_frame(report.frame_rows))

    def test_y_up_import_is_detected_from_name_anchors(self):
        frame = rot_frame(ROT_X_POS90)
        m = two_part_manifest()
        report = matching.match(m, objects=scene_objects(frame))
        self.assertEqual(len(report.matched), 2)
        self.assertEqual(report.frame_agree, 2)
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(report.frame_rows[i][j], frame[i][j], places=9)
        self.assertIn("Y-up", matching.describe_frame(report.frame_rows))

    def test_rotated_translated_scene_without_paths_uses_name_anchors(self):
        # The exporter's occurrence matcher refused (paths null), so no
        # path anchors exist: uniquely-named components must anchor the
        # FULL frame. Rotation AND translation: the old candidate loop knew
        # only zero-translation up-axis swaps, so a Y-up import dropped at
        # the 3D cursor could never be found (live 2026-08-22).
        frame = rot_frame(ROT_X_POS90, t=(0.3, -0.2, 0.15))
        m = two_part_manifest(with_paths=False)
        report = matching.match(m, objects=scene_objects(frame))
        self.assertEqual(len(report.matched), 2)
        self.assertTrue(all(e.step == 3 for e in report.matched))
        for i in range(3):
            for j in range(4):
                self.assertAlmostEqual(report.frame_rows[i][j], frame[i][j], places=9)

    def test_cursor_offset_import_is_carried_in_the_frame(self):
        # STEPper places imports at the 3D cursor (main.py transform_to_up
        # bakes the offset into matrix_world). The frame must carry that
        # translation so the rig lands on the geometry, not at the origin.
        frame = rot_frame([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                          t=(0.3, -0.2, 0.15))
        m = two_part_manifest()
        report = matching.match(m, objects=scene_objects(frame))
        self.assertEqual(len(report.matched), 2)
        self.assertEqual(report.frame_agree, 2)
        for i in range(3):
            self.assertAlmostEqual(report.frame_rows[i][3], frame[i][3], places=9)
        self.assertIn("offset", matching.describe_frame(report.frame_rows))

    def test_duplicate_names_fall_back_to_candidate_up_axes(self):
        # Every name duplicated: neither path nor unique-name anchors
        # exist, so the canonical up-axis conversions compete: transform
        # agreement under the winner is the only thing telling twins apart.
        frame = rot_frame(ROT_X_POS90)
        m = duplicate_name_manifest()
        report = matching.match(m, objects=scene_objects(frame, m=m))
        self.assertEqual(len(report.matched), 2)
        self.assertTrue(all(e.step == 3 for e in report.matched))
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(report.frame_rows[i][j], frame[i][j], places=9)

    def test_snap_rejects_non_axis_rotation(self):
        angle = 0.3
        r = [[math.cos(angle), -math.sin(angle), 0.0],
             [math.sin(angle), math.cos(angle), 0.0],
             [0.0, 0.0, 1.0]]
        self.assertEqual(matching._snap_frame_rot(r), r)

    def test_snap_cleans_noisy_axis_swap(self):
        noisy = [[1.0000001, 1e-8, 0.0],
                 [-1e-8, 1e-7, -0.9999999],
                 [0.0, 1.0000002, 1e-9]]
        self.assertEqual(matching._snap_frame_rot(noisy), ROT_X_POS90)


if __name__ == "__main__":
    unittest.main()
