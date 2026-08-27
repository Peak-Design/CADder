# SPDX-License-Identifier: GPL-3.0-or-later
"""Pose-sync tests: matched geometry snaps onto the manifest's SolidWorks
transforms. Born from the flexible-twin STEP limitation (2026-08-23): a
flexed flexible subassembly and a rigid twin of the same document share ONE
internal layout in the STEP file, so the twin imports mis-posed — the
matcher still finds it (fuzzy step), and pose_sync moves it to where
SolidWorks actually had it.

No bpy — pose_sync degrades to plain Python like matching does."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from STEPper_NEXT.rig import matching, pose_sync  # noqa: E402
from STEPper_NEXT.ci.rig.test_matching import (FakeObj, identity4,  # noqa: E402
                                               make_manifest, translated)


def rot_z(deg, x=0.0, y=0.0, z=0.0):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [[c, -s, 0.0, x], [s, c, 0.0, y], [0.0, 0.0, 1.0, z],
            [0.0, 0.0, 0.0, 1.0]]


class PoseObj(FakeObj):
    """FakeObj plus the pose surface pose_sync touches."""

    def __init__(self, name, props, matrix, parent=None):
        super().__init__(name, props, matrix)
        self.parent = parent
        self.parent_type = "OBJECT"
        self.matrix_parent_inverse = identity4()
        self.matrix_basis = None   # written by sync; world NOT recomputed

    def __hash__(self):
        return id(self)


def close(a, b, tol=1e-9):
    return all(abs(a[i][j] - b[i][j]) <= tol for i in range(4) for j in range(4))


class MisPosedTwinTest(unittest.TestCase):
    """End-to-end with the real matcher: the twin's leaf sits at the WRONG
    pose (the STEP shared layout), matches through the fuzzy step, and sync
    moves it onto its manifest transform. The healthy leaf stays put."""

    def test_twin_moves_healthy_stays(self):
        healthy_t = rot_z(0.0, 0.05, 0.02, 0.028)
        twin_t = rot_z(45.0, 0.15, 0.02, 0.028)
        m = make_manifest([
            ("c001", "leaf", "asm/leaf", healthy_t),
            ("c002", "leaf", "asm/leaf", twin_t),
        ])
        root = PoseObj("asm", {"STEP_name": "asm", "STEP_uuid": 10,
                               "STEP_file": "asm.step"}, identity4())
        good = PoseObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       healthy_t)
        # The twin imported at the flexible instance's internal layout —
        # wrong spot AND wrong rotation.
        bad = PoseObj("leaf.001", {"STEP_name": "leaf", "STEP_uuid": 2,
                                   "STEP_parent": 10, "STEP_file": "asm.step"},
                      rot_z(80.0, 0.15, 0.09, 0.028))
        objs = [root, good, bad]
        report = matching.match(m, objects=objs)
        got = {e.component_id: e.object_name for e in report.matched}
        self.assertEqual({"c001": "leaf", "c002": "leaf.001"}, got)

        pr = pose_sync.sync(m, report, objects=objs)
        self.assertEqual(1, pr.already_ok)
        self.assertEqual(["leaf.001"], [n for n, _ in pr.moved])
        self.assertIsNone(good.matrix_basis)          # never touched
        self.assertTrue(close(bad.matrix_basis, twin_t))

    def test_move_distance_is_reported_in_metres(self):
        m = make_manifest([("c001", "leaf", None, translated(0.1, 0.0, 0.0))])
        obj = PoseObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 1,
                               "STEP_file": "asm.step"},
                      translated(0.1, 0.03, 0.04))
        report = matching.MatchReport(
            matched=[matching.MatchEntry("c001", "leaf", 5, "fuzzy")],
            frame_rows=matching.identity_frame())
        pr = pose_sync.sync(m, report, objects=[obj])
        self.assertAlmostEqual(0.05, pr.moved[0][1], places=9)


class RotatedFrameTest(unittest.TestCase):
    """A Y-up import: the scene frame is a +90° X rotation. The corrected
    pose must land in the SCENE frame (frame @ manifest transform), not at
    the raw manifest coordinates."""

    def test_target_is_under_the_scene_frame(self):
        frame = [[1.0, 0.0, 0.0, 0.0],
                 [0.0, 0.0, -1.0, 0.0],
                 [0.0, 1.0, 0.0, 0.0],
                 [0.0, 0.0, 0.0, 1.0]]
        comp_t = translated(0.0, 0.2, 0.0)
        m = make_manifest([("c001", "leaf", None, comp_t)])
        obj = PoseObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 1,
                               "STEP_file": "asm.step"}, identity4())
        report = matching.MatchReport(
            matched=[matching.MatchEntry("c001", "leaf", 5, "fuzzy")],
            frame_rows=frame)
        pose_sync.sync(m, report, objects=[obj])
        self.assertTrue(close(obj.matrix_basis, matching.apply_frame(frame, comp_t)))


class ParentedChildTest(unittest.TestCase):
    """TREE-mode import: the mis-posed leaf hangs under an occurrence empty
    that stays where it is. The child's new basis must compose with the
    parent's (unchanged) world matrix back to the manifest pose."""

    def test_basis_composes_through_the_parent(self):
        target = rot_z(30.0, 0.05, 0.0, 0.1)
        m = make_manifest([("c001", "leaf", None, target)])
        parent = PoseObj("sub", {"STEP_name": "sub", "STEP_uuid": 10,
                                 "STEP_file": "asm.step"},
                         rot_z(90.0, 0.02, 0.0, 0.0))
        child = PoseObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 1,
                                 "STEP_parent": 10, "STEP_file": "asm.step"},
                        translated(0.5, 0.5, 0.5), parent=parent)
        report = matching.MatchReport(
            matched=[matching.MatchEntry("c001", "leaf", 5, "fuzzy")],
            frame_rows=matching.identity_frame())
        pose_sync.sync(m, report, objects=[parent, child])
        world = matching.apply_frame(parent.matrix_world, child.matrix_basis)
        self.assertTrue(close(world, target, tol=1e-9))


