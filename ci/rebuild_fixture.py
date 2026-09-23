# SPDX-License-Identifier: GPL-3.0-or-later
"""What the Rebuild from CAD smokes share: an assembly written as the CAD
add-in writes one (a .swmesh and a rig manifest), a send of it into
Blender, and a stand-in for the CAD application's replies.

Not a smoke of its own. The smokes import it as CADder.ci.rebuild_fixture.
"""

import json
import math
import os
import struct

import bpy

from CADder import bridge
from CADder.rig import cad_link, swmesh


def translate(x, y=0.0, z=0.0):
    return [[1.0, 0.0, 0.0, x], [0.0, 1.0, 0.0, y], [0.0, 0.0, 1.0, z],
            [0.0, 0.0, 0.0, 1.0]]


def mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def turn_z(angle, about=(0.0, 0.0, 0.0)):
    """A turn about the Z axis through the point `about`."""
    c, s = math.cos(angle), math.sin(angle)
    rot = [[c, -s, 0.0, 0.0], [s, c, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0],
           [0.0, 0.0, 0.0, 1.0]]
    return mul(translate(*about), mul(rot, translate(-about[0], -about[1],
                                                      -about[2])))


def flat(rows):
    return [float(v) for row in rows for v in row]


class Part:
    """One placement of the assembly, as the export names it."""

    def __init__(self, cid, path, pid, rows, group, name=None):
        self.cid = cid
        self.path = path
        self.pid = pid
        self.rows = rows
        self.group = group
        self.name = name or path.rpartition("/")[2].rpartition("-")[0]


def manifest(parts, group_names, joints, name="app"):
    groups = {}
    for part in parts:
        groups.setdefault(part.group, []).append(part.cid)
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": name + ".step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": p.cid, "sw_path": p.path, "step_name": p.name,
             "step_occurrence_path": None, "sw_persistent_id": p.pid,
             "transform": p.rows}
            for p in parts],
        "rigid_groups": [
            {"id": "g%03d" % g, "name": group_names[g], "components": cids,
             "grounded": g == 0, "frame": None, "bbox_diag": 0.2}
            for g, cids in sorted(groups.items())],
        "joints": joints,
        "loops": [], "warnings": [],
    }


def revolute(jid, parent, child, origin, limits=None):
    return {"id": jid, "type": "revolute", "parent_group": "g%03d" % parent,
            "child_group": "g%03d" % child, "origin": list(origin),
            "axis": [0.0, 0.0, 1.0], "secondary_axis": [1.0, 0.0, 0.0],
            "limits": limits}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, parts):
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, 1, len(parts), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.05, 0, 0, 0, 0.05, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for p in parts:
        body += (struct.pack("<i", 1) + _text(p.cid) + _text(p.name)
                 + _text(p.path) + struct.pack("<16d", *flat(p.rows))
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def export(tmp, parts, group_names, joints, name="app"):
    """(mesh path, manifest path), as an export writes them."""
    man = os.path.join(tmp, name + ".rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(manifest(parts, group_names, joints, name), fh)
    return write_mesh(os.path.join(tmp, name + ".swmesh"), parts), man


def send(mesh, man, hierarchy, up_as, update=False, rig_mode=None,
         steps=None):
    """The payload the add-in posts for a send (or a Refresh Model)."""
    payload = {
        "step": None, "mesh": mesh, "manifest": man,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": hierarchy, "up_as": up_as},
    }
    payload["steps"].update(steps or {})
    if rig_mode:
        payload["rig_mode"] = rig_mode
    return bridge._run_job(payload)


def poses_reply(parts):
    """What the add-in answers to "poses": each placement under the id of
    the CAD application's CURRENT walk, with its persistent id."""
    return {"ok": True, "document": "app.SLDASM",
            "components": [{"id": p.cid, "sw_path": p.path,
                            "sw_persistent_id": p.pid,
                            "transform": flat(p.rows)} for p in parts],
            "missing": []}


class FakeCad:
    """Stands in for cad_link.request while it is in a `with` block.

    `answers` maps an op name to a function of the request fields that
    returns the reply. Every request is kept in `seen`."""

    def __init__(self, **answers):
        self.answers = answers
        self.seen = []
        self._real = None

    def request(self, op, timeout=None, instance=None, **fields):
        self.seen.append(dict(fields, op=op))
        answer = self.answers.get(op)
        if answer is None:
            raise cad_link.CadLinkError("the stand-in has no answer for " + op)
        reply = answer(fields)
        if not reply.get("ok"):
            raise cad_link.CadLinkError(reply.get("error") or "refused")
        return reply

    def __enter__(self):
        self._real = cad_link.request
        cad_link.request = self.request
        return self

    def __exit__(self, *exc):
        cad_link.request = self._real
        return False


def rig_object():
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and obj.get("RIG_rig"):
            return obj
    return None


def parts_by_component():
    """component id -> the object that carries it."""
    out = {}
    for obj in bpy.data.objects:
        cid = obj.get("RIG_component_id")
        if cid and obj.get("SWMESH_prototype") is None:
            out[cid] = obj
    return out


def bone_of(arm_obj, group):
    for pb in arm_obj.pose.bones:
        if pb.get("RIG_group") == group:
            return pb
    return None


def off(a, b):
    return max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


def select_none():
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
