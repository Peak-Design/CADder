# SPDX-License-Identifier: GPL-3.0-or-later
"""Manifest parse/validation tests. No bpy â€” the parser must behave
identically under plain Python and inside Blender."""

import copy
import math
import os
import sys
import tempfile
import unittest

# The addons directory (the parent of the STEPper_NEXT repo root) makes
# "import STEPper_NEXT.rig" work from any checkout named STEPper_NEXT.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from STEPper_NEXT.rig import manifest  # noqa: E402
from STEPper_NEXT.rig.manifest import ManifestError  # noqa: E402


def identity4():
    return [[1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def base_manifest():
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.SwToBlender", "version": "0.1.0"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "asm.step", "ap": "AP214", "sha1": None,
                        "occurrence_matching": None},
        "components": [],
        "rigid_groups": [],
        "joints": [],
        "loops": [],
        "warnings": [],
    }


def component(cid, name):
    return {"id": cid, "sw_path": name + "-1", "step_name": name,
            "step_occurrence_path": "Asm/" + name + "-1",
            "transform": identity4()}


def hinge_manifest():
    data = base_manifest()
    data["components"] = [component("c001", "Base"), component("c002", "Arm")]
    data["rigid_groups"] = [
        {"id": "g000", "name": "base", "components": ["c001"],
         "grounded": True, "frame": identity4(), "bbox_diag": 0.3},
        {"id": "g001", "name": "arm", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.2},
    ]
    data["joints"] = [{
        "id": "j001", "type": "revolute",
        "parent_group": "g000", "child_group": "g001",
        "origin": [0.1, 0.0, 0.05],
        "axis": [0.0, 0.0, 1.0],
        "secondary_axis": [1.0, 0.0, 0.0],
        "limits": {"rotation": {"min": -0.5, "max": 1.0, "value_at_rest": 0.25},
                   "translation": None},
    }]
    return data


def four_bar_manifest():
    data = base_manifest()
    data["components"] = [component("c%03d" % i, n) for i, n in
                          enumerate(["Ground", "Crank", "Rocker", "Coupler"], 1)]
    data["rigid_groups"] = [
        {"id": "g000", "name": "ground", "components": ["c001"],
         "grounded": True, "frame": identity4(), "bbox_diag": 0.5},
        {"id": "g001", "name": "crank", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.1},
        {"id": "g002", "name": "rocker", "components": ["c003"],
         "grounded": False, "frame": None, "bbox_diag": 0.15},
        {"id": "g003", "name": "coupler", "components": ["c004"],
         "grounded": False, "frame": None, "bbox_diag": 0.2},
    ]

    def rev(jid, parent, child, origin):
        return {"id": jid, "type": "revolute",
                "parent_group": parent, "child_group": child,
                "origin": origin, "axis": [0.0, 0.0, 1.0],
                "secondary_axis": [1.0, 0.0, 0.0], "limits": None}

    # Mirrors the exporter's choices: j001 drives, so the cut sits just past
    # the crank (j003) and j004 is oriented rootward through the rocker.
    data["joints"] = [
        rev("j001", "g000", "g001", [0.0, 0.0, 0.0]),
        rev("j002", "g000", "g002", [0.3, 0.0, 0.0]),
        rev("j003", "g001", "g003", [0.0, 0.1, 0.0]),
        rev("j004", "g002", "g003", [0.3, 0.1, 0.0]),
    ]
    data["loops"] = [{
        "id": "L1",
        "member_joints": ["j001", "j002", "j003", "j004"],
        "closure_joint": "j003",
        "suggested_driver_joint": "j001",
        "planar": True,
        "plane_normal": [0.0, 0.0, 1.0],
    }]
    return data