class ScalePreservationTest(unittest.TestCase):
    """STEPper bakes STEP_applied_scale into the object matrix; snapping to
    the manifest's rigid transform must keep those column norms or the
    geometry shrinks by the import scale."""

    def test_import_scale_survives_the_move(self):
        m = make_manifest([("c001", "leaf", None, translated(0.1, 0.0, 0.0))])
        scaled = [[0.001, 0.0, 0.0, 0.4],
                  [0.0, 0.001, 0.0, 0.0],
                  [0.0, 0.0, 0.001, 0.0],
                  [0.0, 0.0, 0.0, 1.0]]
        obj = PoseObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 1,
                               "STEP_file": "asm.step"}, scaled)
        report = matching.MatchReport(
            matched=[matching.MatchEntry("c001", "leaf", 5, "fuzzy")],
            frame_rows=matching.identity_frame())
        pose_sync.sync(m, report, objects=[obj])
        b = obj.matrix_basis
        for c in range(3):
            n = math.sqrt(sum(b[r][c] ** 2 for r in range(3)))
            self.assertAlmostEqual(0.001, n, places=12)
        self.assertAlmostEqual(0.1, b[0][3], places=12)

    def test_mirrored_instance_is_skipped_not_flipped(self):
        m = make_manifest([("c001", "leaf", None, translated(0.1, 0.0, 0.0))])
        mirrored = [[-1.0, 0.0, 0.0, 0.4],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0]]
        obj = PoseObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 1,
                               "STEP_file": "asm.step"}, mirrored)
        report = matching.MatchReport(
            matched=[matching.MatchEntry("c001", "leaf", 5, "fuzzy")],
            frame_rows=matching.identity_frame())
        pr = pose_sync.sync(m, report, objects=[obj])
        self.assertEqual([], pr.moved)
        self.assertEqual(1, len(pr.skipped))
        self.assertIsNone(obj.matrix_basis)


if __name__ == "__main__":
    unittest.main()
