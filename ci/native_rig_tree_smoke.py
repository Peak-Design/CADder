# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the assembly tree under a rig, on a send with
parented empties.

Two subassemblies hang off a fixed base. lift-1 is rigid: one component,
two parts, one bone. Its empty must go on that bone with both parts still
under it, so the tree survives the rig and the parts move with the bone.
flex-1 is flexible: each of its two parts rides a bone of its own. Its
empty cannot hold them, so the parts go to their bones and the empty,
which then holds nothing, is removed.

Run:  blender -b --factory-startup -P native_rig_tree_smoke.py
"""

import json
import os
import struct
import sys
import tempfile

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from CADder.rig import (  # noqa: E402
    graph, manifest as man_mod, native_import, parenting, rig_build, swmesh)


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


def _hinge(jid, parent, child, x):
    return {"id": jid, "type": "revolute", "parent_group": parent,
            "child_group": child, "origin": [x, 0, 0], "axis": [0, 0, 1],
            "secondary_axis": [1, 0, 0], "limits": None}


MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "tree.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "base-1", "step_name": "base",
         "step_occurrence_path": None, "transform": _t(0.0)},
        {"id": "c002", "sw_path": "lift-1", "step_name": "lifter",
         "step_occurrence_path": None, "transform": _t(0.5),
         "subassembly_solving": "rigid"},
        {"id": "c003", "sw_path": "flex-1/left-1", "step_name": "left",
         "step_occurrence_path": None, "transform": _t(1.0)},
        {"id": "c004", "sw_path": "flex-1/right-1", "step_name": "right",
         "step_occurrence_path": None, "transform": _t(1.2)},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"],
         "grounded": True, "frame": None, "bbox_diag": 0.1},
        {"id": "g001", "name": "lifter", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.2},
        {"id": "g002", "name": "left", "components": ["c003"],
         "grounded": False, "frame": None, "bbox_diag": 0.1},
        {"id": "g003", "name": "right", "components": ["c004"],
         "grounded": False, "frame": None, "bbox_diag": 0.1},
    ],
    "joints": [
        _hinge("j001", "g000", "g001", 0.5),
        _hinge("j002", "g000", "g002", 1.0),
        _hinge("j003", "g002", "g003", 1.2),
    ],
    "loops": [],
    "warnings": [],
}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path):
    data = struct.pack("<III", swmesh.MAGIC, 3, 0)
    data += struct.pack("<d", 0.0005)
    data += struct.pack("<IIII", 1, 1, 5, 2)    # materials, defs, instances, nodes
    data += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    data += _text("") + struct.pack("<I", 0)
    data += struct.pack("<i", 1) + _text("blob") + struct.pack("<II", 3, 1)
    data += struct.pack("<9f", 0, 0, 0, 0.1, 0, 0, 0, 0.1, 0)
    data += struct.pack("<3i", 0, 1, 2) + struct.pack("<i", 0)
    for cid, name, path_, x, local in (
            ("c001", "base", "base-1", 0.0, None),
            ("c002", "block", "lift-1/block-1", 0.5, _t(0.0)),
            ("c002", "pin", "lift-1/pin-1", 0.6, _t(0.1)),
            ("c003", "left", "flex-1/left-1", 1.0, None),
            ("c004", "right", "flex-1/right-1", 1.2, None)):
        data += (struct.pack("<i", 1) + _text(cid) + _text(name)
                 + _text(path_) + struct.pack("<16d", *_flat(_t(x))))
        data += struct.pack("<B", 0 if local is None else 1)
        if local is not None:
            data += struct.pack("<16d", *_flat(local))
    for path_, name, cid, x in (("lift-1", "lifter", "c002", 0.5),
                                ("flex-1", "flexer", "", 1.0)):
        data += (_text(path_) + _text(name) + _text(cid)
                 + struct.pack("<16d", *_flat(_t(x))))
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def check(ok, what):
    if not ok:
        raise SystemExit("native_rig_tree_smoke: FAIL: " + what)


def by_path(path):
    for obj in bpy.data.objects:
        if obj.get("SWMESH_path") == path:
            return obj
    return None


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json", delete=False) as fh:
        json.dump(MANIFEST, fh)
        manifest_path = fh.name
    try:
        m = man_mod.load(manifest_path)
    finally:
        os.unlink(manifest_path)
    mesh = write_mesh(os.path.join(tempfile.gettempdir(), "native_rig_tree_smoke.swmesh"))
    objects, report = native_import.build(bpy.context, mesh, manifest=m,
                                          hierarchy="EMPTIES")
    check(bpy.data.objects.get("lifter") is not None, "no lifter empty")
    check(bpy.data.objects.get("flexer") is not None, "no flexer empty")

    plan = graph.build(m)
    result = rig_build.build(bpy.context, m, plan, report.frame_rows)
    arm = result.armature_object
    out = parenting.relink(bpy.context, arm)
    check(not out.violations, "parts moved while parenting: %s" % out.violations)

    # 1. The rigid subassembly keeps its tree, on its bone.
    lifter = bpy.data.objects.get("lifter")
    block, pin = by_path("lift-1/block-1"), by_path("lift-1/pin-1")
    check(lifter.parent is arm and lifter.parent_type == "BONE",
          "the lifter empty is not on a bone (parent %s)" % lifter.parent)
    check(block.parent is lifter and pin.parent is lifter,
          "the lifter's parts left their empty")
    check(out.carried == ["lifter"], "carried %s" % out.carried)

    # 2. The flexible one cannot keep it: its parts are on their bones, and
    #    its empty, which holds nothing now, is gone.
    left, right = by_path("flex-1/left-1"), by_path("flex-1/right-1")
    check(left.parent is arm and right.parent is arm,
          "the flexible parts are not on their bones")
    check(left.parent_bone != right.parent_bone, "the flexible parts share a bone")
    check(bpy.data.objects.get("flexer") is None, "the bare flexer empty is still there")
    check(out.removed_empties == 1, "removed %d empties" % out.removed_empties)

    # 3. The lifter's bone moves the whole subassembly.
    was = pin.matrix_world.translation.copy()
    offset = lifter.matrix_world.inverted() @ pin.matrix_world
    # A hinge bone turns about its own Y axis, the joint axis.
    pb = arm.pose.bones[lifter.parent_bone]
    pb.rotation_euler[1] = 0.5
    bpy.context.view_layer.update()
    check((pin.matrix_world.translation - was).length > 1e-3,
          "the pin did not move with the lifter's bone")
    still = lifter.matrix_world.inverted() @ pin.matrix_world
    check(all(abs(a - b) < 1e-6 for ra, rb in zip(offset, still)
              for a, b in zip(ra, rb)),
          "the pin moved inside the subassembly")
    pb.rotation_euler[1] = 0.0
    bpy.context.view_layer.update()

    # 4. A second relink, as a rig rebuild does, leaves it as it is.
    again = parenting.relink(bpy.context, arm)
    check(not again.violations, "the second relink moved parts")
    check(lifter.parent is arm and block.parent is lifter,
          "the second relink broke the tree")

    print("native_rig_tree_smoke: OK: a rigid subassembly keeps its tree on "
          "its bone and moves as one, and the bare empty of a flexible one "
          "is removed")


main()
