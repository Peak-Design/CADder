# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for a direct send into a scene whose Unit Scale is not 1.

    blender -b --factory-startup --python-exit-code 1 -P ci/native_unit_scale_smoke.py

The .swmesh and the manifest are in meters. Blender puts a meter at
1 / Unit Scale Blender units, and the rig build, pose sync and the STEP
import all convert with that value. The direct link placed its geometry as
if one Blender unit were one meter. In a millimeter scene (Unit Scale
0.001), pose sync then moved every part a thousand times further out
without scaling its mesh, and the rig was built at those places: small
parts, far apart, and bones that did not meet them. Lengths typed in the
scene went the other way: a Custom distance of 0.8 mm went to the CAD
application as 0.8 m.

The send and the Refresh here go through bridge._run_job, as a send from
SolidWorks does, at Unit Scale 1 and at 0.001. Both must give the same
machine, a thousand times larger in Blender units at 0.001.
"""

import json
import os
import struct
import sys
import tempfile

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge  # noqa: E402
from CADder.rig import defeature, native_import, swmesh  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "native_unit_scale_smoke")
SIZE = 0.05          # every part is a triangle this many meters across
PIVOT = 0.15         # the arm turns about this x, in meters


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _flat(rows):
    return [v for row in rows for v in row]


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


# component id, path, persistent id, x, group index, local x inside the
# component. The lifter is a rigid subassembly: one component, two parts.
def parts(arm_x):
    return [
        ("c001", "base-1", "pbase", 0.0, 0, None),
        ("c002", "arm-1", "parm", arm_x, 1, None),
        ("c003", "lifter-1/rod-1", "plift", 0.6, 2, 0.1),
        ("c003", "lifter-1/block-1", "plift", 0.5, 2, 0.0),
    ]


def manifest(rows):
    comps = {}
    for cid, path, pid, x, g, local in rows:
        if local is None:
            comps[cid] = {"id": cid, "sw_path": path,
                          "step_name": path.rpartition("/")[2],
                          "step_occurrence_path": None,
                          "sw_persistent_id": pid, "transform": _t(x)}
        else:
            top = path.partition("/")[0]
            comps[cid] = {"id": cid, "sw_path": top, "step_name": top,
                          "step_occurrence_path": None,
                          "sw_persistent_id": pid, "transform": _t(x - local),
                          "subassembly_solving": "rigid"}
    groups = {}
    for cid, _path, _pid, _x, g, _local in rows:
        if cid not in groups.setdefault(g, []):
            groups[g].append(cid)
    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "units.step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": list(comps.values()),
        "rigid_groups": [
            {"id": "g%03d" % g, "name": "g%03d" % g, "components": cids,
             "grounded": g == 0, "frame": None, "bbox_diag": 0.2}
            for g, cids in sorted(groups.items())],
        "joints": [
            {"id": "j001", "type": "revolute", "parent_group": "g000",
             "child_group": "g001", "origin": [PIVOT, 0, 0],
             "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None},
            {"id": "j002", "type": "revolute", "parent_group": "g000",
             "child_group": "g002", "origin": [0.5, 0, 0],
             "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None},
        ],
        "loops": [], "warnings": [],
    }


def write_mesh(path, rows):
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, 1, len(rows), 1)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    body += struct.pack("<i", 1) + _text("blob")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, SIZE, 0, 0, 0, SIZE, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 0)
    for cid, where, _pid, x, _g, local in rows:
        body += (struct.pack("<i", 1) + _text(cid)
                 + _text(where.rpartition("/")[2]) + _text(where)
                 + struct.pack("<16d", *_flat(_t(x))))
        body += struct.pack("<B", 0 if local is None else 1)
        if local is not None:
            body += struct.pack("<16d", *_flat(_t(local)))
    body += (_text("lifter-1") + _text("lifter") + _text("c003")
             + struct.pack("<16d", *_flat(_t(0.5))))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def export(arm_x):
    os.makedirs(TMP, exist_ok=True)
    rows = parts(arm_x)
    man = os.path.join(TMP, "units.rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(manifest(rows), fh)
    return write_mesh(os.path.join(TMP, "units.swmesh"), rows), man


def send(mesh, man, hierarchy, update=False):
    payload = {
        "step": None, "mesh": mesh, "manifest": man,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": hierarchy, "up_as": "ZPOS"},
    }
    if update:
        payload["rig_mode"] = "KEEP"
    return bridge._run_job(payload)


def fail(where, msg):
    raise SystemExit("native_unit_scale_smoke: FAIL: %s: %s" % (where, msg))


def the_parts():
    return {o.get("SWMESH_path"): o for o in bpy.data.objects
            if o.get("RIG_component_id") and not o.get("SWMESH_prototype")}


def rig_object():
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and obj.get("RIG_rig"):
            return obj
    return None


def world_span(obj):
    """How wide the part is in the world, from its geometry. A collection
    instance draws the meshes of its prototypes."""
    meshes = [obj.data] if obj.type == "MESH" else [
        o.data for o in obj.instance_collection.all_objects if o.type == "MESH"]
    xs = [(obj.matrix_world @ v.co).x for me in meshes for v in me.vertices]
    return max(xs) - min(xs)


def check_scene(where, bu, arm_x):
    """Every part on its CAD pose, at its CAD size, in Blender units."""
    found = the_parts()
    for _cid, path, _pid, x, _g, _local in parts(arm_x):
        obj = found.get(path)
        if obj is None:
            fail(where, "%s is not in the scene" % path)
        at = obj.matrix_world.translation
        if abs(at.x - x * bu) > 1e-3 * bu or abs(at.y) > 1e-6 * bu:
            fail(where, "%s is at x=%.4f, the CAD puts it at %.4f"
                 % (path, at.x, x * bu))
        scale = obj.matrix_world.to_scale()
        if max(abs(s - 1.0) for s in scale) > 1e-6:
            fail(where, "%s is scaled by %s" % (path, tuple(scale)))
        span = world_span(obj)
        if abs(span - SIZE * bu) > 1e-4 * bu:
            fail(where, "%s is %.5f wide, the CAD makes it %.5f"
                 % (path, span, SIZE * bu))
    arm_obj = rig_object()
    if arm_obj is None:
        fail(where, "no rig was built")
    pb = next((b for b in arm_obj.pose.bones if b.get("RIG_group") == "g001"),
              None)
    if pb is None:
        fail(where, "no bone for the arm")
    head = arm_obj.matrix_world @ pb.bone.head_local
    if abs(head.x - PIVOT * bu) > 1e-4 * bu:
        fail(where, "the arm bone is at x=%.4f, its pivot is at %.4f"
             % (head.x, PIVOT * bu))

    # The bone turns the arm about the pivot, so the arm and its bone
    # have to meet: turned, the arm stays as far from the pivot as it was.
    rest = found["arm-1"].matrix_world.translation.copy()
    pb.rotation_mode = "XYZ"
    pb.rotation_euler = (0.0, 0.7, 0.0)
    bpy.context.view_layer.update()
    turned = found["arm-1"].matrix_world.translation.copy()
    pb.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    pivot = Vector((PIVOT * bu, 0.0, 0.0))
    if abs((turned - pivot).length - (rest - pivot).length) > 1e-4 * bu:
        fail(where, "the arm does not turn about its bone: %.4f from the "
             "pivot at rest, %.4f turned" % ((rest - pivot).length,
                                             (turned - pivot).length))
    if (turned - rest).length < 1e-3 * bu:
        fail(where, "turning the arm bone did not move the arm")


def run(hierarchy, scale_length):
    where = "%s at Unit Scale %g" % (hierarchy, scale_length)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    bpy.context.scene.unit_settings.scale_length = scale_length
    bu = 1.0 / scale_length

    mesh, man = export(0.2)
    result = send(mesh, man, hierarchy)
    if not result.get("ok"):
        fail(where, "the send failed: %s" % result.get("error"))
    moved = (result.get("stages", {}).get("poses") or {}).get("moved") or []
    if moved:
        fail(where, "pose sync had to move parts the send had just placed: "
             "%s" % moved)
    check_scene(where + ", send", bu, 0.2)

    # The arm moved in CAD, then Refresh Model.
    mesh, man = export(0.25)
    result = send(mesh, man, hierarchy, update=True)
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    check_scene(where + ", refresh", bu, 0.25)
    reshaped = (result.get("stages", {}).get("update") or {}).get("reshaped") or []
    if reshaped:
        fail(where, "the refresh replaced the meshes of unchanged parts: %s"
             % reshaped)

    # Rebuild from CAD asks for the geometry again and puts it on the
    # parts that are there, the same size.
    changed = native_import.refine(bpy.context, mesh)
    if not changed:
        fail(where, "the rebuild changed nothing")
    check_scene(where + ", rebuild", bu, 0.25)


def run_scale_change(hierarchy, first, then):
    """The Unit Scale changes between the send and a refresh. Blender does
    not scale anything when it changes, so the meshes stay the size they
    were built at. The refresh placed the parts at the new scale and kept
    those meshes: every part stood in the right place, a thousand times
    too small. A send of an older version, which built every mesh in
    meters, gave the same on its first refresh in a millimeter scene."""
    where = "%s from Unit Scale %g to %g" % (hierarchy, first, then)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    bpy.context.scene.unit_settings.scale_length = first
    mesh, man = export(0.2)
    result = send(mesh, man, hierarchy)
    if not result.get("ok"):
        fail(where, "the send failed: %s" % result.get("error"))
    bpy.context.scene.unit_settings.scale_length = then
    mesh, man = export(0.25)
    result = send(mesh, man, hierarchy, update=True)
    if not result.get("ok"):
        fail(where, "the refresh failed: %s" % result.get("error"))
    check_scene(where + ", refresh", 1.0 / then, 0.25)

    # A locked rig is the user's: the refresh says so and does not build
    # it again. Its bones stay at the scale they had.
    from CADder.rig import rig_build
    arm_obj = rig_object()
    arm_obj[rig_build.LOCK_TAG] = True
    heads = [b.head_local.copy() for b in arm_obj.data.bones]
    bpy.context.scene.unit_settings.scale_length = first
    mesh, man = export(0.25)
    result = send(mesh, man, hierarchy, update=True)
    if not result.get("ok"):
        fail(where, "the refresh of a locked rig failed: %s" % result.get("error"))
    said = json.dumps(result.get("stages", {}).get("rig") or {})
    if "is locked" not in said:
        fail(where, "the refresh did not say the locked rig stays: %s" % said)
    if [b.head_local for b in arm_obj.data.bones] != heads:
        fail(where, "the refresh built the locked rig again")


def check_lengths_to_cad():
    """A length the user types in the scene goes to the CAD application in
    meters."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    scene = bpy.context.scene
    scene.unit_settings.scale_length = 0.001
    stepper = scene.stepper
    stepper.quality_preset = "CUSTOM"
    stepper.lin_deflection_len = 0.8          # 0.8 mm in this scene
    asked = native_import.cad_quality(scene)
    if abs(asked["chord_m"] - 0.0008) > 1e-9:
        fail("lengths", "a 0.8 mm distance goes to the CAD as %g m"
             % asked["chord_m"])
    stepper.quality_preset = "FINE"           # a preset is meters already
    asked = native_import.cad_quality(scene)
    if abs(asked["chord_m"] - 0.0002) > 1e-9:
        fail("lengths", "the Fine preset goes to the CAD as %g m"
             % asked["chord_m"])
    scene.unit_settings.scale_length = 1.0
    stepper.quality_preset = "CUSTOM"
    stepper.lin_deflection_len = 0.0008
    asked = native_import.cad_quality(scene)
    if abs(asked["chord_m"] - 0.0008) > 1e-9:
        fail("lengths", "at Unit Scale 1, 0.8 mm goes as %g m"
             % asked["chord_m"])

    scene.unit_settings.scale_length = 0.001
    me = bpy.data.meshes.new("plate")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    plate = bpy.data.objects.new("plate", me)
    plate["RIG_component_id"] = "c001"
    scene.collection.objects.link(plate)
    plate.cad_defeature.enabled = True
    plate.cad_defeature.size = 0.5            # 0.5 mm in this scene
    rows = defeature.orders([plate], scene)
    if not rows or abs(rows[0]["size_m"] - 0.0005) > 1e-9:
        fail("lengths", "a 0.5 mm feature size goes to the CAD as %s" % rows)


def main():
    done = []
    for hierarchy in ("FLAT", "EMPTIES", "COLLECTION_INSTANCES"):
        for scale_length in (1.0, 0.001):
            run(hierarchy, scale_length)
            done.append("%s/%g" % (hierarchy, scale_length))
        run_scale_change(hierarchy, 1.0, 0.001)
        run_scale_change(hierarchy, 0.001, 1.0)
        done.append("%s/1 to 0.001 and back" % hierarchy)
    check_lengths_to_cad()
    print("native_unit_scale_smoke: OK: a send, a refresh and a rebuild put "
          "the parts and the rig at the same scale (%s), and scene lengths "
          "go to the CAD application in meters" % ", ".join(done))


main()
