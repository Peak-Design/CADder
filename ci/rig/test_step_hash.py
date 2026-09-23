# SPDX-License-Identifier: GPL-3.0-or-later
"""The STEP file a manifest names must be the one it was written for.

The manifest carries the SHA-1 of its STEP file, to tell a stale pair.
Nothing read it: a manifest loaded beside a STEP file that was written
again later was matched against it without a word. Matching now hashes the
file first and says so when it differs. No bpy.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import manifest, matching  # noqa: E402
from CADder.ci.rig.test_matching import FakeObj, translated  # noqa: E402


def write_pair(folder, step_bytes, sha1):
    step = os.path.join(folder, "asm.step")
    with open(step, "wb") as fh:
        fh.write(step_bytes)
    path = os.path.join(folder, "asm.rig.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "manifest_version": "1.0.0",
            "generator": {"name": "test", "version": "0"},
            "units": {"length": "meter", "angle": "radian"},
            "frame": {"handedness": "right", "up_axis": "Z",
                      "transform_convention": "row_major_4x4_global"},
            "step_export": {"file": "asm.step", "ap": "AP214", "sha1": sha1,
                            "occurrence_matching": None},
            "components": [
                {"id": "c001", "sw_path": "leaf-1", "step_name": "leaf",
                 "step_occurrence_path": None,
                 "transform": translated(0.1, 0.0, 0.0)}],
            "rigid_groups": [
                {"id": "g000", "name": "leaf", "components": ["c001"],
                 "grounded": True}],
            "joints": [], "loops": [], "warnings": [],
        }, fh)
    return manifest.load(path), step


def leaf():
    return FakeObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 1,
                            "STEP_file": "asm.step"},
                   translated(0.1, 0.0, 0.0))


class StepHashTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="step_hash_")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_step_file_written_again_is_reported(self):
        written = b"ISO-10303-21; the export the manifest was made from"
        m, step = write_pair(self.folder, b"ISO-10303-21; a later export",
                             hashlib.sha1(written).hexdigest())
        self.assertIsNotNone(manifest.stale_step(m))
        report = matching.match(m, objects=[leaf()], collections=[])
        self.assertTrue(any("asm.step" in n and "SHA-1" in n
                            for n in report.notes), report.notes)

    def test_the_step_file_it_was_written_for_passes(self):
        data = b"ISO-10303-21; the export"
        m, _step = write_pair(self.folder, data,
                              hashlib.sha1(data).hexdigest().upper())
        self.assertIsNone(manifest.stale_step(m))
        report = matching.match(m, objects=[leaf()], collections=[])
        self.assertEqual(report.notes, [])

    def test_no_hash_means_no_check(self):
        # A manifest for the direct link: no STEP was written with it.
        m, step = write_pair(self.folder, b"anything", None)
        os.remove(step)
        self.assertIsNone(manifest.stale_step(m))
        report = matching.match(m, objects=[leaf()], collections=[])
        self.assertEqual(report.notes, [])

    def test_a_missing_step_file_is_not_a_stale_one(self):
        m, step = write_pair(self.folder, b"x", hashlib.sha1(b"x").hexdigest())
        os.remove(step)
        self.assertIsNone(manifest.stale_step(m))


if __name__ == "__main__":
    unittest.main()