def ram_manifest(closure_kind="aim_pair"):
    """A hydraulic ram working a clamp: the shape live ClampRig has
    twice over. Down the pin axis: the clamp pivots at the origin, the ram's
    bore at (1,0,0) and the rod's pin at (0,1,0), so the ram is sqrt(2) long
    and the corner at the clamp pivot is square."""
    data = base_manifest()
    data["components"] = [component("c%03d" % i, n) for i, n in
                          enumerate(["Body", "Clamp", "Barrel", "Rod"], 1)]
    data["rigid_groups"] = [
        {"id": "g000", "name": "body", "components": ["c001"],
         "grounded": True, "frame": identity4(), "bbox_diag": 1.0},
        {"id": "g001", "name": "clamp", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.4},
        {"id": "g002", "name": "barrel", "components": ["c003"],
         "grounded": False, "frame": None, "bbox_diag": 0.4},
        {"id": "g003", "name": "rod", "components": ["c004"],
         "grounded": False, "frame": None, "bbox_diag": 0.3},
    ]
    pin = [0.0, 0.0, 1.0]

    def rev(jid, parent, child, origin, jtype="revolute"):
        return {"id": jid, "type": jtype, "parent_group": parent,
                "child_group": child, "origin": origin, "axis": pin,
                "secondary_axis": [1.0, 0.0, 0.0], "limits": None}

    root2 = math.sqrt(2.0)
    data["joints"] = [
        rev("j001", "g000", "g001", [0.0, 0.0, 0.0]),
        rev("j002", "g000", "g002", [1.0, 0.0, 0.0]),
        {"id": "j003", "type": "prismatic",
         "parent_group": "g002", "child_group": "g003",
         "origin": [0.5, 0.5, 0.0],
         "axis": [-1.0 / root2, 1.0 / root2, 0.0],
         "secondary_axis": [0.0, 0.0, 1.0],
         "limits": {"rotation": None,
                    "translation": {"min": 0.3, "max": 0.8,
                                    "value_at_rest": 0.5}}},
        rev("j004", "g001", "g003", [0.0, 1.0, 0.0], jtype="cylindrical"),
    ]
    data["loops"] = [{
        "id": "L1",
        "member_joints": ["j001", "j002", "j003", "j004"],
        "closure_joint": "j003",
        "closure_kind": closure_kind,
        "suggested_driver_joint": "j001",
        "planar": True,
        "plane_normal": pin,
    }]
    return data


