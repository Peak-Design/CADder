# SPDX-License-Identifier: GPL-3.0-or-later
"""Matching hygiene tests, born from the live corpus 07 round three
(2026-08-22), a long-lived test scene: stale RIG_* tags from earlier
manifests, imports of several STEP files side by side, twin subassembly
instances sharing one occurrence path: reported "unmatched: c004" for a
component whose object sat exactly where the manifest said. Each test here
pins one of the hygiene rules that make the cascade survive such scenes.

No bpy: matching.py degrades to plain Python and the fake objects provide
the tiny surface it reads."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import manifest, matching  # noqa: E402


def identity4():
    return [[1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def translated(x, y, z):
    m = identity4()
    m[0][3], m[1][3], m[2][3] = x, y, z
    return m


def turned(x):
    """Turned 180 degrees about Z and moved to x: a mirrored instance, which
    is what puts an occurrence's parts in the opposite order along an axis
    from the order they are walked in."""
    return [[-1.0, 0.0, 0.0, x],
            [0.0, -1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]




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

    def __delitem__(self, key):
        del self._props[key]


class FakeCollection:
    """The collection surface matching.py touches: a name, its children and
    the objects linked directly into it. bpy gives no parent, which is why
    matching inverts the children map to get one."""

    def __init__(self, name, objects=(), children=(), props=None):
        self.name = name
        self.objects = list(objects)
        self.children = list(children)
        self._props = dict(props or {})

    def get(self, key, default=None):
        return self._props.get(key, default)


def make_manifest(comps, step_file="asm.step"):
    """comps: list of (cid, step_name, occurrence_path, transform) or the
    same with a fifth entry, subassembly_solving: "rigid" or "flexible" for
    a component that IS a subassembly occurrence, absent for a part."""
    comps = [c if len(c) == 5 else tuple(c) + (None,) for c in comps]
    return manifest.parse({
        "manifest_version": "1.0.0",
        "generator": {"name": "test", "version": "0"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": step_file},
        "components": [
            {
                "id": cid,
                "sw_path": "asm/" + name + "-1",
                "step_name": name,
                "step_occurrence_path": path,
                "transform": transform,
                "subassembly_solving": solving,
            }
            for cid, name, path, transform, solving in comps
        ],
        "rigid_groups": [
            {"id": "g%03d" % i, "name": name, "components": [cid], "grounded": i == 0}
            for i, (cid, name, _, _, _) in enumerate(comps)
        ],
        "joints": [],
        "loops": [],
        "warnings": [],
    })


class TwinPathsTest(unittest.TestCase):
    """Two instances of one subassembly rebuild the SAME product-name
    occurrence path: the exact-path step sees two hits for each twin
    component. That ambiguity must fall through to the transform step, not
    kill the component (the old cascade dropped it on the spot)."""

    def test_twin_paths_disambiguate_by_transform(self):
        m = make_manifest([
            ("c001", "leaf", "asm/leaf", translated(0.05, 0.02, 0.028)),
            ("c002", "leaf", "asm/leaf", translated(0.15, 0.02, 0.028)),
        ])
        root = FakeObj("asm", {"STEP_name": "asm", "STEP_uuid": 10,
                               "STEP_file": "asm.step"}, identity4())
        objs = [
            root,
            FakeObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 1,
                             "STEP_parent": 10, "STEP_file": "asm.step"},
                    translated(0.05, 0.02, 0.028)),
            FakeObj("leaf.001", {"STEP_name": "leaf", "STEP_uuid": 2,
                                 "STEP_parent": 10, "STEP_file": "asm.step"},
                    translated(0.15, 0.02, 0.028)),
        ]
        report = matching.match(m, objects=objs)
        got = {e.component_id: e.object_name for e in report.matched}
        self.assertEqual({"c001": "leaf", "c002": "leaf.001"}, got)
        self.assertEqual([], report.ambiguous)
        self.assertEqual([], report.unmatched)


class StalePreseedTest(unittest.TestCase):
    """A RIG_component_id tag left by an earlier manifest can point at an
    object the current component no longer describes. With two healthy
    anchors outvoting it on the frame, the stale claim is reverted and the
    component re-matches to the object that actually sits at its
    transform."""

    def test_stale_preseed_reverts_to_transform_match(self):
        m = make_manifest([
            ("c001", "base", None, identity4()),
            ("c002", "arm", None, translated(0.1, 0.0, 0.0)),
            ("c003", "lid", None, translated(0.0, 0.1, 0.0)),
        ])
        stale = FakeObj("base.stale",
                        {"STEP_name": "base", "STEP_uuid": 1,
                         "STEP_file": "asm.step", "RIG_component_id": "c001",
                         "RIG_group": "g000"},
                        translated(0.5, 0.5, 0.0))
        fresh = FakeObj("base",
                        {"STEP_name": "base", "STEP_uuid": 2,
                         "STEP_file": "asm.step"},
                        identity4())
        arm = FakeObj("arm",
                      {"STEP_name": "arm", "STEP_uuid": 3,
                       "STEP_file": "asm.step", "RIG_component_id": "c002",
                       "RIG_group": "g001"},
                      translated(0.1, 0.0, 0.0))
        lid = FakeObj("lid",
                      {"STEP_name": "lid", "STEP_uuid": 4,
                       "STEP_file": "asm.step", "RIG_component_id": "c003",
                       "RIG_group": "g002"},
                      translated(0.0, 0.1, 0.0))
        report = matching.match(m, objects=[stale, fresh, arm, lid])
        got = {e.component_id: e.object_name for e in report.matched}
        self.assertEqual("base", got["c001"])
        self.assertEqual("arm", got["c002"])
        self.assertEqual("lid", got["c003"])
        self.assertIn("base.stale", report.unclaimed_objects)
        self.assertIsNone(stale.get("RIG_component_id"))
        self.assertIsNone(stale.get("RIG_group"))

    def test_renumbered_ids_do_not_block_matching(self):
        """Component ids are positional. A re-export that walks the
        assembly in another order renumbers every one of them, so EVERY
        tag left by the previous match names a component its object never
        was. Live TongRig (2026-09-14): the manifest re-sent against the
        standing import matched nothing. The wrong pre-seeds emptied the
        pool before the path steps ran, then voted a frame from wrong
        pairs, and the transform steps had nothing to agree with.

        Five parts, two of them twins of one product, every tag shifted by
        one. Everything must match, by path, and every group tag must be
        this manifest's."""
        rows = {
            "base": identity4(),
            "arm": translated(0.1, 0.0, 0.0),
            "lid": translated(0.0, 0.1, 0.0),
            "pin": translated(0.2, 0.0, 0.0),
            "pin.001": translated(0.3, 0.0, 0.0),
        }
        m = make_manifest([
            ("c001", "base", "asm/base", rows["base"]),
            ("c002", "arm", "asm/arm", rows["arm"]),
            ("c003", "lid", "asm/lid", rows["lid"]),
            ("c004", "pin", "asm/pin", rows["pin"]),
            ("c005", "pin", "asm/pin", rows["pin.001"]),
        ])
        root = FakeObj("asm", {"STEP_name": "asm", "STEP_uuid": 10,
                               "STEP_file": "asm.step"}, identity4())
        # The previous manifest numbered them arm, lid, pin, pin, base.
        was = {"base": "c005", "arm": "c001", "lid": "c002",
               "pin": "c003", "pin.001": "c004"}
        objs = [root]
        for i, name in enumerate(rows):
            objs.append(FakeObj(name, {
                "STEP_name": name.split(".")[0], "STEP_uuid": i + 1,
                "STEP_parent": 10, "STEP_file": "asm.step",
                "RIG_component_id": was[name], "RIG_group": "gXXX",
            }, rows[name]))
        report = matching.match(m, objects=objs)
        got = {e.component_id: e.object_name for e in report.matched}
        self.assertEqual({"c001": "base", "c002": "arm", "c003": "lid",
                          "c004": "pin", "c005": "pin.001"}, got)
        self.assertEqual([], report.unmatched)
        self.assertEqual([], report.ambiguous)
        for obj in objs[1:]:
            self.assertEqual(got_id := obj.get("RIG_component_id"),
                             {v: k for k, v in got.items()}[obj.name])
            self.assertEqual("g%03d" % (int(got_id[1:]) - 1),
                             obj.get("RIG_group"))


