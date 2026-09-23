# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a send uses the rig of ITS OWN assembly, never merely
the first rig in the scene.

    blender -b --factory-startup --python-exit-code 1 -P ci/bridge_rig_scope_smoke.py

Each assembly keeps a rig of its own, side by side in one scene. The
bridge took the first rig it found in the scene for a send that had no rig
of its own yet. An update then cleared that rig's bones and built the other
assembly's bones inside it, and relink hung the parts on the wrong rig. A
locked rig of one assembly also made a send of a different assembly skip
its own rig and attach its parts to the locked one.

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
from CADder.rig import rig_build, swmesh  # noqa: E402


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


# component id, path, persistent id, x, group, step name
PARTS = [
    ("c001", "base-1", "pbase", 0.0, 0, "base"),
    ("c002", "arm-1", "parm", 0.2, 1, "arm"),
    ("c003", "clip-1", "pclip", 0.4, 2, "clip"),
]
GROUP_NAMES = ("base", "arm", "clip")


def manifest(name, prefix):
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": name + ".step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": cid, "sw_path": prefix + path, "step_name": prefix + sname,
             "step_occurrence_path": None, "sw_persistent_id": prefix + pid,
             "transform": _t(x)}
            for cid, path, pid, x, _g, sname in PARTS],
        "rigid_groups": [
            {"id": "g%03d" % g, "name": prefix + GROUP_NAMES[g],
             "components": [cid for cid, _p, _i, _x, gg, _n in PARTS if gg == g],
             "grounded": g == 0, "frame": None, "bbox_diag": 0.2}
            for g in range(3)],
        "joints": [
            {"id": "j001", "type": "revolute", "parent_group": "g000",
             "child_group": "g001", "origin": [0.2, 0, 0], "axis": [0, 0, 1],
             "secondary_axis": [1, 0, 0], "limits": None},
            {"id": "j002", "type": "revolute", "parent_group": "g001",
             "child_group": "g002", "origin": [0.4, 0, 0], "axis": [0, 0, 1],
             "secondary_axis": [1, 0, 0], "limits": None},
        ],
        "loops": [], "warnings": [],
    }


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path):
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, 1, len(PARTS), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.05, 0, 0, 0, 0.05, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, path_, _pid, x, _g, name in PARTS:
        body += (struct.pack("<i", 1) + _text(cid) + _text(name)
                 + _text(path_) + struct.pack("<16d", *_flat(_t(x)))
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def export(tmp, name, prefix=""):
    man = os.path.join(tmp, name + ".rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(manifest(name, prefix), fh)
    return write_mesh(os.path.join(tmp, name + ".swmesh")), man


def send(mesh, man, update=False, rig_mode=None, **steps):
    payload = {
        "step": None, "mesh": mesh, "manifest": man,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": "FLAT", "up_as": "ZPOS"},
    }
    payload["steps"].update(steps)
    if rig_mode:
        payload["rig_mode"] = rig_mode
    return bridge._run_job(payload)


def fail(where, msg):
    raise SystemExit("bridge_rig_scope_smoke: FAIL: %s: %s" % (where, msg))


def other_rig(tmp, where):
    """The rig of a second assembly, standing in the scene before the
    first one arrives. Built from its manifest alone, as a STEP route rig
    is, so the direct send of the first assembly does not replace it."""
    _mesh, man = export(tmp, "other", prefix="o")
    result = bridge._run_job({
        "step": None, "mesh": None, "manifest": man,
        "steps": {"import": False, "replace": False, "match": False,
                  "sync_poses": False, "build_rig": True, "relink": False,
                  "cleanup": False},
    })
    if not result.get("ok"):
        fail(where, "the other assembly's rig was not built: %s"
             % result.get("error"))
    arm = bpy.data.objects.get("other_Rig")
    if arm is None:
        fail(where, "no other_Rig after the manifest send")
    return arm


def fingerprint(arm):
    return (arm.get("RIG_source"), sorted(b.name for b in arm.data.bones))


def app_parts():
    return [o for o in bpy.data.objects
            if o.get("SWMESH_file") == "app" and o.get("RIG_component_id")]


def rig_holding(obj):
    holder = obj
    while holder is not None and not (holder.parent is not None
                                      and holder.parent.type == "ARMATURE"
                                      and holder.parent_type == "BONE"):
        holder = holder.parent
    return None if holder is None else holder.parent


def check_other_intact(where, arm, before):
    try:
        now = fingerprint(arm)
    except ReferenceError:
        fail(where, "the other assembly's rig was removed")
    if now != before:
        fail(where, "the other assembly's rig changed: before %s, after %s"
             % (before, now))


def check_on_own_rig(where, other):
    parts = app_parts()
    if not parts:
        fail(where, "no parts of the assembly in the scene")
    for obj in parts:
        arm = rig_holding(obj)
        if arm is None:
            fail(where, "%s is on no rig" % obj.name)
        if arm is other:
            fail(where, "%s hangs on %s, the other assembly's rig"
                 % (obj.name, other.name))
        if not str(arm.get("RIG_source") or "").endswith("app.rig.json"):
            fail(where, "%s hangs on %s, which is not the rig of its "
                 "assembly" % (obj.name, arm.name))


def check_off_other(where, other):
    for obj in app_parts():
        if rig_holding(obj) is other:
            fail(where, "%s hangs on %s, the other assembly's rig"
                 % (obj.name, other.name))


def first_rig():
    for obj in bpy.context.scene.objects:
        if obj.type == "ARMATURE" and obj.get("RIG_rig"):
            return obj
    return None


def run_no_rig_of_its_own(tmp, mode):
    """The assembly stands in the scene without a rig (sent with Build Rig
    off), and a different assembly's rig stands beside it."""
    where = "no rig of its own: " + mode
    bpy.ops.wm.read_factory_settings(use_empty=True)
    other = other_rig(tmp, where)
    before = fingerprint(other)
    mesh, man = export(tmp, "app")
    result = send(mesh, man, build_rig=False, relink=False)
    if not result.get("ok"):
        fail(where, "the first send failed: %s" % result.get("error"))
    result = send(mesh, man, update=True, rig_mode=mode)
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    check_other_intact(where, other, before)
    check_on_own_rig(where, other)
    return where


def run_not_in_scene(tmp, mode):
    """Refresh of an assembly the scene does not hold: the update becomes
    an import, and a different assembly's rig stands in the scene."""
    where = "not in the scene: " + mode
    bpy.ops.wm.read_factory_settings(use_empty=True)
    other = other_rig(tmp, where)
    before = fingerprint(other)
    mesh, man = export(tmp, "app")
    result = send(mesh, man, update=True, rig_mode=mode)
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    check_other_intact(where, other, before)
    check_on_own_rig(where, other)
    return where


def run_keep_relinks_own(tmp):
    """KEEP binds the parts again to the rig they came off, also when a
    different assembly's rig comes first in the scene."""
    where = "keep relinks to its own rig"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    other = other_rig(tmp, where)
    before = fingerprint(other)
    mesh, man = export(tmp, "app")
    if not send(mesh, man).get("ok"):
        fail(where, "the first send failed")
    check_on_own_rig(where + " after the send", other)
    if first_rig() is not other:
        fail(where, "the test needs the other rig first in the scene, "
             "found %s" % first_rig().name)
    result = send(mesh, man, update=True, rig_mode="KEEP")
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    check_other_intact(where, other, before)
    check_on_own_rig(where, other)
    return where


def run_other_locked_send(tmp):
    """A different assembly's rig is locked, and this assembly is sent for
    the first time. Its parts must not go on the locked rig."""
    where = "other rig locked: send"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    other = other_rig(tmp, where)
    other[rig_build.LOCK_TAG] = True
    before = fingerprint(other)
    mesh, man = export(tmp, "app")
    result = send(mesh, man)
    if not result.get("ok"):
        fail(where, "the send failed: %s" % result.get("error"))
    check_other_intact(where, other, before)
    check_off_other(where, other)
    rig_stage = (result.get("stages") or {}).get("rig") or {}
    if rig_stage.get("locked") != other.name:
        fail(where, "the reply does not name the locked rig: %s" % rig_stage)
    return where


def run_other_locked_refresh(tmp):
    """A different assembly's rig is locked, and this assembly, with a rig
    of its own, is refreshed with KEEP."""
    where = "other rig locked: refresh"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    other = other_rig(tmp, where)
    mesh, man = export(tmp, "app")
    if not send(mesh, man).get("ok"):
        fail(where, "the first send failed")
    other[rig_build.LOCK_TAG] = True
    before = fingerprint(other)
    result = send(mesh, man, update=True, rig_mode="KEEP")
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    check_other_intact(where, other, before)
    check_on_own_rig(where, other)
    rig_stage = (result.get("stages") or {}).get("rig") or {}
    if rig_stage.get("mode") != "KEEP":
        fail(where, "the rig of the assembly was not kept: %s" % rig_stage)
    return where


def main():
    tmp = os.path.join(tempfile.gettempdir(), "bridge_rig_scope_smoke")
    os.makedirs(tmp, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    done = []
    for mode in ("KEEP", "APPEND", "REGENERATE"):
        done.append(run_no_rig_of_its_own(tmp, mode))
        done.append(run_not_in_scene(tmp, mode))
    done.append(run_keep_relinks_own(tmp))
    done.append(run_other_locked_send(tmp))
    done.append(run_other_locked_refresh(tmp))
    print("bridge_rig_scope_smoke: OK: %d sends each used the rig of their "
          "own assembly" % len(done))


main()
