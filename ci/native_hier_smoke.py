# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the direct link's import options: the four hierarchy
modes the STEP importer offers, the up axis, one collection per file, and
material slots per part.

    blender -b --factory-startup -P ci/native_hier_smoke.py

A synthetic .swmesh with two definitions (a base and a pin, the pin used
twice) and a manifest whose paths put both pins under a subassembly.
"""

import json
import os
import struct
import sys
import tempfile

import bpy
from mathutils import Vector

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from STEPper_NEXT.rig import appearance, manifest as man_mod, native_import, swmesh  # noqa: E402
from STEPper_NEXT import rig  # noqa: E402
from STEPper_NEXT.rig import ui as rig_ui  # noqa: E402


def _t(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.SwToBlender", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "hier.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "base-1", "step_name": "base",
         "step_occurrence_path": None, "transform": _t(0, 0, 0)},
        {"id": "c002", "sw_path": "sub-1", "step_name": "sub",
         "step_occurrence_path": None, "transform": _t(0.5, 0, 0),
         "subassembly_solving": "flexible"},
        {"id": "c003", "sw_path": "sub-1/pin-1", "step_name": "pin",
         "step_occurrence_path": None, "transform": _t(0.5, 0, 0)},
        {"id": "c004", "sw_path": "sub-1/pin-2", "step_name": "pin",
         "step_occurrence_path": None, "transform": _t(0.7, 0, 0)},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.1},
        {"id": "g001", "name": "pin", "components": ["c003"], "grounded": False,
         "frame": None, "bbox_diag": 0.05},
        {"id": "g002", "name": "pin2", "components": ["c004"], "grounded": False,
         "frame": None, "bbox_diag": 0.05},
    ],
    # One hinge about the CAD Z axis, so a Y-up import must turn its bone
    # to Blender -Y along with the geometry.
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [0.5, 0.0, 0.0],
         "axis": [0.0, 0.0, 1.0], "secondary_axis": [1.0, 0.0, 0.0],
         "limits": None, "sw_mates": [],
         "coupling": None},
    ],
    "loops": [],
    "warnings": [],
}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path):
    body = struct.pack("<III", swmesh.MAGIC, 1, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<III", 3, 2, 3)            # materials, definitions, instances
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0) + _text("")
    body += _text("red") + struct.pack("<6f", 0.9, 0.1, 0.1, 1.0, 0.5, 0.0) + _text("")
    body += _text("blue") + struct.pack("<6f", 0.1, 0.1, 0.9, 1.0, 0.5, 0.0) + _text("")
    # definition 1: the base, one triangle, red
    body += struct.pack("<i", 1) + _text("base")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.1, 0, 0, 0, 0.1, 0)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 1)
    # definition 2: the pin, one triangle, blue
    body += struct.pack("<i", 2) + _text("pin")
    body += struct.pack("<II", 3, 1)
    body += struct.pack("<9f", 0, 0, 0, 0.05, 0, 0, 0, 0, 0.05)
    body += struct.pack("<3i", 0, 1, 2)
    body += struct.pack("<i", 2)
    for did, cid, name, x in ((1, "c001", "base-1", 0.0), (2, "c003", "pin-1", 0.5),
                              (2, "c004", "pin-2", 0.7)):
        rows = [v for row in _t(x, 0, 0) for v in row]
        body += struct.pack("<i", did) + _text(cid) + _text(name) + struct.pack("<16d", *rows)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def _check(cond, msg):
    if not cond:
        raise SystemExit("native_hier_smoke: FAIL: " + msg)


def _collections_named(prefix):
    return [c.name for c in bpy.data.collections if c.name.startswith(prefix)]


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    path = write_mesh(os.path.join(tempfile.gettempdir(), "hier.swmesh"))
    m = man_mod.parse(MANIFEST)

    # FLAT: one collection per part name under hier, materials per part
    objs, report = native_import.build(bpy.context, path, manifest=m, hierarchy="FLAT")
    _check(len(objs) == 3, "FLAT built %d objects" % len(objs))
    _check(bpy.data.collections.get("hier") is not None, "no hier collection")
    groups = sorted(c.name for c in bpy.data.collections["hier"].children)
    _check(groups == ["base", "pin"], "FLAT groups %s" % groups)
    base = bpy.data.objects["base-1"]
    _check([mt.name for mt in base.data.materials] == ["SW red"],
           "the base carries %s, not only its own material" % [mt.name for mt in base.data.materials])
    pin = bpy.data.objects["pin-1"]
    _check([mt.name for mt in pin.data.materials] == ["SW blue"], "the pin's slots are wrong")
    _check(pin.data is bpy.data.objects["pin-2"].data, "the two pins do not share one mesh")
    # Linear: the add-in's colours are display sRGB and Blender's are not.
    want = tuple(round(c, 3) for c in appearance.linear((0.9, 0.1, 0.1)))
    _check(tuple(round(v, 3) for v in base.color[:3]) == want,
           "object colour not set: %s, want %s" % (tuple(base.color[:3]), want))

    # TREE: nested collections, the pins under sub-1
    objs, _ = native_import.build(bpy.context, path, manifest=m, hierarchy="TREE")
    # The second import replaced the first: one collection of that name,
    # not a numbered copy beside it.
    named = [c.name for c in bpy.data.collections if c.name.startswith("hier")]
    _check(named.count("hier") == 1 and "hier.001" not in named,
           "the FLAT import was not replaced: %s" % named)
    sub = bpy.data.collections.get("sub-1")
    _check(sub is not None and sub.name in [c.name for c in bpy.data.collections["hier"].children],
           "no sub-1 collection under hier")
    _check(sorted(o.name for o in sub.objects) == ["pin-1", "pin-2"], "the pins are not in sub-1")
    _check("base-1" in [o.name for o in bpy.data.collections["hier"].objects], "the base is not at the root")

    # EMPTIES: the pins parented under an empty at the subassembly's pose
    objs, _ = native_import.build(bpy.context, path, manifest=m, hierarchy="EMPTIES")
    emp = bpy.data.objects.get("sub-1")
    _check(emp is not None and emp.type == "EMPTY", "no sub-1 empty")
    _check(round(emp.matrix_world.translation.x, 3) == 0.5, "the empty is not at the subassembly's pose")
    _check(emp.get("RIG_component_id") == "c002", "the empty does not carry its component id")
    for name in ("pin-1", "pin-2"):
        o = bpy.data.objects[name]
        _check(o.parent is emp, "%s is not parented under sub-1" % name)
    _check(round(bpy.data.objects["pin-2"].matrix_world.translation.x, 3) == 0.7,
           "parenting moved pin-2")

    # COLLECTION_INSTANCES: prototypes hidden, occurrences instancing them
    objs, _ = native_import.build(bpy.context, path, manifest=m, hierarchy="COLLECTION_INSTANCES")
    comps = bpy.data.collections.get("hier.components")
    _check(comps is not None, "no hier.components collection")
    protos = sorted(c.name for c in comps.children)
    _check(protos == ["base", "pin"], "prototype collections %s" % protos)
    for name in ("pin-1", "pin-2"):
        o = bpy.data.objects[name]
        _check(o.instance_type == "COLLECTION" and o.instance_collection is bpy.data.collections["pin"],
               "%s does not instance the pin prototype" % name)
        _check(o.get("RIG_component_id") in ("c003", "c004"), "%s lost its component id" % name)
    lc = bpy.context.view_layer.layer_collection.children["hier"] \
        if "hier" in bpy.context.view_layer.layer_collection.children else None
    # The prototypes live inside the assembly's own collection, hidden
    # from the view layer.
    def excluded_names(layer_col):
        out = [layer_col.name] if layer_col.exclude else []
        for child in layer_col.children:
            out.extend(excluded_names(child))
        return out

    excluded = excluded_names(bpy.context.view_layer.layer_collection)
    _check("hier.components" in excluded, "the prototype collection is not hidden")
    _check("hier.components" in [c.name for c in bpy.data.collections["hier"].children],
           "the prototype collection is not inside the file's collection")

    # Up axis. The import always lands in one collection named after the
    # file, whatever the hierarchy inside it is.
    objs, report = native_import.build(bpy.context, path, manifest=m, hierarchy="FLAT",
                                       up_as="YPOS")
    top = bpy.data.collections.get("hier")
    _check(top is not None, "the import is not in a collection named after the file")
    pin2 = bpy.data.objects["pin-2"]
    _check([round(v, 3) for v in pin2.matrix_world.translation] == [0.7, 0.0, 0.0],
           "Y up moved a point on the X axis")
    _check(report.frame_rows[2][1] == 1.0, "the report's frame does not say Y became Z")
    v = pin2.matrix_world @ pin2.instance_collection.objects[0].data.vertices[2].co \
        if pin2.instance_collection else pin2.matrix_world @ pin2.data.vertices[2].co
    _check(round(v.y, 3) == -0.05, "the pin's Z point did not turn to -Y: %s" % [round(c, 3) for c in v])

    # The rig follows the turned geometry THROUGH THE OPERATOR, whose gate
    # trusts a frame only when the import anchored it: the hinge about CAD
    # Z rests along Blender -Y, on the turned pin.
    rig_ui._STATE["manifest"] = m
    rig_ui._STATE["match_report"] = report
    _check("FINISHED" in bpy.ops.cadlink.build_rig(), "the rig did not build")
    arm = rig_ui._STATE["build"].armature_object
    bone = arm.pose.bones[rig_ui._STATE["build"].bone_names["g001"]]
    axis = (arm.matrix_world @ bone.matrix).to_3x3() @ Vector((0.0, 1.0, 0.0))
    head = (arm.matrix_world @ bone.matrix).translation
    _check([round(c, 3) for c in axis] == [0.0, -1.0, 0.0],
           "the hinge bone does not rest along -Y after the Y-up turn: %s" % [round(c, 3) for c in axis])
    _check([round(c, 3) for c in head] == [0.5, 0.0, 0.0],
           "the hinge bone is not on its joint: %s" % [round(c, 3) for c in head])
    bpy.data.objects.remove(arm, do_unlink=True)
    bpy.data.collections.remove(bpy.data.collections["hier_Rig"])

    # And back to nothing: every collection and object of the import goes
    native_import.remove_previous()
    _check(not _collections_named("hier"), "collections left behind: %s" % _collections_named("hier"))
    _check(bpy.data.objects.get("pin-1") is None, "objects left behind")

    print("native_hier_smoke: OK: FLAT, TREE, EMPTIES and COLLECTION_INSTANCES "
          "with STEP-import names, material slots per part, object colours, "
          "Y up with the rig turned along, one collection per file, and a "
          "clean removal")


main()
