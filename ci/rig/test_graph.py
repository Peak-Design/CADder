# SPDX-License-Identifier: GPL-3.0-or-later
"""RigPlan tests: ordering, loop verification, dependency pre-flight. No
bpy. graph.py must plan the whole rig before Blender is ever involved."""

import importlib
import math
import os
import sys
import unittest

# The addons directory (the parent of the CADder repo root) makes
# "import CADder.rig" work from any checkout named CADder.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import graph, manifest  # noqa: E402
from CADder.rig.manifest import ManifestError  # noqa: E402

from test_manifest import base_manifest, component, four_bar_manifest, hinge_manifest, identity4, ram_manifest  # noqa: E402


def plan_of(data):
    return graph.build(manifest.parse(data))


class TestImportGuards(unittest.TestCase):
    """Every module must import under plain Python. The bpy guard pattern is
    what keeps CI able to run manifest.py and graph.py at all."""

    def test_package_imports_without_bpy(self):
        for name in ("CADder.rig", "CADder.rig.manifest",
                     "CADder.rig.graph", "CADder.rig.matching",
                     "CADder.rig.constraints", "CADder.rig.drivers",
                     "CADder.rig.loops", "CADder.rig.parenting",
                     "CADder.rig.rig_build", "CADder.rig.ui",
                     "CADder.rig.definition", "CADder.rig"):
            importlib.import_module(name)

    def test_register_refuses_without_bpy(self):
        import CADder.rig as main
        with self.assertRaises(RuntimeError):
            main.register()


def five_bar_manifest(mobility=2):
    """The shape of the live wrench: a ring of five pins round four moving
    bodies. Five freedoms, three spent closing the ring, so two inputs."""
    data = base_manifest()
    data["components"] = [component("c%03d" % i, n) for i, n in
                          enumerate(["Ground", "A", "B", "C", "D"], 1)]
    data["rigid_groups"] = [
        {"id": "g000", "name": "ground", "components": ["c001"],
         "grounded": True, "frame": identity4(), "bbox_diag": 0.5},
    ] + [{"id": "g%03d" % i, "name": n, "components": ["c%03d" % (i + 1)],
          "grounded": False, "frame": None, "bbox_diag": 0.1}
         for i, n in enumerate(["a", "b", "c", "d"], 1)]

    def rev(jid, parent, child, origin):
        return {"id": jid, "type": "revolute",
                "parent_group": parent, "child_group": child,
                "origin": origin, "axis": [0.0, 0.0, 1.0],
                "secondary_axis": [1.0, 0.0, 0.0], "limits": None}

    # ground -j001- a -j003- b -j004- c -j005- d, and d back to ground at
    # j002. j002 drives and j005 is the cut, so the driver side is d alone
    # and the driven branch runs c, b, a: three bodies for two freedoms,
    # the shape of the live wrench.
    data["joints"] = [
        rev("j001", "g000", "g001", [0.0, 0.0, 0.0]),
        rev("j002", "g000", "g004", [0.4, 0.0, 0.0]),
        rev("j003", "g001", "g002", [0.1, 0.1, 0.0]),
        rev("j004", "g002", "g003", [0.2, 0.15, 0.0]),
        rev("j005", "g003", "g004", [0.3, 0.1, 0.0]),
    ]
    data["loops"] = [{
        "id": "L1",
        "member_joints": ["j001", "j002", "j003", "j004", "j005"],
        "closure_joint": "j005",
        "suggested_driver_joint": "j002",
        "planar": True,
        "plane_normal": [0.0, 0.0, 1.0],
        "mobility": mobility,
    }]
    return data


def screw_manifest(child_axis):
    """A screw with one body pinned to it about child_axis."""
    data = base_manifest()
    data["components"] = [component("c%03d" % i, n) for i, n in
                          enumerate(["Ground", "Screw", "Knuckle"], 1)]
    data["rigid_groups"] = [
        {"id": "g000", "name": "ground", "components": ["c001"],
         "grounded": True, "frame": identity4(), "bbox_diag": 0.5},
        {"id": "g001", "name": "screw", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.1},
        {"id": "g002", "name": "knuckle", "components": ["c003"],
         "grounded": False, "frame": None, "bbox_diag": 0.1},
    ]
    data["joints"] = [
        {"id": "j001", "type": "screw", "parent_group": "g000",
         "child_group": "g001", "origin": [0.0, 0.0, 0.0],
         "axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 0.0, 1.0],
         "limits": None,
         "coupling": {"kind": "screw", "driver_joint": None,
                      "lead_m_per_rev": 0.0254}},
        {"id": "j002", "type": "revolute", "parent_group": "g001",
         "child_group": "g002", "origin": [0.05, 0.0, 0.0],
         "axis": list(child_axis), "secondary_axis": [1.0, 0.0, 0.0],
         "limits": None},
    ]
    return data


