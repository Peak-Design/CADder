# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Match Geometry after a direct send.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_match_after_send_smoke.py

The STEP Rig panel says its stages are for putting one right after a send.
Match Geometry took the component and group tags off the parts of a rigid
subassembly, because a direct-send part has no STEP name, and the next
Relink left them off the rig. After Match Geometry and Relink here, every
part must still ride its bone, in a Z-up and a Y-up send.
"""

import json
import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import swmesh, ui as rig_ui  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "rig_match_after_send_smoke")


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


# component, path, x, local x inside the component
PARTS = [("c001", "base-1", 0.0, None),
         ("c002", "lifter-1/rod-1", 0.6, 0.1),
         ("c002", "lifter-1/barrel-1", 0.5, 0.0)]


def export():
    os.makedirs(TMP, exist_ok=True)
    man = os.path.join(TMP, "lift.rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump({
            "manifest_version": "1.0.0",
            "generator": {"name": "Peak.Cadder", "version": "smoke"},
            "units": {"length": "meter", "angle": "radian"},
            "frame": {"handedness": "right", "up_axis": "Z",
                      "transform_convention": "row_major_4x4_global"},
            "step_export": {"file": "lift.step", "ap": "AP214",
                            "sha1": None, "occurrence_matching": None},
            "components": [
                {"id": "c001", "sw_path": "base-1", "step_name": "base",
                 "step_occurrence_path": None, "sw_persistent_id": "pbase",
                 "transform": _t(0.0)},
                {"id": "c002", "sw_path": "lifter-1", "step_name": "lifter",
                 "step_occurrence_path": None, "sw_persistent_id": "plift",
                 "transform": _t(0.5), "subassembly_solving": "rigid"}],
            "rigid_groups": [
                {"id": "g000", "name": "base", "components": ["c001"],
                 "grounded": True, "frame": None, "bbox_diag": 0.2},
                {"id": "g001", "name": "lifter", "components": ["c002"],
                 "grounded": False, "frame": None, "bbox_diag": 0.2}],
            "joints": [
                {"id": "j001", "type": "revolute", "parent_group": "g000",
                 "child_group": "g001", "origin": [0.5, 0, 0],
                 "axis": [0, 0, 1], "secondary_axis": [1, 0, 0],
                 "limits": None}],
            "loops": [], "warnings": [],
        }, fh)
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, 1, len(PARTS), 1)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.05, 0, 0, 0, 0.05, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, path, x, local in PARTS:
        body += (struct.pack("<i", 1) + _text(cid)
                 + _text(path.rpartition("/")[2]) + _text(path)
                 + struct.pack("<16d", *_flat(_t(x))))
        body += struct.pack("<B", 0 if local is None else 1)
        if local is not None:
            body += struct.pack("<16d", *_flat(_t(local)))
    body += (_text("lifter-1") + _text("lifter") + _text("c002")
             + struct.pack("<16d", *_flat(_t(0.5))))
    mesh = os.path.join(TMP, "lift.swmesh")
    with open(mesh, "wb") as fh:
        fh.write(body)
    return mesh, man


def fail(where, msg):
    raise SystemExit("rig_match_after_send_smoke: FAIL: %s: %s" % (where, msg))


def on_a_bone(obj):
    holder = obj
    while holder is not None:
        if holder.parent is not None and holder.parent.type == "ARMATURE" \
                and holder.parent_type == "BONE":
            return holder.parent_bone
        holder = holder.parent
    return None


def run(up_as):
    where = "up " + up_as
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh, man = export()
    result = bridge._run_job({
        "step": None, "mesh": mesh, "manifest": man,
        "steps": {"import": False, "replace": True, "match": True,
                  "sync_poses": True, "build_rig": True, "relink": True,
                  "cleanup": True},
        "import_options": {"hierarchy_types": "FLAT", "up_as": up_as},
    })
    if not result.get("ok"):
        fail(where, "the send failed: %s" % result.get("error"))
    parts = {o.get("SWMESH_path"): o for o in bpy.data.objects
             if o.get("SWMESH_path")}
    before = {p: on_a_bone(o) for p, o in parts.items()}
    worlds = {p: o.matrix_world.copy() for p, o in parts.items()}
    if not all(before.values()):
        fail(where, "the send left parts off the rig: %s" % before)

    if "FINISHED" not in bpy.ops.cadlink.match_geometry():
        fail(where, "Match Geometry did not finish")
    report = rig_ui._STATE["match_report"]
    if report.unmatched:
        fail(where, "Match Geometry left %s unmatched" % report.unmatched)
    if report.frame_agree <= 0:
        fail(where, "Match Geometry lost the frame of the send")
    for path, obj in parts.items():
        for tag in ("RIG_component_id", "RIG_group"):
            if obj.get(tag) is None:
                fail(where, "Match Geometry took %s off %s" % (tag, path))

    if "FINISHED" not in bpy.ops.cadlink.relink_geometry():
        fail(where, "Relink did not finish")
    bpy.context.view_layer.update()
    for path, obj in parts.items():
        if on_a_bone(obj) != before[path]:
            fail(where, "%s rides %s after Relink, %s before"
                 % (path, on_a_bone(obj), before[path]))
        d = (obj.matrix_world.translation - worlds[path].translation).length
        if d > 1e-5:
            fail(where, "%s moved %.5f" % (path, d))


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    for up_as in ("ZPOS", "YPOS"):
        run(up_as)
    print("rig_match_after_send_smoke: OK: Match Geometry and Relink after a "
          "direct send kept every part on its bone, Z up and Y up")


main()