class DottedProductNamesTest(unittest.TestCase):
    """Product numbers with dotted suffixes, in a tree-collection import.
    Live TongRig (2026-09-14): the hydraulic cylinder is "42S TC100.2",
    its body "42S TC100.1.02", and each holds a nested subassembly named
    one dot deeper. Stripping every dotted number as a Blender duplicate
    suffix folded all of them into one label, the manifest's single
    occurrence met four collections, and the cylinder rode the ground.

    Blender's suffix is always at least three digits, and the importer now
    stamps the CAD name on each node collection; both are covered, with
    and without the stamp."""

    def _scene(self, stamped):
        m = make_manifest([
            ("c001", "base", "asm/base", identity4()),
            ("c002", "42S TC100.2", "asm/42S TC100.2",
             translated(0.1, 0.0, 0.0), "rigid"),
            ("c003", "42S TC100.1.02", "asm/42S TC100.1.02",
             translated(0.5, 0.0, 0.0), "rigid"),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        rod = FakeObj("rod", {"STEP_name": "rod", "STEP_uuid": 3,
                              "STEP_parent": 2, "STEP_file": "asm.step"},
                      translated(0.15, 0.0, 0.0))
        piston = FakeObj("piston", {"STEP_name": "piston", "STEP_uuid": 5,
                                    "STEP_parent": 4, "STEP_file": "asm.step"},
                         translated(0.2, 0.0, 0.0))
        barrel = FakeObj("barrel", {"STEP_name": "barrel", "STEP_uuid": 7,
                                    "STEP_parent": 6, "STEP_file": "asm.step"},
                         translated(0.55, 0.0, 0.0))
        gland = FakeObj("gland", {"STEP_name": "gland", "STEP_uuid": 9,
                                  "STEP_parent": 8, "STEP_file": "asm.step"},
                        translated(0.6, 0.0, 0.0))

        def col(name, **kw):
            props = {"STEP_name": name} if stamped else {}
            return FakeCollection(name, props=props, **kw)

        rod_sub = col("42S TC100.2.01", objects=[piston])
        cyl_rod = col("42S TC100.2", objects=[rod], children=[rod_sub])
        body_sub = col("42S TC100.1.02.01", objects=[gland])
        cyl_body = col("42S TC100.1.02", objects=[barrel], children=[body_sub])
        asm = col("asm", objects=[base], children=[cyl_rod, cyl_body])
        root = FakeCollection("asm.hierarchy", children=[asm])
        return m, [base, rod, piston, barrel, gland], [
            root, asm, cyl_rod, rod_sub, cyl_body, body_sub]

    def _check(self, stamped):
        m, objs, cols = self._scene(stamped)
        report = matching.match(m, objects=objs, collections=cols)
        self.assertEqual([], report.unmatched, report.notes)
        got = {e.component_id: sorted(e.object_names) for e in report.matched}
        self.assertEqual(["piston", "rod"], got["c002"])
        self.assertEqual(["barrel", "gland"], got["c003"])
        for name in ("rod", "piston"):
            self.assertEqual("g001", next(o for o in objs if o.name == name).get("RIG_group"))
        for name in ("barrel", "gland"):
            self.assertEqual("g002", next(o for o in objs if o.name == name).get("RIG_group"))

    def test_stamped_collections(self):
        self._check(stamped=True)

    def test_unstamped_collections(self):
        self._check(stamped=False)


class ForeignFileTest(unittest.TestCase):
    """A scene holding imports of several STEP files: objects from OTHER
    files must not compete, even when they sit at the same transforms (two
    exports of the same assembly do exactly that)."""

    def test_other_files_objects_are_ignored(self):
        m = make_manifest([
            ("c001", "base", None, identity4()),
        ], step_file="asm.step")
        mine = FakeObj("base",
                       {"STEP_name": "base", "STEP_uuid": 1,
                        "STEP_file": "C:/exports/asm.step"},
                       identity4())
        other = FakeObj("base.001",
                        {"STEP_name": "base", "STEP_uuid": 1,
                         "STEP_file": "C:/exports/other.step"},
                        identity4())
        report = matching.match(m, objects=[mine, other])
        got = {e.component_id: e.object_name for e in report.matched}
        self.assertEqual({"c001": "base"}, got)
        self.assertEqual([], report.ambiguous)
        self.assertEqual([], report.unmatched)


class CollectionHierarchyTest(unittest.TestCase):
    """A rigid subassembly is ONE body, so the manifest names the assembly
    occurrence rather than its parts. That node carries no shape, so the
    import modes that build collections give it no object at all: it exists
    only as a COLLECTION, and the body it stands for is every part below it.

    Live ClampRig (2026-08-24) came in as a tree collection: 53 of 122
    components matched and only the loose parts moved with the rig."""

    def _scene(self, with_empties):
        m = make_manifest([
            ("c001", "base", "asm/base", identity4()),
            ("c002", "sub", "asm/sub", translated(0.1, 0.0, 0.0), "rigid"),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        part = FakeObj("part", {"STEP_name": "part", "STEP_uuid": 3,
                                "STEP_parent": 2, "STEP_file": "asm.step"},
                       translated(0.15, 0.0, 0.0))
        objs = [base, part]
        sub = FakeCollection("sub", objects=[part])
        asm = FakeCollection("asm", objects=[base], children=[sub])
        root = FakeCollection("asm.hierarchy", children=[asm])
        cols = [root, asm, sub]
        if with_empties:
            objs[:0] = [
                FakeObj("asm", {"STEP_name": "asm", "STEP_uuid": 10,
                                "STEP_parent": -1, "STEP_file": "asm.step"},
                        identity4()),
                FakeObj("sub", {"STEP_name": "sub", "STEP_uuid": 2,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                        translated(0.1, 0.0, 0.0)),
            ]
            cols = []
        return m, objs, cols

    def test_empties_import_matches_the_subassembly_occurrence(self):
        m, objs, cols = self._scene(with_empties=True)
        report = matching.match(m, objects=objs, collections=cols)
        got = {e.component_id: e.object_name for e in report.matched}
        self.assertEqual({"c001": "base", "c002": "sub"}, got)
        self.assertEqual([], report.unmatched)
        self.assertIsNone(report.hint)

    def test_collection_import_claims_the_parts_below_the_collection(self):
        m, objs, cols = self._scene(with_empties=False)
        report = matching.match(m, objects=objs, collections=cols)
        self.assertEqual([], report.unmatched)
        self.assertIsNone(report.hint)
        by_cid = {e.component_id: e for e in report.matched}
        # One entry per COMPONENT, whatever it owns: the stale-tag sweep and
        # the scene-frame vote both index the report by component id.
        self.assertEqual(2, len(report.matched))
        self.assertIsNone(by_cid["c001"].collection_name)
        self.assertEqual("sub", by_cid["c002"].collection_name)
        self.assertEqual(["part"], by_cid["c002"].object_names)
        # What parenting actually reads is the tag on the object.
        part = [o for o in objs if o.name == "part"][0]
        self.assertEqual("g001", part.get("RIG_group"))
        # ...but NOT RIG_component_id, which is the retessellate key: giving
        # a part of a subassembly that id makes SolidWorks send back the
        # whole subassembly's mesh for it.
        self.assertIsNone(part.get("RIG_component_id"))
        self.assertEqual("c002", part.get("RIG_component_of"))

    def test_a_collection_body_is_left_alone_by_pose_sync(self):
        from CADder.rig import pose_sync
        m, objs, cols = self._scene(with_empties=False)
        report = matching.match(m, objects=objs, collections=cols)
        out = pose_sync.sync(m, report, objects=objs)
        self.assertEqual([], out.moved)
        # NOT "skipped": nothing declined to move, there was simply no
        # object carrying the occurrence pose to compare against.
        self.assertEqual([], out.skipped)
        self.assertEqual(["sub"], [name for name, _ in out.collections])


class TwinCollectionsTest(unittest.TestCase):
    """Two occurrences of one subassembly are two collections with the same
    CAD name, told apart by Blender's ".001" only. The manifest's transform
    says where each occurrence sits, so the parts nearest a component's own
    frame are that component's."""

    def _scene(self):
        m = make_manifest([
            ("c001", "base", "asm/base", identity4()),
            ("c002", "sub", "asm/sub", identity4(), "rigid"),
            ("c003", "sub", "asm/sub", translated(1.0, 0.0, 0.0), "rigid"),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        # Both occurrences hold the part at the same place INSIDE themselves,
        # which is what makes them the same product.
        left = FakeObj("part", {"STEP_name": "part", "STEP_uuid": 3,
                                "STEP_parent": 2, "STEP_file": "asm.step"},
                       translated(0.1, 0.0, 0.0))
        right = FakeObj("part.001", {"STEP_name": "part", "STEP_uuid": 5,
                                     "STEP_parent": 4, "STEP_file": "asm.step"},
                        translated(1.1, 0.0, 0.0))
        sub_a = FakeCollection("sub", objects=[left])
        sub_b = FakeCollection("sub.001", objects=[right])
        asm = FakeCollection("asm", objects=[base], children=[sub_a, sub_b])
        root = FakeCollection("asm.hierarchy", children=[asm])
        return m, [base, left, right], [root, asm, sub_a, sub_b]

    def test_each_occurrence_goes_to_the_component_it_sits_at(self):
        m, objs, cols = self._scene()
        report = matching.match(m, objects=objs, collections=cols)
        self.assertEqual([], report.unmatched)
        owned = {e.component_id: e.object_names for e in report.matched}
        self.assertEqual(["part"], owned["c002"])
        self.assertEqual(["part.001"], owned["c003"])
        self.assertEqual([], report.notes)

    def test_a_twin_laid_out_differently_is_claimed_but_flagged(self):
        m, objs, cols = self._scene()
        # Move one occurrence's part somewhere the product's own layout does
        # not put it: still nearest its component, so still claimed, but the
        # evidence is thinner and the report has to say so.
        right = [o for o in objs if o.name == "part.001"][0]
        right.matrix_world = translated(1.3, 0.0, 0.0)
        report = matching.match(m, objects=objs, collections=cols)
        owned = {e.component_id: e.object_names for e in report.matched}
        self.assertEqual(["part.001"], owned["c003"])
        self.assertTrue(any("do not sit the same way" in n
                            for n in report.notes), report.notes)

    def test_corresponding_parts_are_compared_not_nearest_ones(self):
        """Two parts of one name inside each occurrence, and the second
        occurrence turned 180 degrees, so the order the parts sit in along
        the world axes is the REVERSE of the order they are walked in. The
        layouts still have to agree: comparing them in position order pairs
        a part of one occurrence against a different part of the other and
        reports a product as laid out unlike itself (live ClampRig,
        2026-08-24: a 98-part module)."""
        m = make_manifest([
            ("c001", "base", "asm/base", identity4()),
            ("c002", "sub", "asm/sub", identity4(), "rigid"),
            ("c003", "sub", "asm/sub", turned(1.0), "rigid"),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        # Walk order 3, 4 sits at x = 0.1, 0.2 ...
        a1 = FakeObj("part", {"STEP_name": "part", "STEP_uuid": 3,
                              "STEP_parent": 2, "STEP_file": "asm.step"},
                     translated(0.1, 0.0, 0.0))
        a2 = FakeObj("part.001", {"STEP_name": "part", "STEP_uuid": 4,
                                  "STEP_parent": 2, "STEP_file": "asm.step"},
                     translated(0.2, 0.0, 0.0))
        # ... and walk order 5, 6 at x = 0.9, 0.8: same layout, opposite
        # order on the axis.
        b1 = FakeObj("part.002", {"STEP_name": "part", "STEP_uuid": 5,
                                  "STEP_parent": 4, "STEP_file": "asm.step"},
                     turned(0.9))
        b2 = FakeObj("part.003", {"STEP_name": "part", "STEP_uuid": 6,
                                  "STEP_parent": 4, "STEP_file": "asm.step"},
                     turned(0.8))
        sub_a = FakeCollection("sub", objects=[a1, a2])
        sub_b = FakeCollection("sub.001", objects=[b1, b2])
        asm = FakeCollection("asm", objects=[base], children=[sub_a, sub_b])
        root = FakeCollection("asm.hierarchy", children=[asm])

        report = matching.match(m, objects=[base, a1, a2, b1, b2],
                                collections=[root, asm, sub_a, sub_b])
        self.assertEqual([], report.unmatched)
        owned = {e.component_id: e.object_names for e in report.matched}
        self.assertEqual(["part", "part.001"], owned["c002"])
        self.assertEqual(["part.002", "part.003"], owned["c003"])
        self.assertEqual([], report.notes)


class FlexibleOccurrenceTest(unittest.TestCase):
    """A flexible subassembly is walked, so every part inside it is a
    component in its own right and usually in another rigid group. It must be
    PAIRED (that is what tells its rigid twin's collection from its own)
    but never expanded, or it claims parts that belong to those components.

    Live corpus 07 flexible-sub1 (2026-08-24): two instances of one hinge,
    one flexible and one rigid, sharing an occurrence path."""

    def test_a_flexible_occurrence_claims_none_of_its_parts(self):
        m = make_manifest([
            ("c001", "base", "asm/base", identity4()),
            # The flexible instance, and its two parts as components.
            ("c002", "hinge", "asm/hinge", identity4(), "flexible"),
            ("c003", "leaf", "asm/hinge/leaf", translated(0.1, 0.0, 0.0)),
            # The rigid twin: one body, owns both its parts.
            ("c004", "hinge", "asm/hinge", translated(1.0, 0.0, 0.0), "rigid"),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        leaf = FakeObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 3,
                                "STEP_parent": 2, "STEP_file": "asm.step"},
                       translated(0.1, 0.0, 0.0))
        twin = FakeObj("leaf.001", {"STEP_name": "leaf", "STEP_uuid": 5,
                                    "STEP_parent": 4, "STEP_file": "asm.step"},
                       translated(1.1, 0.0, 0.0))
        flex = FakeCollection("hinge", objects=[leaf])
        rigid = FakeCollection("hinge.001", objects=[twin])
        asm = FakeCollection("asm", objects=[base], children=[flex, rigid])
        root = FakeCollection("asm.hierarchy", children=[asm])

        report = matching.match(m, objects=[base, leaf, twin],
                                collections=[root, asm, flex, rigid])
        self.assertEqual([], report.unmatched)
        owned = {e.component_id: e.object_names for e in report.matched}
        self.assertEqual([], owned["c002"])          # flexible: owns nothing
        self.assertEqual(["leaf"], owned["c003"])    # its part is its own
        self.assertEqual(["leaf.001"], owned["c004"])
        # ...and the part of the flexible one keeps ITS group, not the
        # flexible container's.
        self.assertEqual("g002", leaf.get("RIG_group"))
        self.assertEqual("g003", twin.get("RIG_group"))


class UnnamedProductsTest(unittest.TestCase):
    """A STEP file whose product labels are blank gives every component the
    same useless occurrence path: live corpus 07 flexible-sub2 spells all
    five of them "flexible-sub2/ ". The occurrence's own name is then the
    only join left, and the collection still carries it."""

    def test_a_blank_occurrence_path_falls_back_to_the_name(self):
        m = make_manifest([
            ("c001", "base", "asm/ ", identity4()),
            ("c002", "sub", "asm/ ", translated(1.0, 0.0, 0.0), "rigid"),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        part = FakeObj("part", {"STEP_name": "part", "STEP_uuid": 3,
                                "STEP_parent": 2, "STEP_file": "asm.step"},
                       translated(1.1, 0.0, 0.0))
        sub_col = FakeCollection("sub", objects=[part])
        asm = FakeCollection("asm", objects=[base], children=[sub_col])
        root = FakeCollection("asm.hierarchy", children=[asm])

        report = matching.match(m, objects=[base, part],
                                collections=[root, asm, sub_col])
        owned = {e.component_id: e.object_names for e in report.matched}
        self.assertEqual(["part"], owned.get("c002"))
        self.assertEqual("g001", part.get("RIG_group"))


class StaleMemberTagTest(unittest.TestCase):
    """A part claimed as a MEMBER of a subassembly body carries RIG_group but
    no RIG_component_id, so neither the pre-seed nor the stale-tag sweep can
    see it on a later run. Group ids are positional: a re-export that drops
    a component renumbers them, so a tag that survives names a DIFFERENT
    body, and parenting reads the raw tag. Out of the rig is recoverable. In
    the wrong rigid group is not."""

    def test_a_member_this_run_did_not_reclaim_loses_its_group(self):
        m = make_manifest([
            ("c001", "base", "asm/base", identity4()),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        # Left over from a previous run against a manifest that had a
        # subassembly here. Nothing in this one claims it.
        orphan = FakeObj("leaf", {"STEP_name": "leaf", "STEP_uuid": 3,
                                  "STEP_parent": 2, "STEP_file": "asm.step",
                                  "RIG_component_of": "c009",
                                  "RIG_group": "g007"},
                         translated(5.0, 0.0, 0.0))

        report = matching.match(m, objects=[base, orphan])

        self.assertEqual(["leaf"], report.unclaimed_objects)
        self.assertIsNone(orphan.get("RIG_group"))
        self.assertIsNone(orphan.get("RIG_component_of"))

    def test_a_member_reclaimed_this_run_keeps_its_group(self):
        m = make_manifest([
            ("c001", "base", "asm/base", identity4()),
            ("c002", "sub", "asm/sub", translated(0.1, 0.0, 0.0), "rigid"),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        part = FakeObj("part", {"STEP_name": "part", "STEP_uuid": 3,
                                "STEP_parent": 2, "STEP_file": "asm.step",
                                "RIG_component_of": "c002",
                                "RIG_group": "g001"},
                       translated(0.15, 0.0, 0.0))
        sub_col = FakeCollection("sub", objects=[part])
        asm = FakeCollection("asm", objects=[base], children=[sub_col])
        root = FakeCollection("asm.hierarchy", children=[asm])

        matching.match(m, objects=[base, part],
                       collections=[root, asm, sub_col])

        self.assertEqual("g001", part.get("RIG_group"))
        self.assertEqual("c002", part.get("RIG_component_of"))


class NestedComponentsTest(unittest.TestCase):
    """The manifest nests components inside one another: live
    ClampRig's ram rod sits inside the ram barrel and belongs to a
    different rigid group. The inner one must take its parts before the
    outer one sweeps up what is left."""

    def test_the_inner_component_takes_its_own_parts(self):
        m = make_manifest([
            ("c001", "base", "asm/base", identity4()),
            ("c002", "sub", "asm/sub", identity4(), "rigid"),
            ("c003", "inner", "asm/sub/inner", translated(0.2, 0.0, 0.0),
             "rigid"),
        ])
        base = FakeObj("base", {"STEP_name": "base", "STEP_uuid": 1,
                                "STEP_parent": 10, "STEP_file": "asm.step"},
                       identity4())
        shell = FakeObj("shell", {"STEP_name": "shell", "STEP_uuid": 3,
                                  "STEP_parent": 2, "STEP_file": "asm.step"},
                        translated(0.1, 0.0, 0.0))
        core = FakeObj("core", {"STEP_name": "core", "STEP_uuid": 5,
                                "STEP_parent": 4, "STEP_file": "asm.step"},
                       translated(0.25, 0.0, 0.0))
        inner = FakeCollection("inner", objects=[core])
        sub = FakeCollection("sub", objects=[shell], children=[inner])
        asm = FakeCollection("asm", objects=[base], children=[sub])
        root = FakeCollection("asm.hierarchy", children=[asm])

        report = matching.match(m, objects=[base, shell, core],
                                collections=[root, asm, sub, inner])
        self.assertEqual([], report.unmatched)
        owned = {e.component_id: e.object_names for e in report.matched}
        self.assertEqual(["core"], owned["c003"])
        self.assertEqual(["shell"], owned["c002"])
        self.assertEqual("g002", core.get("RIG_group"))
        self.assertEqual("g001", shell.get("RIG_group"))


if __name__ == "__main__":
    unittest.main()