class TestParse(unittest.TestCase):

    def test_minimal_hinge(self):
        m = manifest.parse(hinge_manifest())
        self.assertEqual(m.manifest_version, "1.0.0")
        self.assertEqual(len(m.components), 2)
        self.assertEqual(len(m.rigid_groups), 2)
        self.assertEqual(m.joints[0].type, "revolute")
        self.assertEqual(m.joints[0].axis, (0.0, 0.0, 1.0))
        self.assertEqual(m.grounded_groups()[0].id, "g000")
        self.assertIsNone(m.joints[0].translation_limit)

    def test_limit_deltas(self):
        m = manifest.parse(hinge_manifest())
        lim = m.joints[0].rotation_limit
        self.assertAlmostEqual(lim.delta_min, -0.75)
        self.assertAlmostEqual(lim.delta_max, 0.75)

    def test_path_joint_parses(self):
        data = hinge_manifest()
        data["joints"][0] = {
            "id": "j001", "type": "path",
            "parent_group": "g000", "child_group": "g001",
            "origin": [0.02, 0.0, 0.02],
            "axis": [1.0, 0.0, 0.0],
            "secondary_axis": [0.0, 0.0, 1.0],
            "limits": None,
            "path": {"points": [[0.0, 0.0, 0.02], [0.05, 0.0, 0.02],
                                [0.1, 0.02, 0.02]], "closed": False},
        }
        m = manifest.parse(data)
        j = m.joints[0]
        self.assertEqual(j.type, "path")
        self.assertEqual(len(j.path_points), 3)
        self.assertEqual(j.path_points[1], (0.05, 0.0, 0.02))
        self.assertFalse(j.path_closed)

    def test_path_joint_without_points_is_rejected(self):
        data = hinge_manifest()
        data["joints"][0]["type"] = "path"
        with self.assertRaises(ManifestError):
            manifest.parse(data)

    def test_four_bar(self):
        m = manifest.parse(four_bar_manifest())
        self.assertEqual(len(m.loops), 1)
        self.assertEqual(m.loops[0].closure_joint, "j003")
        self.assertTrue(m.loops[0].planar)

    def test_wrong_major_version(self):
        data = hinge_manifest()
        data["manifest_version"] = "2.0.0"
        with self.assertRaisesRegex(ManifestError, "major 2"):
            manifest.parse(data)

    def test_minor_version_and_unknown_fields_accepted(self):
        data = hinge_manifest()
        data["manifest_version"] = "1.7.3"
        data["future_section"] = {"anything": 1}
        data["joints"][0]["future_field"] = "x"
        m = manifest.parse(data)
        self.assertEqual(m.manifest_version, "1.7.3")

    def test_dangling_component_in_group(self):
        data = hinge_manifest()
        data["rigid_groups"][1]["components"] = ["c999"]
        with self.assertRaisesRegex(ManifestError, "c999"):
            manifest.parse(data)

    def test_dangling_joint_group(self):
        data = hinge_manifest()
        data["joints"][0]["child_group"] = "g999"
        with self.assertRaisesRegex(ManifestError, "g999"):
            manifest.parse(data)

    def test_axis_without_secondary(self):
        data = hinge_manifest()
        data["joints"][0]["secondary_axis"] = None
        with self.assertRaisesRegex(ManifestError, "secondary_axis"):
            manifest.parse(data)

    def test_wrong_units(self):
        data = hinge_manifest()
        data["units"] = {"length": "millimeter", "angle": "radian"}
        with self.assertRaisesRegex(ManifestError, "units"):
            manifest.parse(data)

    def test_wrong_frame(self):
        data = hinge_manifest()
        data["frame"]["up_axis"] = "Y"
        with self.assertRaisesRegex(ManifestError, "frame"):
            manifest.parse(data)

    def test_limit_min_over_max(self):
        data = hinge_manifest()
        data["joints"][0]["limits"]["rotation"] = {
            "min": 1.0, "max": -1.0, "value_at_rest": 0.0}
        with self.assertRaisesRegex(ManifestError, "exceeds"):
            manifest.parse(data)

    def test_closure_outside_members(self):
        data = four_bar_manifest()
        data["loops"][0]["closure_joint"] = "j999"
        with self.assertRaises(ManifestError):
            manifest.parse(data)

    def test_dangling_coupling_driver(self):
        data = hinge_manifest()
        data["joints"][0]["coupling"] = {"kind": "gear", "driver_joint": "j999",
                                         "ratio": 2.0}
        with self.assertRaisesRegex(ManifestError, "j999"):
            manifest.parse(data)

    def test_duplicate_ids(self):
        data = hinge_manifest()
        data["components"].append(copy.deepcopy(data["components"][0]))
        with self.assertRaisesRegex(ManifestError, "duplicate"):
            manifest.parse(data)

    def _mirror_manifest(self, scope=None):
        data = hinge_manifest()
        coupling = {"kind": "mirror", "driver_joint": "j001",
                    "mirror_plane": {"point": [0.0, 0.0, 0.0],
                                     "normal": [0.0, 1.0, 0.0]}}
        if scope is not None:
            coupling["mirror_scope"] = scope
        data["joints"][0]["coupling"] = coupling
        data["joints"][0]["id"] = "j002"
        data["joints"].insert(0, dict(data["joints"][0], id="j001",
                                      coupling=None))
        return data

    def test_mirror_scope_defaults_to_plane(self):
        """A manifest written before mirror features existed carries no
        scope, and the reading it was written under is the plane one."""
        m = manifest.parse(self._mirror_manifest())
        self.assertEqual(m.joints[1].coupling.mirror_scope, "plane")

    def test_mirror_scope_rigid_is_kept(self):
        m = manifest.parse(self._mirror_manifest("rigid"))
        self.assertEqual(m.joints[1].coupling.mirror_scope, "rigid")

    def test_unknown_mirror_scope_is_rejected(self):
        with self.assertRaisesRegex(ManifestError, "mirror_scope"):
            manifest.parse(self._mirror_manifest("halfway"))

    def test_load_rejects_bad_json(self):
        fd, path = tempfile.mkstemp(suffix=".rig.json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaisesRegex(ManifestError, "not valid JSON"):
                manifest.load(path)
        finally:
            os.unlink(path)

    def test_load_records_source_path(self):
        import json
        fd, path = tempfile.mkstemp(suffix=".rig.json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(hinge_manifest(), f)
            m = manifest.load(path)
            self.assertEqual(m.source_path, path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
