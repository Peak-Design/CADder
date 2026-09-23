# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a send without a manifest brings untagged geometry.

    blender -b --factory-startup --python-exit-code 1 -P ci/bridge_no_manifest_smoke.py

The add-in sends no manifest for a part document, and none for an assembly
that the user sends without its rig after a mate error. The bridge still
gave the import the manifest of the LAST send. Component ids are
positional (c001, c002), so the parts took the group ids and persistent
ids of a different assembly, and the next refresh paired the parts by
those wrong persistent ids: one part took the place and the shape of
another.

A refresh without a manifest of an assembly that has a rig must also keep
the group ids its parts already have, or the rig loses track of them.

Every send runs through bridge._run_job, the path SolidWorks drives.
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


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


# component id, path, persistent id, x, group, name
ASSEMBLY_A = [
    ("c001", "base-1", "pA1", 0.0, 0, "base"),
    ("c002", "arm-1", "pA2", 0.2, 1, "arm"),
    ("c003", "clip-1", "pA3", 0.4, 1, "clip"),
]


def manifest(parts, name):
    groups = {}
    for cid, _path, _pid, _x, g, _name in parts:
        groups.setdefault(g, []).append(cid)
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": name + ".step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": cid, "sw_path": path, "step_name": sname,
             "step_occurrence_path": None, "sw_persistent_id": pid,
             "transform": _t(x)}
            for cid, path, pid, x, _g, sname in parts],
        "rigid_groups": [
            {"id": "g%03d" % g, "name": "group%d" % g, "components": cids,
             "grounded": g == 0, "frame": None, "bbox_diag": 0.2}
            for g, cids in sorted(groups.items())],
        "joints": [
            {"id": "j001", "type": "revolute", "parent_group": "g000",
             "child_group": "g001", "origin": [0.2, 0, 0], "axis": [0, 0, 1],
             "secondary_axis": [1, 0, 0], "limits": None},
        ],
        "loops": [], "warnings": [],
    }


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, parts, size=0.05):
    """parts: (component id, path, x, name). One triangle per definition,
    of `size`, so a test can tell the shapes apart."""
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, len(parts), len(parts), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    for i, (_cid, _path, _x, name) in enumerate(parts):
        s = size * (i + 1)
        body += struct.pack("<i", i + 1) + _text(name)
        body += struct.pack("<II", 3, 1)
        body += struct.pack("<9f", 0, 0, 0, s, 0, 0, 0, s, 0)
        body += struct.pack("<3i", 0, 1, 2)
        body += struct.pack("<i", 0)
    for i, (cid, path_, x, name) in enumerate(parts):
        body += (struct.pack("<i", i + 1) + _text(cid) + _text(name)
                 + _text(path_) + struct.pack("<16d", *_flat(_t(x)))
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def send(mesh, man, update=False, rig_mode=None):
    have = bool(man)
    payload = {
        "step": None, "mesh": mesh, "manifest": man,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": have, "sync_poses": have, "build_rig": have,
                  "relink": have, "cleanup": have},
        "import_options": {"hierarchy_types": "FLAT", "up_as": "ZPOS"},
    }
    if rig_mode:
        payload["rig_mode"] = rig_mode
    result = bridge._run_job(payload)
    if not result.get("ok"):
        fail("send", "the send failed: %s" % result.get("error"))
    return result


def send_a(tmp):
    man = os.path.join(tmp, "a.rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(manifest(ASSEMBLY_A, "a"), fh)
    mesh = write_mesh(os.path.join(tmp, "a.swmesh"),
                      [(c, p, x, n) for c, p, _i, x, _g, n in ASSEMBLY_A])
    send(mesh, man)
    return mesh


def fail(where, msg):
    raise SystemExit("bridge_no_manifest_smoke: FAIL: %s: %s" % (where, msg))


def of_file(stem):
    return {o.get("SWMESH_path"): o for o in bpy.data.objects
            if o.get("SWMESH_file") == stem and o.get("RIG_component_id")}


def check_untagged(where, stem):
    for path, obj in sorted(of_file(stem).items()):
        for tag in ("RIG_group", "SWMESH_persistent_id"):
            if tag in obj.keys():
                fail(where, "%s (%s) carries %s %r from a different "
                     "assembly's manifest" % (obj.name, path, tag, obj[tag]))
    if rig_ui._STATE.get("manifest") is not None:
        fail(where, "the last assembly's manifest is still loaded")


def run_part_document(tmp):
    where = "part document after an assembly"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    send_a(tmp)
    mesh = write_mesh(os.path.join(tmp, "b.swmesh"),
                      [("c001", "b-1", 0.0, "b")])
    send(mesh, None)
    check_untagged(where, "b")
    return where


def run_refresh_without_rig(tmp):
    where = "assembly sent and refreshed without its rig"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    send_a(tmp)
    mesh = os.path.join(tmp, "b.swmesh")
    write_mesh(mesh, [("c001", "x-1", 0.0, "x"), ("c002", "y-1", 0.2, "y")])
    send(mesh, None)
    check_untagged(where + " (send)", "b")
    before = of_file("b")
    names = {path: obj.name for path, obj in before.items()}
    # A part goes in front of x in CAD: every component id moves on by one.
    write_mesh(mesh, [("c001", "z-1", 0.4, "z"), ("c002", "x-1", 0.0, "x"),
                      ("c003", "y-1", 0.2, "y")])
    send(mesh, None, update=True, rig_mode="KEEP")
    check_untagged(where + " (refresh)", "b")
    after = of_file("b")
    for path in ("x-1", "y-1"):
        obj = after.get(path)
        if obj is None or obj.name != names[path]:
            fail(where, "the object of %s is now %s, it was %s"
                 % (path, None if obj is None else obj.name, names[path]))
    if "z-1" not in after:
        fail(where, "the new part z did not arrive")
    return where


def run_own_rig_kept(tmp):
    where = "rigged assembly refreshed without its manifest"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh = send_a(tmp)
    groups = {p: o.get("RIG_group") for p, o in of_file("a").items()}
    if None in groups.values():
        fail(where, "the first send tagged no groups: %s" % groups)
    send(mesh, None, update=True, rig_mode="KEEP")
    for path, obj in sorted(of_file("a").items()):
        if obj.get("RIG_group") != groups[path]:
            fail(where, "%s lost its group: %s, it was %s"
                 % (obj.name, obj.get("RIG_group"), groups[path]))
        if obj.parent is None or obj.parent.type != "ARMATURE":
            fail(where, "%s came off the rig" % obj.name)
    return where


def main():
    tmp = os.path.join(tempfile.gettempdir(), "bridge_no_manifest_smoke")
    os.makedirs(tmp, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    done = [run_part_document(tmp), run_refresh_without_rig(tmp),
            run_own_rig_kept(tmp)]
    print("bridge_no_manifest_smoke: OK: %d sends without a manifest brought "
          "the geometry without a different assembly's tags" % len(done))


main()
