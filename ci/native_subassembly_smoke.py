# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a RIGID SUBASSEMBLY on the direct link.

    blender -b --factory-startup -P ci/native_subassembly_smoke.py

SolidWorks solves a rigid subassembly as one body and the manifest says so:
one component, one rigid group, one bone. What arrives in Blender must
still be the parts, because that is what a STEP import of the same assembly
gives and what the outliner is for. Before 2026-09-16 the add-in welded
everything under a rigid subassembly into one mesh named after the
subassembly, so a part used inside one and again outside it could not share
a mesh and lost its own name (Oscar, cam-follower).

The file here is a version-3 .swmesh: it carries the occurrence paths and
the branches of the tree itself, so the hierarchy no longer depends on the
rig manifest and a send with no rig still nests.
"""

import os
import struct
import sys
import tempfile

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from CADder.rig import manifest as man_mod, native_import, pose_sync, swmesh  # noqa: E402
from CADder import rig  # noqa: E402


def _t(x, y=0.0, z=0.0):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "cam.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "rod-1", "step_name": "rod",
         "step_occurrence_path": None, "transform": _t(0.0)},
        # The whole subassembly, as one component: the parts inside it have
        # no component of their own.
        {"id": "c002", "sw_path": "lift-1", "step_name": "lifter",
         "step_occurrence_path": None, "transform": _t(0.5),
         "subassembly_solving": "rigid"},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "rod", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.1},
        {"id": "g001", "name": "lifter", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.2},
    ],
    "joints": [],
    "loops": [],
    "warnings": [],
}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path):
    """One rod definition used twice, once loose and once inside the
    subassembly, plus a block that only the subassembly holds."""
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    # materials, definitions, instances, nodes
    body += struct.pack("<IIII", 1, 2, 3, 1)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)

    for did, name, size in ((1, "rod", 0.1), (2, "block", 0.2)):
        body += struct.pack("<i", did) + _text(name)
        body += struct.pack("<II", 3, 1)
        body += struct.pack("<9f", 0, 0, 0, size, 0, 0, 0, size, 0)
        body += struct.pack("<3i", 0, 1, 2)
        body += struct.pack("<i", 0)

    # The third column is where the part sits INSIDE its component: the
    # loose rod IS its component and the block sits at the subassembly's
    # origin, so only the inner rod carries one.
    for did, cid, name, path_, x, local in (
            (1, "c001", "rod", "rod-1", 0.0, None),
            (2, "c002", "block", "lift-1/block-1", 0.5, _t(0.0)),
            (1, "c002", "rod", "lift-1/rod-1", 0.6, _t(0.1))):
        body += (struct.pack("<i", did) + _text(cid) + _text(name)
                 + _text(path_) + struct.pack("<16d", *_flat(_t(x))))
        body += struct.pack("<B", 0 if local is None else 1)
        if local is not None:
            body += struct.pack("<16d", *_flat(local))

    body += (_text("lift-1") + _text("lifter") + _text("c002")
             + struct.pack("<16d", *_flat(_t(0.5))))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def _check(cond, msg):
    if not cond:
        raise SystemExit("native_subassembly_smoke: FAIL: " + msg)


def _by_path(path):
    for obj in bpy.data.objects:
        if obj.get("SWMESH_path") == path:
            return obj
    return None


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    path = write_mesh(os.path.join(tempfile.gettempdir(), "cam.swmesh"))
    m = man_mod.parse(MANIFEST)

    objs, report = native_import.build(bpy.context, path, manifest=m,
                                       hierarchy="TREE")
    _check(len(objs) == 3, "built %d objects, want 3" % len(objs))

    loose = _by_path("rod-1")
    inner = _by_path("lift-1/rod-1")
    block = _by_path("lift-1/block-1")
    _check(None not in (loose, inner, block), "an occurrence path went missing")

    # The point of the whole change: one mesh, two placements, at two
    # levels of the assembly.
    _check(loose.data is inner.data,
           "the rod inside the subassembly does not share the loose rod's "
           "mesh: %s vs %s" % (loose.data.name, inner.data.name))
    _check(loose.data.name.startswith("rod"),
           "the mesh is not named after the part: %s" % loose.data.name)
    _check(inner.name.startswith("rod"),
           "the part inside the subassembly is named %s" % inner.name)

    # TREE: a collection for the subassembly, named after the DOCUMENT as
    # the STEP route names its products.
    lifter = bpy.data.collections.get("lifter")
    _check(lifter is not None, "no collection for the subassembly")
    _check(sorted(o.name for o in lifter.objects) == sorted([inner.name, block.name]),
           "the subassembly's parts are not in its collection: %s"
           % [o.name for o in lifter.objects])
    _check(loose.name in [o.name for o in bpy.data.collections["cam"].objects],
           "the loose rod is not at the root")

    # Every part of the subassembly is the SAME component and so the same
    # body of the rig, which is what makes it rigid.
    _check(inner.get("RIG_component_id") == "c002"
           and block.get("RIG_component_id") == "c002",
           "the subassembly's parts do not carry its component id")
    _check(inner.get("RIG_group") == "g001" and block.get("RIG_group") == "g001",
           "the subassembly's parts are not in its rigid group")

    # Where each part sits INSIDE the component: the block at its origin,
    # the rod 100 mm along.
    _check(loose.get("SWMESH_local") is None,
           "a part that is its own component carries an offset")
    _check([round(v, 4) for v in block.get("SWMESH_local")]
           == [round(v, 4) for v in _flat(_t(0.0))],
           "the block's place in the subassembly is wrong")
    local = list(inner.get("SWMESH_local"))
    _check(round(local[3], 4) == 0.1,
           "the inner rod's offset in the subassembly is %s" % round(local[3], 4))

    # EMPTIES: one empty for the subassembly, at its pose, with the parts
    # under it.
    objs, report = native_import.build(bpy.context, path, manifest=m,
                                       hierarchy="EMPTIES")
    emp = bpy.data.objects.get("lifter")
    _check(emp is not None and emp.type == "EMPTY",
           "no empty named after the subassembly")
    _check(round(emp.matrix_world.translation.x, 3) == 0.5,
           "the empty is not at the subassembly's pose")
    for obj in (_by_path("lift-1/rod-1"), _by_path("lift-1/block-1")):
        _check(obj.parent is emp, "%s is not under the subassembly" % obj.name)

    # And the pose stage moves the subassembly as ONE body: the manifest
    # says the component is 500 mm further along, so every part of it moves
    # 500 mm and keeps its place inside.
    for component in m.components:
        if component.id == "c002":
            component.transform = _t(1.0)
    out = pose_sync.sync(m, report)
    _check(not out.skipped, "pose sync skipped: %s" % out.skipped)
    # sync leaves the depsgraph to its caller, and a parented part's world
    # matrix is only right once it has run.
    bpy.context.view_layer.update()
    inner = _by_path("lift-1/rod-1")
    block = _by_path("lift-1/block-1")
    _check(round(inner.matrix_world.translation.x, 3) == 1.1,
           "the rod inside the subassembly landed at %.3f, not 1.1"
           % inner.matrix_world.translation.x)
    _check(round(block.matrix_world.translation.x, 3) == 1.0,
           "the block landed at %.3f, not 1.0" % block.matrix_world.translation.x)

    native_import.remove_previous()
    _check(bpy.data.objects.get("block") is None, "objects left behind")

    print("native_subassembly_smoke: OK: a rigid subassembly arrives as its "
          "parts, sharing meshes with the same part outside it, nested under "
          "its own branch, in one rigid group, and moves as one body")


main()
