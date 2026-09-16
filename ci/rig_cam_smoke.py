# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the cam contact: a cam free in its plane.

    blender -b --factory-startup -P ci/rig_cam_smoke.py

A cam on a planar joint (turns about Z, slides in X and Y), which no
table can drive, carrying two profiles: an eccentric circle and a hexagon.
Four sliding followers ride them through "cam" couplings: a vertex and a
roller on the circle, a flat face on the circle mounted on the vertex
follower (the rod-in-lifter arrangement of the SolidWorks samples), and a
roller on the hexagon (sharp corners). The cam bone is posed through
turns and translations and every follower must land where the geometry
says, to a few microns.
"""

import json
import math
import os
import sys
import tempfile

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, manifest as man_mod, rig_build  # noqa: E402


def _t(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


E, R = 0.010, 0.030            # the circle's eccentricity and radius
RR = 0.005                     # the rollers' radius
HEX = 0.025                    # the hexagon's circumradius
HALF = 0.010                   # the bands' half height along Z
HEX_Z = 0.04                   # the hexagon band sits above the circle band, on the same axis


def band(profile, z0=0.0):
    """A closed profile (list of (x, y)) extruded into a band along Z."""
    pts, tris = [], []
    n = len(profile)
    for x, y in profile:
        pts.append([x, y, z0 - HALF])
        pts.append([x, y, z0 + HALF])
    for i in range(n):
        j = (i + 1) % n
        a, b, c, d = 2 * i, 2 * i + 1, 2 * j, 2 * j + 1
        tris.append([a, c, d])
        tris.append([a, d, b])
    return pts, tris


CIRCLE = [(E + R * math.cos(2 * math.pi * k / 360), R * math.sin(2 * math.pi * k / 360))
          for k in range(360)]
HEXAGON = [(HEX * math.cos(math.pi / 3 * k), HEX * math.sin(math.pi / 3 * k))
           for k in range(6)]
CIRCLE_PTS, CIRCLE_TRIS = band(CIRCLE)
HEX_PTS, HEX_TRIS = band(HEXAGON, HEX_Z)


def cam(kind, point, pts, tris, **fol):
    follower = {"kind": kind, "point": point, "axis": None, "radius": None, "normal": None}
    follower.update(fol)
    return {"kind": "cam", "driver_joint": "j001", "ratio": None,
            "meters_per_radian": None, "lead_m_per_rev": None,
            "cam": {"axis": [0, 0, 1], "origin": [0, 0, 0],
                    "surface": {"points": pts, "triangles": tris},
                    "follower": follower}}


def joint(jid, parent, child, x, y, coupling=None):
    return {"id": jid, "type": "prismatic", "parent_group": parent, "child_group": child,
            "origin": [x, y, 0], "axis": [1, 0, 0], "secondary_axis": [0, 0, 1],
            "limits": None, "coupling": coupling, "source_mates": [],
            "confidence": "high", "notes": None}


MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "cam-smoke.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "ground-1", "step_name": "ground", "step_occurrence_path": None, "transform": _t(0, 0, 0)},
        {"id": "c002", "sw_path": "cam-1", "step_name": "cam", "step_occurrence_path": None, "transform": _t(0, 0, 0)},
        {"id": "c003", "sw_path": "vertex-1", "step_name": "vertex", "step_occurrence_path": None, "transform": _t(E + R, 0, 0)},
        {"id": "c004", "sw_path": "roller-1", "step_name": "roller", "step_occurrence_path": None, "transform": _t(E + R + RR, 0, 0)},
        {"id": "c005", "sw_path": "rod-1", "step_name": "rod", "step_occurrence_path": None, "transform": _t(E - R, 0, 0)},
        {"id": "c006", "sw_path": "hexroller-1", "step_name": "hexroller", "step_occurrence_path": None, "transform": _t(HEX + RR, 0, HEX_Z)},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "ground", "components": ["c001"], "grounded": True, "frame": None, "bbox_diag": 0.5},
        {"id": "g001", "name": "cam", "components": ["c002"], "grounded": False, "frame": None, "bbox_diag": 0.3},
        {"id": "g002", "name": "vertex", "components": ["c003"], "grounded": False, "frame": None, "bbox_diag": 0.03},
        {"id": "g003", "name": "roller", "components": ["c004"], "grounded": False, "frame": None, "bbox_diag": 0.03},
        {"id": "g004", "name": "rod", "components": ["c005"], "grounded": False, "frame": None, "bbox_diag": 0.03},
        {"id": "g005", "name": "hexroller", "components": ["c006"], "grounded": False, "frame": None, "bbox_diag": 0.03},
    ],
    "joints": [
        {"id": "j001", "type": "planar", "parent_group": "g000", "child_group": "g001",
         "origin": [0, 0, 0], "axis": [0, 0, 1], "secondary_axis": [1, 0, 0],
         "limits": None, "coupling": None, "source_mates": [], "confidence": "high", "notes": None},
        joint("j002", "g000", "g002", E + R, 0,
              cam("vertex", [E + R, 0, 0], CIRCLE_PTS, CIRCLE_TRIS)),
        joint("j003", "g000", "g003", E + R + RR, 0,
              cam("roller", [E + R + RR, 0, 0.004], CIRCLE_PTS, CIRCLE_TRIS, axis=[0, 0, 1], radius=RR)),
        # The rod slides in the vertex follower and its flat end rides the
        # cam's far side: the face normal points +X, into the cam.
        joint("j004", "g002", "g004", E - R, 0,
              cam("flat", [E - R, 0, 0], CIRCLE_PTS, CIRCLE_TRIS, normal=[1, 0, 0])),
        joint("j005", "g000", "g005", HEX + RR, 0,
              cam("roller", [HEX + RR, 0, HEX_Z], HEX_PTS, HEX_TRIS, axis=[0, 0, 1], radius=RR)),
    ],
    "loops": [],
    "warnings": [],
}


def _check(cond, msg):
    if not cond:
        raise SystemExit("rig_cam_smoke: FAIL: " + msg)


def circle_centre(theta, tx, ty):
    return (tx + E * math.cos(theta), ty + E * math.sin(theta))


def expect_vertex(theta, tx, ty):
    cx, cy = circle_centre(theta, tx, ty)
    return cx + math.sqrt(R * R - cy * cy)


def expect_roller(theta, tx, ty):
    cx, cy = circle_centre(theta, tx, ty)
    return cx + math.sqrt((R + RR) ** 2 - cy * cy)


def expect_flat(theta, tx, ty):
    cx, cy = circle_centre(theta, tx, ty)
    return cx - R


def expect_hex_roller(theta, tx, ty):
    """First contact along the ray y = 0 from +X: the largest x over
    boundary points b with |b.y| <= RR of b.x + sqrt(RR^2 - b.y^2)."""
    c, s = math.cos(theta), math.sin(theta)
    best = -1e9
    for k in range(6):
        x0, y0 = HEX * math.cos(math.pi / 3 * k), HEX * math.sin(math.pi / 3 * k)
        x1, y1 = HEX * math.cos(math.pi / 3 * (k + 1)), HEX * math.sin(math.pi / 3 * (k + 1))
        for i in range(4001):
            t = i / 4000
            px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            wx = tx + px * c - py * s
            dy = ty + px * s + py * c
            if abs(dy) <= RR:
                best = max(best, wx + math.sqrt(RR * RR - dy * dy))
    return best


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json", delete=False) as fh:
        json.dump(MANIFEST, fh)
        path = fh.name
    try:
        m = man_mod.load(path)
    finally:
        os.unlink(path)
    for jid in ("j002", "j003", "j004", "j005"):
        c = m.joint_by_id()[jid].coupling
        _check(c.kind == "cam" and c.cam_surface_triangles, "%s did not parse as a cam" % jid)

    plan = graph.build(m)
    names = {bp.group.id: (bp.cam_prj_name, bp.cam_rel_name, bp.cam_off_name) for bp in plan.bones}
    _check(names["g002"][0].startswith("PRJ_"), "no projection bone planned for the vertex follower")
    _check(names["g004"][1].startswith("REL_") and names["g004"][2].startswith("OFF_"),
           "no REL/OFF bones planned for the flat follower")

    result = rig_build.build(bpy.context, m, plan)
    _check(not result.warnings, "warnings: %s" % result.warnings)
    arm = result.armature_object
    _check(len(result.cam_surface_names) == 3, "cam surfaces: %s" % result.cam_surface_names)
    for gid in ("g002", "g003", "g004", "g005"):
        pb = arm.pose.bones[result.bone_names[gid]]
        _check(pb.lock_location[1], "%s's slide is not locked as driven" % gid)

    cam_pb = arm.pose.bones[result.bone_names["g001"]]
    rest = {gid: arm.pose.bones[result.bone_names[gid]].bone.head_local.copy()
            for gid in ("g002", "g003", "g004", "g005")}
    # The cam bone's local axes: Y = the cam axis (world Z), Z = secondary
    # (world X), X = Y cross Z = world Y.
    worst = 0.0
    poses = [(0.0, 0.0, 0.0), (0.7, 0.0, 0.0), (2.5, 0.0, 0.0), (0.0, 0.006, 0.0),
             (0.0, 0.0, -0.005), (1.2, 0.004, 0.003), (4.0, -0.006, 0.007), (5.9, 0.002, -0.004)]
    for theta, tx, ty in poses:
        cam_pb.location = (ty, 0.0, tx)
        cam_pb.rotation_euler = (0.0, theta, 0.0)
        bpy.context.view_layer.update()
        got = {gid: (arm.matrix_world @ arm.pose.bones[result.bone_names[gid]].matrix).translation
               for gid in rest}
        for gid, fn in (("g002", expect_vertex), ("g003", expect_roller),
                        ("g004", expect_flat), ("g005", expect_hex_roller)):
            want = fn(theta, tx, ty)
            err = abs(got[gid].x - want)
            worst = max(worst, err)
            _check(err < 2e-5, "%s at theta %.2f, t (%.3f, %.3f): x %.6f, expected %.6f" % (
                gid, theta, tx, ty, got[gid].x, want))
            _check(abs(got[gid].y - rest[gid].y) < 1e-9 and abs(got[gid].z - rest[gid].z) < 1e-9,
                   "%s left its slide line" % gid)

    # A rebuild replaces the surfaces and the helpers, never doubles them.
    cam_pb.location = (0.0, 0.0, 0.0)
    cam_pb.rotation_euler = (0.0, 0.0, 0.0)
    result2 = rig_build.build(bpy.context, m, graph.build(m))
    surfaces = [o.name for o in bpy.data.objects if o.name.startswith("CADLINK_cam_")]
    _check(len(surfaces) == 3, "surfaces after a rebuild: %s" % surfaces)
    _check(not result2.warnings, "rebuild warnings: %s" % result2.warnings)

    print("rig_cam_smoke: OK: a vertex, two rollers (one round a hexagon's corners) "
          "and a flat face on a moving base follow a cam free in its plane through "
          "%d poses, worst error %.2e m" % (len(poses), worst))


main()