class TestScrewSpinBone(unittest.TestCase):
    """A screw's turn lives on a bone of its own, below the body's.

    Two reasons, both live wrench.sldasm (2026-09-16, Oscar). A driver
    reading the slide off the bone it writes is a depsgraph cycle, and
    Blender breaks it with a stale value. And the turn is driven, so a body
    joined to the screw about another axis would be carried round by a turn
    it cannot undo: "everything rotates with the screw but it shouldn't".
    Hanging the turn BELOW the body means such a child inherits the slide
    alone by parenting where every child parents."""

    def test_a_screw_turns_on_a_bone_of_its_own(self):
        plan = plan_of(screw_manifest([0.0, 0.0, 1.0]))
        self.assertTrue(plan.bone_by_group["g001"].spin_name)

    def test_a_pin_across_the_screw_axis_stays_off_the_turn(self):
        plan = plan_of(screw_manifest([0.0, 0.0, 1.0]))
        self.assertFalse(plan.bone_by_group["g002"].parent_spin)

    def test_a_pin_on_the_screw_axis_rides_the_turn(self):
        # Coaxial: the body turns about the screw's OWN axis, so its own
        # channel can undo whatever the screw does.
        plan = plan_of(screw_manifest([1.0, 0.0, 0.0]))
        self.assertTrue(plan.bone_by_group["g002"].parent_spin)

    def test_a_weld_on_a_screw_rides_the_turn(self):
        data = screw_manifest([0.0, 0.0, 1.0])
        data["joints"][1]["type"] = "fixed"
        plan = plan_of(data)
        self.assertTrue(plan.bone_by_group["g002"].parent_spin)

    def test_a_plain_hinge_turns_on_its_own_bone(self):
        # Only a screw turns without being asked. A hinge is the user's to
        # pose, so its body needs no second bone.
        data = screw_manifest([0.0, 0.0, 1.0])
        data["joints"][0]["type"] = "revolute"
        data["joints"][0]["coupling"] = None
        plan = plan_of(data)
        self.assertEqual(plan.bone_by_group["g001"].spin_name, "")
        self.assertFalse(plan.bone_by_group["g002"].parent_spin)


class TestBranchLimits(unittest.TestCase):
    """Two links reaching one point have two answers, mirrored in the line
    through the ends of the chain. The assembly SolidWorks shipped is on
    one of them, and an elbow held inside the half turn either side of its
    rest bend cannot reach the other.

    Live wrench.sldasm (2026-09-16, Oscar): "at some point secondgrip and
    centerlink instantly flip to the wrong position/direction"."""

    def test_the_elbow_is_held_to_its_own_half_turn(self):
        lplan = plan_of(five_bar_manifest(mobility=2)).loops[0]
        # The driven branch is c, b: c is the elbow, b the shoulder.
        self.assertEqual(sorted(lplan.branch_limits), ["g003"])
        low, high = lplan.branch_limits["g003"]
        # Its links turn by +26.565 then -26.565 degrees, so it rests bent
        # by -53.13 degrees. Straight is 53.13 degrees away one way, folded
        # right back is 126.87 the other.
        self.assertAlmostEqual(math.degrees(high), 53.130, places=3)
        self.assertAlmostEqual(math.degrees(low), -126.870, places=3)
        self.assertAlmostEqual(high - low, math.pi, places=9)

    def test_the_shoulder_of_the_chain_is_left_free(self):
        # The shoulder swings the whole chain round and passes through its
        # own straight pose with nothing wrong at all.
        lplan = plan_of(five_bar_manifest(mobility=2)).loops[0]
        self.assertNotIn("g002", lplan.branch_limits)

    def test_a_loop_that_is_not_planar_is_left_alone(self):
        data = five_bar_manifest(mobility=2)
        data["loops"][0]["planar"] = False
        lplan = plan_of(data).loops[0]
        self.assertEqual(lplan.branch_limits, {})


class TestLoopMobility(unittest.TestCase):
    """A loop that takes more than one input must leave a bone out of the
    solve for each spare input, or the user has nothing to pose with.

    Live wrench.sldasm (2026-09-16, Oscar): "there should be an additional
    revolute joint". Five pins round one ring is a five-bar, and the whole
    driven side was being solved from one input."""

    def test_a_five_bar_keeps_one_bone_out_of_the_solve(self):
        plan = plan_of(five_bar_manifest(mobility=2))
        lplan = plan.loops[0]
        # The driven branch is c, b, a: tip first. With two inputs the
        # root-most of them stays posable.
        self.assertEqual(lplan.driven_chain, ["g003", "g002"])
        self.assertEqual(lplan.chain_count, 2)
        self.assertTrue(any("stays posable" in w for w in plan.warnings),
                        plan.warnings)

    def test_one_input_solves_the_whole_driven_side(self):
        plan = plan_of(five_bar_manifest(mobility=1))
        lplan = plan.loops[0]
        self.assertEqual(lplan.driven_chain, ["g003", "g002", "g001"])
        self.assertEqual(lplan.chain_count, 3)

    def test_a_manifest_without_the_field_reads_as_one_input(self):
        data = five_bar_manifest()
        del data["loops"][0]["mobility"]
        self.assertEqual(manifest.parse(data).loops[0].mobility, 1)


