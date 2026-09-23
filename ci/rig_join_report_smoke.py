# SPDX-License-Identifier: GPL-3.0-or-later
"""What a join of two rigs reports, and when it does not join at all.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_join_report_smoke.py

rig_join_smoke checks that a join keeps everything in its place. This one
checks what the join SAYS about it:

  * a clean join of rigs with limit dials reports no missing bone and no
    drift, for rigs built now and for rigs built before every bone was
    tagged with its manifest.
  * a join onto a posed bone merges nothing, so the user can clear the
    pose and join again, as the message tells them to.
  * in a scene where one unit is a millimeter, a clean join reports no
    drift, and the figures it gives are millimeters.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy  # noqa: E402
from mathutils import Euler, Matrix  # noqa: E402

bpy.ops.preferences.addon_enable(module="CADder")
from CADder.rig import (graph, joining, manifest as mm,  # noqa: E402
                        parenting, rig_build)

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("   FAIL:", msg)
    return cond


def ident4():
    return [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def manifest(step_file, at):
    """A ground and two hinges about Z. The first hinge has a limit, so it
    gets a dial: a bone with no group of its own. Both rigs use the same
    ids and names, so the join renames the incoming bones."""
    def comp(cid, name, x):
        t = ident4()
        t[0][3] = at + x
        return {"id": cid, "sw_path": name + "-1", "step_name": name,
                "step_occurrence_path": None, "transform": t}

    def hinge(jid, child, x, limits=None):
        return {"id": jid, "type": "revolute", "parent_group": "g000",
                "child_group": child, "origin": [at + x, 0, 0],
                "axis": [0, 0, 1], "secondary_axis": [1, 0, 0],
                "limits": limits}

    return {
        "manifest_version": "1.0.0",
        "generator": {"name": "rig_join_report_smoke", "version": "1"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": step_file, "ap": "AP214", "sha1": None,
                        "occurrence_matching": None},
        "components": [comp("c001", "base", 0.0), comp("c002", "link", 0.1),
                       comp("c003", "lever", 0.2)],
        "rigid_groups": [
            {"id": "g000", "name": "base", "components": ["c001"],
             "grounded": True, "frame": None, "bbox_diag": 0.2},
            {"id": "g001", "name": "link", "components": ["c002"],
             "grounded": False, "frame": None, "bbox_diag": 0.1},
            {"id": "g002", "name": "lever", "components": ["c003"],
             "grounded": False, "frame": None, "bbox_diag": 0.1},
        ],
        "joints": [
            hinge("j001", "g001", 0.1,
                  {"rotation": {"min": -1.0, "max": 1.0,
                                "value_at_rest": 0.0},
                   "translation": None}),
            hinge("j002", "g002", 0.2),
        ],
        "loops": [], "warnings": [],
    }


def build(name, at):
    source = name + ".rig.json"
    man = mm.parse(manifest(name + ".step", at), source_path=source)
    result = rig_build.build(bpy.context, man, graph.build(man))
    arm = result.armature_object
    arm.name = name + "_Rig"
    unit = rig_build._unit_scale(bpy.context)
    for gid, bone_name in result.bone_names.items():
        mesh = bpy.data.meshes.new("%s_%s" % (name, gid))
        mesh.from_pydata([(0, 0, 0), (0.02, 0, 0), (0, 0.02, 0)], [],
                         [(0, 1, 2)])
        obj = bpy.data.objects.new("%s_%s_part" % (name, gid), mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj["RIG_group"] = gid
        obj["RIG_source"] = source
        obj.matrix_world = (arm.matrix_world @ arm.pose.bones[bone_name].matrix
                            @ Matrix.Translation((0.03 * unit, 0.0, 0.0)))
    parenting.relink(bpy.context, arm)
    return result


def untag_helpers(arm):
    """What a rig built before every bone carried its source looks like:
    only the group bones say which manifest they came from."""
    for pb in arm.pose.bones:
        if not pb.get("RIG_group") and "RIG_source" in pb.keys():
            del pb["RIG_source"]


def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")


# ── A clean join says it is clean ───────────────────────────────────────

def clean_join_is_quiet(legacy):
    print("-- a clean join of rigs with dials%s"
          % (", built before the tags" if legacy else ""))
    fresh()
    machine = build("machine", 0.0)
    gripper = build("gripper", 1.0)
    host, sub = machine.armature_object, gripper.armature_object
    if legacy:
        untag_helpers(host)
        untag_helpers(sub)
    check(any(name.startswith("LIM_") for name in sub.data.bones.keys()),
          "the gripper has no dial bone to test with")
    sub.location = (0.35, 0.12, 0.08)
    sub.rotation_euler = Euler((0.0, 0.0, 0.6), "XYZ")
    report = joining.join(bpy.context, host, [sub],
                          attach_bone=machine.bone_names["g001"])
    check(report.renamed, "the names were supposed to collide")
    check(not report.warnings, "a clean join warns: %s" % report.warnings)
    check(not report.drift, "a clean join reports drift: %s" % report.drift)


# ── A posed attach bone stops the join before anything is merged ────────

def posed_attach_joins_nothing():
    print("-- a join onto a posed bone")
    fresh()
    machine = build("machine", 0.0)
    gripper = build("gripper", 1.0)
    host, sub = machine.armature_object, gripper.armature_object
    attach = machine.bone_names["g001"]
    posed = host.pose.bones[attach]
    posed.rotation_mode = "XYZ"
    posed.rotation_euler[1] = 0.4
    bpy.context.view_layer.update()
    count = len(host.data.bones)
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    sub.select_set(True)
    host.select_set(True)
    bpy.context.view_layer.objects.active = host

    report = joining.join(bpy.context, host, [sub], attach_bone=attach)
    check(any("rest pose" in w for w in report.warnings),
          "the posed attach bone was not reported: %s" % report.warnings)
    check("gripper_Rig" in bpy.data.objects,
          "the gripper's rig was merged although nothing was attached")
    check(len(host.data.bones) == count,
          "the host has %d bone(s), had %d" % (len(host.data.bones), count))
    check(not report.joined and not report.bones_added,
          "the report says %s joined, %d bone(s) added"
          % (report.joined, report.bones_added))
    _host, others = joining.joinable(bpy.context)
    check([o.name for o in others] == ["gripper_Rig"],
          "the rigs cannot be joined again: %s" % [o.name for o in others])

    # As the message says: clear the pose and join again.
    posed.rotation_euler[1] = 0.0
    bpy.context.view_layer.update()
    again = joining.join(bpy.context, host, [sub], attach_bone=attach)
    check(again.attached_to == attach and not again.warnings,
          "the join after the pose was cleared: attached to %r, %s"
          % (again.attached_to, again.warnings))


# ── A millimeter scene reports millimeters ──────────────────────────────

def millimetre_scene_reports_millimetres():
    print("-- a join in a scene with 1 unit = 1 mm")
    fresh()
    bpy.context.scene.unit_settings.scale_length = 0.001
    # Two meters out, so the bones stand some thousands of units from the
    # origin, where single precision is coarser than a micrometre.
    machine = build("machine", 2.0)
    gripper = build("gripper", 3.0)
    host, sub = machine.armature_object, gripper.armature_object
    sub.location = (350.0, 120.0, 80.0)
    report = joining.join(bpy.context, host, [sub],
                          attach_bone=machine.bone_names["g001"])
    check(not report.drift, "a clean join reports drift: %s" % report.drift)
    check(not report.warnings, "a clean join warns: %s" % report.warnings)

    # Half a millimeter off rest is half a millimeter, not 500.
    fresh()
    bpy.context.scene.unit_settings.scale_length = 0.001
    machine = build("machine", 2.0)
    gripper = build("gripper", 3.0)
    host, sub = machine.armature_object, gripper.armature_object
    attach = machine.bone_names["g001"]
    host.pose.bones[attach].location = (0.0, 0.5, 0.0)
    bpy.context.view_layer.update()
    report = joining.join(bpy.context, host, [sub], attach_bone=attach)
    check(any(w.startswith("%s is 0.5 mm " % attach) for w in report.warnings),
          "half a millimeter off rest reads: %s" % report.warnings)


def main():
    clean_join_is_quiet(legacy=False)
    clean_join_is_quiet(legacy=True)
    posed_attach_joins_nothing()
    millimetre_scene_reports_millimetres()

    print()
    if FAILS:
        print("rig_join_report_smoke: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("rig_join_report_smoke: OK")


main()
