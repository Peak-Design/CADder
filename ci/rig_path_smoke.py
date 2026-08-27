# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the two joints that carry their own geometry: path
(SW-To-Blender corpus 17) and surface. Both work the same way: the manifest's
shape becomes a hidden mesh parented to the parent bone, the child bone
carries a nearest-surface Shrinkwrap targeting it, a dragged bone lands ON
that shape, and, in the regression that cost a live round (2026-08-23), a
bone RESTING on it must not move at all, which Clamp To violated by 26 mm
because it maps one location axis into curve parameter instead of finding
the nearest point.

Run:  blender -b --factory-startup -P rig_path_smoke.py
"""

import json
import math
import os
import sys
import tempfile

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from STEPper_NEXT.rig import graph, manifest as man_mod, matching, rig_build  # noqa: E402


POINTS = [[0, 0, 0.02], [0.03, 0.01, 0.02], [0.06, 0.025, 0.02],
          [0.09, 0.03, 0.025], [0.12, 0.02, 0.03]]

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.SwToBlender", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "path-smoke.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "track-1", "step_name": "track",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
        {"id": "c002", "sw_path": "shuttle-1", "step_name": "shuttle",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0.02], [0, 1, 0, 0], [0, 0, 1, 0.02],
                       [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "track", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.2},
        {"id": "g001", "name": "shuttle", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.05},
    ],
    "joints": [
        # The rest point is the midpoint of the first segment lifted 60 um
        # off the chord: a real mate vertex sits on the CURVE, so it misses
        # the sampled polyline by the sagitta.
        {"id": "j001", "type": "path", "parent_group": "g000",
         "child_group": "g001", "origin": [0.015, 0.005, 0.02006],
         "axis": [1, 0, 0], "secondary_axis": [0, 0, 1], "limits": None,
         "path": {"points": POINTS, "closed": False}},
    ],
    "loops": [],
    "warnings": [],
}


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json",
                                     delete=False) as f:
        json.dump(MANIFEST, f)
        path = f.name
    try:
        m = man_mod.load(path)
    finally:
        os.unlink(path)

    plan = graph.build(m)
    res = rig_build.build(bpy.context, m, plan, matching.identity_frame())
    arm = res.armature_object

    rail = bpy.data.objects[res.contact_mesh_names["j001"]]
    assert rail.type == "MESH"
    assert len(rail.data.polygons) == len(POINTS), \
        "the rail needs faces (the shrinkwrap BVH ignores loose edges) and " \
        "one more than the samples (the rest point threaded in)"
    assert rail.parent == arm and rail.parent_type == "BONE"
    assert rail.parent_bone == res.bone_names["g000"]

    pb = arm.pose.bones[res.bone_names["g001"]]
    cons = [c for c in pb.constraints if c.type == "SHRINKWRAP"]
    assert len(cons) == 1 and cons[0].target == rail
    assert cons[0].shrinkwrap_type == "NEAREST_SURFACE"
    assert list(pb.lock_location) == [False, False, False]

    def posed_head():
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        return arm.evaluated_get(dg).pose.bones[pb.name].head

    def off_path(head):
        """Distance to the sampled polyline, segments included."""
        best = 1e9
        for i in range(len(POINTS) - 1):
            a, b = Vector(POINTS[i]), Vector(POINTS[i + 1])
            ab = b - a
            t = max(0.0, min(1.0, (head - a).dot(ab) / ab.length_squared))
            best = min(best, (head - (a + ab * t)).length)
        return best

    # The rest pose must be a FIXED POINT of the constraint: the bone starts
    # on the path, so it may not move, or the geometry parented to it walks.
    rest = Vector(MANIFEST["joints"][0]["origin"])
    drift = (posed_head() - rest).length
    assert drift < 1e-5, "a resting bone slid %.4f m along its path" % drift

    pb.location = (0.5, 0.4, 0.3)
    dragged = posed_head()
    assert off_path(dragged) < 1e-4, \
        "dragged bone left the path (%.4f off)" % off_path(dragged)
    assert (dragged - rest).length > 0.01, "the drag did not move the bone"

    print("rig_path_smoke: OK: resting bone held to %.7f m, dragged bone "
          "%.7f m off the path" % (drift, off_path(dragged)))


# ── surface joints ──────────────────────────────────────────────────────
# A torus, because it is the case that motivated the joint (a rope's corner
# on a shackle's continuous face) and because no analytic joint describes
# it: every point of the surface sits exactly TUBE from the ring circle,
# which is a sharp, parameterisation-free thing to assert.
RING, TUBE = 0.06, 0.02


def torus_patch(u_steps=48, v_steps=24):
    verts, tris = [], []
    for i in range(u_steps):
        u = 2.0 * math.pi * i / u_steps
        for k in range(v_steps):
            v = 2.0 * math.pi * k / v_steps
            verts.append([(RING + TUBE * math.cos(v)) * math.cos(u),
                          (RING + TUBE * math.cos(v)) * math.sin(u),
                          TUBE * math.sin(v)])
    at = lambda i, k: (i % u_steps) * v_steps + (k % v_steps)  # noqa: E731
    for i in range(u_steps):
        for k in range(v_steps):
            tris.append([at(i, k), at(i + 1, k), at(i + 1, k + 1)])
            tris.append([at(i, k), at(i + 1, k + 1), at(i, k + 1)])
    return verts, tris


def off_torus(p):
    """How far a point is off the torus surface, exactly."""
    return abs(math.hypot(math.hypot(p.x, p.y) - RING, p.z) - TUBE)


def surface_main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    verts, tris = torus_patch()
    # A rest point ON the surface but not on a tessellation vertex: the
    # sagitta between them is exactly what _pin_through has to absorb.
    u, v = 0.31, 0.77
    rest = [(RING + TUBE * math.cos(v)) * math.cos(u),
            (RING + TUBE * math.cos(v)) * math.sin(u),
            TUBE * math.sin(v)]

    doc = json.loads(json.dumps(MANIFEST))
    doc["joints"] = [{
        "id": "j001", "type": "surface", "parent_group": "g000",
        "child_group": "g001", "origin": rest,
        "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None,
        "surface": {"points": verts, "triangles": tris},
    }]
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json",
                                     delete=False) as f:
        json.dump(doc, f)
        path = f.name
    try:
        m = man_mod.load(path)
    finally:
        os.unlink(path)

    plan = graph.build(m)
    res = rig_build.build(bpy.context, m, plan, matching.identity_frame())
    arm = res.armature_object

    patch = bpy.data.objects[res.contact_mesh_names["j001"]]
    assert patch.type == "MESH"
    assert len(patch.data.polygons) == len(tris) + 2, \
        "the rest point should have fanned one triangle into three"
    assert patch.parent == arm and patch.parent_bone == res.bone_names["g000"]

    pb = arm.pose.bones[res.bone_names["g001"]]
    cons = [c for c in pb.constraints if c.type == "SHRINKWRAP"]
    assert len(cons) == 1 and cons[0].target == patch
    assert list(pb.lock_rotation) == [False, False, False], \
        "a point on a face constrains no rotation"

    def posed_head():
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        return arm.evaluated_get(dg).pose.bones[pb.name].head

    drift = (posed_head() - Vector(rest)).length
    assert drift < 1e-6, "a resting bone slid %.6f m across its face" % drift

    # Dragged anywhere, the point stays on the torus. The tessellation is
    # chordal, so the tolerance is the sagitta of one tube step, not zero.
    sag = TUBE * (1.0 - math.cos(math.pi / 24)) + 1e-6
    worst = 0.0
    for pull in ((0.03, 0.0, 0.0), (0.0, 0.05, 0.02), (-0.04, -0.02, -0.03),
                 (0.0, 0.0, 0.09)):
        pb.location = pull
        worst = max(worst, off_torus(posed_head()))
    pb.location = (0.0, 0.0, 0.0)
    assert worst < sag, \
        "dragged bone left the torus by %.6f m (sagitta %.6f)" % (worst, sag)

    print("rig_path_smoke: OK: surface joint on a %d-triangle torus, rest "
          "held to %.7f m, dragged %.7f m off the face (sagitta %.7f)"
          % (len(tris), drift, worst, sag))


main()
surface_main()