class TestHingePlan(unittest.TestCase):

    def test_ordering_and_assignment(self):
        plan = plan_of(hinge_manifest())
        self.assertEqual([b.group.id for b in plan.bones], ["g000", "g001"])
        self.assertIsNone(plan.bones[0].joint)
        self.assertEqual(plan.bones[1].joint.id, "j001")
        self.assertEqual(plan.bones[1].parent_group_id, "g000")
        self.assertEqual(plan.joint_group, {"j001": "g001"})
        self.assertEqual(plan.grounded_groups, ["g000"])
        self.assertEqual(plan.free_groups, [])
        self.assertEqual(plan.loops, [])

    def test_grounded_root_is_named_after_the_assembly(self):
        # The first grounded root is THE root: assembly-stem name, root
        # flag set (rig_build rests it at the origin: a grounded group's
        # manifest frame follows an arbitrary member and wandered onto the
        # hinge boss live, 2026-08-23). Other bones keep group names.
        plan = plan_of(hinge_manifest())
        root = plan.bones[0]
        self.assertTrue(root.root)
        self.assertEqual(
            root.bone_name,
            hinge_manifest()["step_export"]["file"].rsplit(".", 1)[0])
        self.assertFalse(plan.bones[1].root)

    def test_second_grounded_island_keeps_its_group_name(self):
        data = hinge_manifest()
        data["components"].append(component("c003", "Anchor"))
        data["rigid_groups"].append(
            {"id": "g002", "name": "anchor", "components": ["c003"],
             "grounded": True, "frame": None, "bbox_diag": 0.1})
        plan = plan_of(data)
        by_gid = {b.group.id: b for b in plan.bones}
        self.assertTrue(by_gid["g000"].root)
        self.assertFalse(by_gid["g002"].root)
        self.assertEqual(by_gid["g002"].bone_name, "anchor")

    def test_mirror_pair_free_joints_become_tree_edges(self):
        """A free joint that carries or drives a mirror coupling is
        deliberately ground-rooted (live corpus 14 sym4, 2026-08-23): both
        halves parent like tree edges, share the plane-aligned rest normal,
        and the pre-flight sees the driven->driver dependency."""
        data = hinge_manifest()
        data["components"].append(component("c003", "SlideB"))
        data["rigid_groups"].append(
            {"id": "g002", "name": "slide_b", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.05})
        data["joints"] = [
            {"id": "j001", "type": "free", "parent_group": "g000",
             "child_group": "g001", "origin": None, "axis": None,
             "secondary_axis": None, "limits": None},
            {"id": "j002", "type": "free", "parent_group": "g000",
             "child_group": "g002", "origin": None, "axis": None,
             "secondary_axis": None, "limits": None,
             "coupling": {"kind": "mirror", "driver_joint": "j001",
                          "mirror_plane": {"point": [0.05, 0.0, 0.0],
                                           "normal": [1.0, 0.0, 0.0]}}},
        ]
        plan = plan_of(data)
        self.assertEqual(plan.free_groups, [])
        self.assertEqual(plan.bone_by_group["g001"].parent_group_id, "g000")
        self.assertEqual(plan.bone_by_group["g002"].parent_group_id, "g000")
        self.assertEqual(plan.bone_by_group["g001"].mirror_normal, [1.0, 0.0, 0.0])
        self.assertEqual(plan.bone_by_group["g002"].mirror_normal, [1.0, 0.0, 0.0])

    def test_plain_free_joint_still_leaves_the_child_unparented(self):
        data = hinge_manifest()
        data["joints"][0] = {
            "id": "j001", "type": "free", "parent_group": "g000",
            "child_group": "g001", "origin": None, "axis": None,
            "secondary_axis": None, "limits": None}
        plan = plan_of(data)
        self.assertEqual(plan.free_groups, ["g001"])
        self.assertIsNone(plan.bone_by_group["g001"].mirror_normal)

    def test_swing_cone_ball_allocates_the_template_names(self):
        # A ball with a cone frame gets the DEF/POLE/GOAL bones planned
        # (bone_name stays the user handle). A legacy limited ball without
        # the frame keeps a single bone and the Euler-box fallback.
        data = hinge_manifest()
        data["joints"][0]["type"] = "ball"
        data["joints"][0]["axis"] = [0.0, 1.0, 0.0]
        data["joints"][0]["secondary_axis"] = [0.0, 1.0, 0.0]
        data["joints"][0]["limits"] = {
            "rotation": {"min": 0.0, "max": 0.7853981633974483,
                         "value_at_rest": 0.0},
            "translation": None}
        plan = plan_of(data)
        bp = plan.bone_by_group["g001"]
        self.assertTrue(graph.swing_cone(bp.joint))
        self.assertEqual(bp.ball_def_name, "DEF_" + bp.bone_name)
        self.assertEqual(bp.ball_pole_name, "POLE_" + bp.bone_name)
        self.assertEqual(bp.ball_goal_name, "GOAL_" + bp.bone_name)

    def test_legacy_limited_ball_plans_no_template(self):
        data = hinge_manifest()
        data["joints"][0]["type"] = "ball"
        data["joints"][0]["axis"] = None
        data["joints"][0]["secondary_axis"] = None
        data["joints"][0]["limits"] = {
            "rotation": {"min": 0.0, "max": 0.7853981633974483,
                         "value_at_rest": 0.0},
            "translation": None}
        plan = plan_of(data)
        bp = plan.bone_by_group["g001"]
        self.assertFalse(graph.swing_cone(bp.joint))
        self.assertEqual(bp.ball_def_name, "")

    def test_parents_precede_children_in_chain(self):
        data = hinge_manifest()
        data["components"].append(component("c003", "Tip"))
        data["rigid_groups"].append(
            {"id": "g002", "name": "tip", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.1})
        data["joints"].append({
            "id": "j002", "type": "prismatic",
            "parent_group": "g001", "child_group": "g002",
            "origin": [0.0, 0.0, 0.1], "axis": [1.0, 0.0, 0.0],
            "secondary_axis": [0.0, 0.0, 1.0], "limits": None})
        plan = plan_of(data)
        order = [b.group.id for b in plan.bones]
        self.assertLess(order.index("g000"), order.index("g001"))
        self.assertLess(order.index("g001"), order.index("g002"))

    def test_free_joint_leaves_child_unparented(self):
        data = hinge_manifest()
        data["joints"][0]["type"] = "free"
        data["joints"][0]["origin"] = None
        data["joints"][0]["axis"] = None
        data["joints"][0]["secondary_axis"] = None
        data["joints"][0]["limits"] = None
        plan = plan_of(data)
        self.assertEqual(plan.free_groups, ["g001"])
        self.assertIsNone(plan.bone_by_group["g001"].parent_group_id)
        self.assertIsNone(plan.bone_by_group["g001"].joint)

    @staticmethod
    def _carrier_data(j1_type="revolute", j2_type="revolute",
                      j2_limits=None, j1_axis=(0.0, 0.0, -1.0),
                      j2_axis=(0.0, 0.0, 1.0)):
        data = hinge_manifest()
        data["rigid_groups"].append(
            {"id": "g002", "name": "puck_carrier", "components": [],
             "grounded": False, "frame": None, "bbox_diag": 0.04})
        data["components"].append(component("c003", "puck"))
        data["rigid_groups"].append(
            {"id": "g003", "name": "puck", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.03})
        data["joints"].append({
            "id": "j002", "type": j1_type,
            "parent_group": "g001", "child_group": "g002",
            "origin": [0.0, 0.0, 0.01], "axis": list(j1_axis),
            "secondary_axis": [1.0, 0.0, 0.0], "limits": None})
        data["joints"].append({
            "id": "j003", "type": j2_type,
            "parent_group": "g002", "child_group": "g003",
            "origin": [0.0466, 0.0453, 0.0],
            "axis": list(j2_axis) if j2_type != "ball" else None,
            "secondary_axis": [1.0, 0.0, 0.0] if j2_type != "ball" else None,
            "limits": j2_limits})
        return data

    def test_orbit_carrier_collapses_to_one_posable_bone(self):
        """Rim-tangent discs: the carrier chain folds into ONE bone with a
        Limit Distance spec: the puck is dragged directly instead of
        rotating a helper (2026-08-23)."""
        plan = plan_of(self._carrier_data())
        self.assertEqual(plan.collapsed_carriers, ["g002"])
        self.assertNotIn("g002", plan.bone_by_group)
        bp = plan.bone_by_group["g003"]
        self.assertEqual(bp.parent_group_id, "g001")
        self.assertEqual(bp.joint.id, "j003")
        spec = bp.collapsed
        self.assertEqual(spec.kind, "orbit_spin")
        # Center projected into the child's plane (j1 origin z=0.01, axis -Z,
        # child at z=0): the Limit Distance sphere must be axial-offset-free.
        self.assertEqual(spec.orbit_center, [0.0, 0.0, 0.0])
        self.assertAlmostEqual(
            spec.orbit_radius, (0.0466 ** 2 + 0.0453 ** 2) ** 0.5, places=9)
        self.assertTrue(spec.helper_name.startswith("TGT_"))
        self.assertNotIn("j002", plan.joint_group)
        self.assertEqual(plan.joint_group["j003"], "g003")
        self.assertEqual(plan.free_groups, [])

    def test_planar_spin_carrier_collapses_with_contact_frame(self):
        """Cylinder on plane: one bone, spin axis in the plane, no helper."""
        plan = plan_of(self._carrier_data(
            j1_type="planar", j1_axis=(0.0, 0.0, 1.0),
            j2_axis=(1.0, 0.0, 0.0)))
        bp = plan.bone_by_group["g003"]
        self.assertEqual(bp.collapsed.kind, "planar_spin")
        self.assertEqual(bp.collapsed.helper_name, "")
        self.assertEqual(bp.parent_group_id, "g001")

    def test_a_tilted_spin_axis_is_left_as_a_chain(self):
        """Tangent cone on a plane (live corpus 15 cone3): the spin axis
        leaves the plane by the half-angle, and no ONE bone can hold that.

        The motion is a yaw about the plane normal composed with a spin
        about the cone axis, two axes a half-angle apart. A single bone with
        a channel locked turns about two PERPENDICULAR rest axes, and
        matching both slices forces the half-angle to zero: the cylinder,
        which is why planar_spin is exact and this is not. So the collapse
        is declined and the exporter's own two-bone chain stands: the
        carrier yaws in the plane, the child spins on its axis, and the dip
        is held by construction rather than by an iterated clamp that
        measured 61 degrees of leak on the live export (2026-08-25)."""
        plan = plan_of(self._carrier_data(
            j1_type="planar", j1_axis=(0.0, 0.0, 1.0),
            j2_axis=(0.9397, 0.0, 0.342)))
        bp = plan.bone_by_group["g003"]
        self.assertIsNone(bp.collapsed)
        self.assertEqual(plan.collapsed_carriers, [])
        # The carrier keeps its bone, and the child hangs off it.
        self.assertIn("g002", plan.bone_by_group)
        self.assertEqual(bp.parent_group_id, "g002")

    def test_limits_on_the_chain_keep_the_carrier_bone(self):
        """Anything the collapse patterns cannot absorb keeps the explicit
        chain: correct beats convenient."""
        plan = plan_of(self._carrier_data(
            j2_limits={"rotation": {"min": -0.5, "max": 0.5,
                                    "value_at_rest": 0.0},
                       "translation": None}))
        self.assertEqual(plan.collapsed_carriers, [])
        self.assertEqual(plan.bone_by_group["g003"].parent_group_id, "g002")
        self.assertIsNone(plan.bone_by_group["g003"].collapsed)

    def test_unknown_pattern_keeps_the_carrier_bone(self):
        plan = plan_of(self._carrier_data(j2_type="prismatic"))
        self.assertEqual(plan.collapsed_carriers, [])
        self.assertIn("g002", plan.bone_by_group)

    def test_skewed_orbit_axes_keep_the_carrier_bone(self):
        plan = plan_of(self._carrier_data(j2_axis=(1.0, 0.0, 0.0)))
        self.assertEqual(plan.collapsed_carriers, [])
        self.assertIn("g002", plan.bone_by_group)

    def test_path_joint_parents_like_any_tree_edge(self):
        """A path joint carries its child in the tree: the curve constrains
        position at pose time, not the parenting."""
        data = hinge_manifest()
        data["joints"][0] = {
            "id": "j001", "type": "path",
            "parent_group": "g000", "child_group": "g001",
            "origin": [0.02, 0.0, 0.02],
            "axis": [1.0, 0.0, 0.0],
            "secondary_axis": [0.0, 0.0, 1.0],
            "limits": None,
            "path": {"points": [[0.0, 0.0, 0.02], [0.05, 0.0, 0.02]],
                     "closed": False},
        }
        plan = plan_of(data)
        self.assertEqual(plan.bone_by_group["g001"].parent_group_id, "g000")
        self.assertEqual(plan.bone_by_group["g001"].joint.id, "j001")
        self.assertEqual(plan.free_groups, [])

    def test_unreferenced_group_is_free(self):
        data = hinge_manifest()
        data["components"].append(component("c003", "Loose"))
        data["rigid_groups"].append(
            {"id": "g002", "name": "loose", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": None})
        plan = plan_of(data)
        self.assertIn("g002", plan.free_groups)

    def test_unique_bone_names_on_name_collision(self):
        data = hinge_manifest()
        data["rigid_groups"][1]["name"] = "base"
        plan = plan_of(data)
        names = [b.bone_name for b in plan.bones]
        self.assertEqual(len(names), len(set(names)))


class TestLoops(unittest.TestCase):

    def test_four_bar_closure_plan(self):
        plan = plan_of(four_bar_manifest())
        self.assertEqual(len(plan.loops), 1)
        lp = plan.loops[0]
        self.assertEqual(lp.closure_joint.id, "j003")
        # j001 is the crank branch, so the crank drives and the coupler +
        # rocker side is IK-solved from the coupler tip.
        self.assertEqual(lp.helper_parent_group, "g001")
        self.assertEqual(lp.ik_tip_group, "g003")
        self.assertEqual(lp.driven_chain, ["g003", "g002"])
        self.assertEqual(lp.chain_count, 2)
        self.assertEqual(lp.driver_chain, ["g001"])
        self.assertTrue(lp.helper_name)
        self.assertTrue(lp.effector_name)
        self.assertNotEqual(lp.helper_name, lp.effector_name)

    def test_driver_suggestion_on_other_branch(self):
        data = four_bar_manifest()
        data["loops"][0]["suggested_driver_joint"] = "j002"
        plan = plan_of(data)
        lp = plan.loops[0]
        self.assertEqual(lp.helper_parent_group, "g003")
        self.assertEqual(lp.ik_tip_group, "g001")
        self.assertEqual(lp.driven_chain, ["g001"])
        self.assertEqual(lp.chain_count, 1)

    def test_closure_joint_must_be_non_tree_edge(self):
        data = base_manifest()
        data["components"] = [component("c%03d" % i, "P%d" % i) for i in range(1, 5)]
        data["rigid_groups"] = [
            {"id": "g000", "name": "a", "components": ["c001"],
             "grounded": True, "frame": None, "bbox_diag": None},
            {"id": "g001", "name": "b", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": None},
            {"id": "g002", "name": "c", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": None},
            {"id": "g003", "name": "d", "components": ["c004"],
             "grounded": False, "frame": None, "bbox_diag": None},
        ]

        def rev(jid, parent, child):
            return {"id": jid, "type": "revolute", "parent_group": parent,
                    "child_group": child, "origin": [0.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, 1.0], "secondary_axis": [1.0, 0.0, 0.0],
                    "limits": None}

        data["joints"] = [rev("j001", "g000", "g001"),
                          rev("j002", "g001", "g002"),
                          rev("j003", "g002", "g003")]
        # Cutting j002 disconnects the chain: it is a tree edge, not a
        # closure, and the plan must refuse rather than build half a rig.
        data["loops"] = [{"id": "L1",
                          "member_joints": ["j001", "j002", "j003"],
                          "closure_joint": "j002",
                          "suggested_driver_joint": None,
                          "planar": False, "plane_normal": None}]
        with self.assertRaisesRegex(ManifestError, "j002"):
            plan_of(data)

    def test_member_list_mismatch(self):
        data = four_bar_manifest()
        data["loops"][0]["member_joints"] = ["j001", "j003", "j004"]
        with self.assertRaisesRegex(ManifestError, "member_joints"):
            plan_of(data)


class TestConsistency(unittest.TestCase):

    def test_two_tree_parents(self):
        data = four_bar_manifest()
        data["loops"] = []
        with self.assertRaisesRegex(ManifestError, "two tree parents|cycle"):
            plan_of(data)

    def test_undeclared_cycle(self):
        data = base_manifest()
        data["components"] = [component("c%03d" % i, "P%d" % i) for i in range(1, 5)]
        data["rigid_groups"] = [
            {"id": "g000", "name": "gnd", "components": ["c001"],
             "grounded": True, "frame": None, "bbox_diag": None},
            {"id": "g001", "name": "r1", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": None},
            {"id": "g002", "name": "r2", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": None},
            {"id": "g003", "name": "r3", "components": ["c004"],
             "grounded": False, "frame": None, "bbox_diag": None},
        ]

        def rev(jid, parent, child):
            return {"id": jid, "type": "revolute", "parent_group": parent,
                    "child_group": child, "origin": [0.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, 1.0], "secondary_axis": [1.0, 0.0, 0.0],
                    "limits": None}

        data["joints"] = [rev("j001", "g001", "g002"),
                          rev("j002", "g002", "g003"),
                          rev("j003", "g003", "g001")]
        with self.assertRaisesRegex(ManifestError, "cycle"):
            plan_of(data)

    def test_grounded_child_refused(self):
        data = hinge_manifest()
        data["joints"][0]["parent_group"] = "g001"
        data["joints"][0]["child_group"] = "g000"
        with self.assertRaisesRegex(ManifestError, "grounded"):
            plan_of(data)

    def test_self_joint_refused(self):
        data = hinge_manifest()
        data["joints"][0]["child_group"] = "g000"
        with self.assertRaisesRegex(ManifestError, "parent and child"):
            plan_of(data)


class TestDependencyPreflight(unittest.TestCase):

    def _two_revolutes(self):
        data = hinge_manifest()
        data["components"].append(component("c003", "Wheel"))
        data["rigid_groups"].append(
            {"id": "g002", "name": "wheel", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.1})
        data["joints"].append({
            "id": "j002", "type": "revolute",
            "parent_group": "g000", "child_group": "g002",
            "origin": [0.2, 0.0, 0.0], "axis": [0.0, 0.0, 1.0],
            "secondary_axis": [1.0, 0.0, 0.0], "limits": None})
        return data

    def test_mutual_couplings_are_a_cycle(self):
        data = self._two_revolutes()
        data["joints"][0]["coupling"] = {"kind": "gear", "driver_joint": "j002",
                                         "ratio": 2.0}
        data["joints"][1]["coupling"] = {"kind": "gear", "driver_joint": "j001",
                                         "ratio": 0.5}
        with self.assertRaisesRegex(ManifestError, "dependency cycle"):
            plan_of(data)

    def test_one_way_coupling_is_fine(self):
        data = self._two_revolutes()
        data["joints"][1]["coupling"] = {"kind": "gear", "driver_joint": "j001",
                                         "ratio": -1.6}
        plan = plan_of(data)
        self.assertEqual(plan.warnings, [])

    def test_screw_self_coupling_is_not_a_cycle(self):
        data = hinge_manifest()
        data["joints"][0]["type"] = "screw"
        data["joints"][0]["coupling"] = {"kind": "screw", "driver_joint": None,
                                         "lead_m_per_rev": 0.005}
        plan = plan_of(data)
        self.assertEqual(plan.warnings, [])

    def test_non_tree_driver_warns_and_skips(self):
        data = four_bar_manifest()
        # j003 is the loop closure, so it is not a tree edge and cannot
        # source a driver. The plan warns instead of dying.
        data["joints"][1]["coupling"] = {"kind": "gear", "driver_joint": "j003",
                                         "ratio": 1.0}
        plan = plan_of(data)
        self.assertTrue(any("j003" in w for w in plan.warnings))



class TestSliderCrank(unittest.TestCase):
    """A loop cut at its slide is closed by aiming, not by IK. Blender's IK
    only rotates, so a slide inside a solved chain can only be locked and the
    mechanism freezes, which is what a hydraulic ram did before this."""

    def test_aim_pair_replaces_the_ik_plan(self):
        plan = plan_of(ram_manifest())
        self.assertEqual(len(plan.sliders), 1)
        self.assertEqual(plan.loops, [])
        sp = plan.sliders[0]
        self.assertEqual((sp.a_group, sp.c_group), ("g002", "g003"))
        self.assertEqual(sp.a_pivot, [1.0, 0.0, 0.0])
        self.assertEqual(sp.c_pivot, [0.0, 1.0, 0.0])

    def test_each_half_aims_at_the_others_parent(self):
        """The whole reason the duplicates exist: aiming the two halves
        straight at each other is a dependency cycle, and Blender would
        refuse the rig. Each target rides the other half's PARENT: the posed
        clamp and the ground, neither of which is aimed at anything."""
        sp = plan_of(ram_manifest()).sliders[0]
        self.assertEqual(sp.a_aim_parent, "g001")   # the clamp carries the rod
        self.assertEqual(sp.c_aim_parent, "g000")   # the body carries the barrel
        self.assertNotEqual(sp.a_aim_parent, sp.c_group)
        self.assertNotEqual(sp.c_aim_parent, sp.a_group)

    def test_both_halves_rest_pointing_at_each_other(self):
        """Damped Track aims a NAMED local axis, so the halves can only track
        each other if their rest +Y already lies along the ram. Resting along
        the joint axis instead pointed the ram bones at the ceiling (live
        ClampRig, 2026-08-24)."""
        plan = plan_of(ram_manifest())
        self.assertEqual(plan.bone_by_group["g002"].aim_at, [0.0, 1.0, 0.0])
        self.assertEqual(plan.bone_by_group["g003"].aim_at, [1.0, 0.0, 0.0])
        # Everything else keeps the ordinary joint-axis frame.
        self.assertIsNone(plan.bone_by_group["g001"].aim_at)

    def test_the_slide_is_not_a_tree_edge(self):
        plan = plan_of(ram_manifest())
        self.assertNotIn("j003", plan.joint_group)
        self.assertEqual(plan.bone_by_group["g003"].parent_group_id, "g001")
        self.assertEqual(plan.bone_by_group["g002"].parent_group_id, "g000")

    def test_an_ik_closure_kind_still_plans_ik(self):
        plan = plan_of(ram_manifest(closure_kind="ik"))
        self.assertEqual(plan.sliders, [])
        self.assertEqual(len(plan.loops), 1)

    def test_a_half_with_no_mount_falls_back_to_ik(self):
        """Aiming needs a pivot to aim FROM, and the grounded root has no
        mount of its own. A slide straight to ground therefore keeps the IK
        plan, and the manifest records why rather than silently dropping the
        closure."""
        data = base_manifest()
        data["components"] = [component("c%03d" % i, n) for i, n in
                              enumerate(["Body", "Slider", "Link"], 1)]
        data["rigid_groups"] = [
            {"id": "g000", "name": "body", "components": ["c001"],
             "grounded": True, "frame": identity4(), "bbox_diag": 1.0},
            {"id": "g001", "name": "slider", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": 0.3},
            {"id": "g002", "name": "link", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.3},
        ]
        pin = [0.0, 0.0, 1.0]

        def rev(jid, parent, child, origin):
            return {"id": jid, "type": "revolute", "parent_group": parent,
                    "child_group": child, "origin": origin, "axis": pin,
                    "secondary_axis": [1.0, 0.0, 0.0], "limits": None}

        data["joints"] = [
            # The slide runs straight to ground, so its parent side is the
            # root and has no mount joint.
            {"id": "j001", "type": "prismatic",
             "parent_group": "g000", "child_group": "g001",
             "origin": [0.0, 0.0, 0.0], "axis": [1.0, 0.0, 0.0],
             "secondary_axis": [0.0, 0.0, 1.0], "limits": None},
            rev("j002", "g000", "g002", [0.0, 0.5, 0.0]),
            rev("j003", "g002", "g001", [0.5, 0.5, 0.0]),
        ]
        data["loops"] = [{
            "id": "L1",
            "member_joints": ["j001", "j002", "j003"],
            "closure_joint": "j001",
            "closure_kind": "aim_pair",
            "suggested_driver_joint": "j002",
            "planar": True,
            "plane_normal": pin,
        }]

        plan = plan_of(data)
        self.assertEqual(plan.sliders, [])
        self.assertEqual(len(plan.loops), 1)
        self.assertTrue(any("Falling back to IK" in w for w in plan.warnings),
                        plan.warnings)



class TestChainedClosure(unittest.TestCase):
    """closure_kind "none": the exporter cut the loop so the TREE carries the
    motion, and the consumer must not solve it. Live ClampRig's cutting
    head and lead screw rod both slide along the machine with the head mated
    to the rod. Cutting between them left both hanging off ground as siblings
    and driving the lead screw left the head behind."""

    @staticmethod
    def _data():
        data = base_manifest()
        data["components"] = [component("c%03d" % i, n) for i, n in
                              enumerate(["Body", "Rod", "Head"], 1)]
        data["rigid_groups"] = [
            {"id": "g000", "name": "body", "components": ["c001"],
             "grounded": True, "frame": identity4(), "bbox_diag": 1.0},
            {"id": "g001", "name": "rod", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": 0.5},
            {"id": "g002", "name": "head", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.3},
        ]
        along = [0.0, 0.0, 1.0]
        data["joints"] = [
            {"id": "j001", "type": "prismatic",
             "parent_group": "g000", "child_group": "g001",
             "origin": [0.0, 0.0, 0.0], "axis": along,
             "secondary_axis": [1.0, 0.0, 0.0],
             "limits": {"rotation": None,
                        "translation": {"min": 0.0, "max": 0.85,
                                        "value_at_rest": 0.0}}},
            {"id": "j003", "type": "planar",
             "parent_group": "g001", "child_group": "g002",
             "origin": [0.0, 0.0, 0.1], "axis": along,
             "secondary_axis": [1.0, 0.0, 0.0], "limits": None},
            {"id": "j002", "type": "prismatic",
             "parent_group": "g000", "child_group": "g002",
             "origin": [0.0, 0.0, 0.1], "axis": along,
             "secondary_axis": [1.0, 0.0, 0.0], "limits": None},
        ]
        data["loops"] = [{
            "id": "L1",
            "member_joints": ["j001", "j002", "j003"],
            "closure_joint": "j002",
            "closure_kind": "none",
            "suggested_driver_joint": "j001",
            "planar": False,
            "plane_normal": None,
        }]
        return data

    def test_the_head_parents_under_the_rod(self):
        plan = plan_of(self._data())
        self.assertEqual(plan.bone_by_group["g001"].parent_group_id, "g000")
        self.assertEqual(plan.bone_by_group["g002"].parent_group_id, "g001")

    def test_nothing_is_solved_but_the_loop_is_still_verified(self):
        plan = plan_of(self._data())
        self.assertEqual(plan.loops, [])
        self.assertEqual(plan.sliders, [])
        self.assertEqual([lp.id for lp in plan.open_loops], ["L1"])

    def test_a_bad_member_list_is_still_rejected(self):
        """Leaving the closure unsolved must not mean leaving it unchecked."""
        data = self._data()
        data["loops"][0]["member_joints"] = ["j001", "j002"]
        with self.assertRaises(ManifestError):
            plan_of(data)

    def test_an_unknown_closure_kind_is_rejected(self):
        data = self._data()
        data["loops"][0]["closure_kind"] = "magic"
        with self.assertRaises(ManifestError):
            plan_of(data)


if __name__ == "__main__":
    unittest.main()


class CamContactPlanTests(unittest.TestCase):
    """A cam-coupled slide plans its hidden bones: PRJ for a vertex or a
    roller, REL and OFF for a flat face."""

    def _plan(self, kind, **follower):
        data = hinge_manifest()
        data["components"].append(component("c003", "Follower"))
        data["rigid_groups"].append(
            {"id": "g002", "name": "follower", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.1})
        data["joints"].append({
            "id": "j002", "type": "prismatic", "parent_group": "g000", "child_group": "g002",
            "origin": [0.03, 0.0, 0.0], "axis": [1.0, 0.0, 0.0],
            "secondary_axis": [0.0, 0.0, 1.0], "limits": None})
        fol = {"kind": kind, "point": [0.03, 0.0, 0.0], "axis": None, "radius": None, "normal": None}
        fol.update(follower)
        data["joints"][1]["coupling"] = {
            "kind": "cam", "driver_joint": "j001",
            "cam": {"axis": [0, 0, 1], "origin": [0, 0, 0],
                    "surface": {"points": [[0.03, 0, -0.01], [0.03, 0, 0.01], [0, 0.03, 0]],
                                "triangles": [[0, 1, 2]]},
                    "follower": fol}}
        return graph.build(manifest.parse(data))

    def test_vertex_follower_plans_a_projection_bone(self):
        plan = self._plan("vertex")
        bp = plan.bone_by_group[plan.joint_group["j002"]]
        self.assertTrue(bp.cam_prj_name.startswith("PRJ_"))
        self.assertEqual(bp.cam_rel_name, "")

    def test_flat_follower_plans_rel_and_off_bones(self):
        plan = self._plan("flat", normal=[1, 0, 0])
        bp = plan.bone_by_group[plan.joint_group["j002"]]
        self.assertEqual(bp.cam_prj_name, "")
        self.assertTrue(bp.cam_rel_name.startswith("REL_"))
        self.assertTrue(bp.cam_off_name.startswith("OFF_"))
