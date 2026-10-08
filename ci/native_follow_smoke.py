# SPDX-License-Identifier: GPL-3.0-or-later
"""A refresh of a direct send puts new parts where the user moved the import.

    blender -b --factory-startup --python-exit-code 1 -P ci/native_follow_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

A user put the parts of a send under an empty and moved the empty. After a
new part in SolidWorks and Refresh Model, the new part stood at its place
in SolidWorks, away from the others and not under the empty. A part that
SolidWorks moved went back to its SolidWorks place in the same way.

The parts that did not change say where the import is now. This smoke
moves an import in each way a user can (an empty of their own, the parts
as one, the empty of the import, the rig) and checks after a refresh:

  * a new part stands with the others, under the same parent,
  * a part that the CAD application moved stands with the others,
  * a part that did not change is not touched,
  * one part that the user moved alone does not move the new part,
  * a second refresh with no change moves nothing.
"""

import json
import math
import os
import struct
import sys
import tempfile

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.ci import native_top_level_smoke as tl  # noqa: E402
from CADder.rig import swmesh  # noqa: E402
from CADder.rig import ui as rig_ui  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "native_follow_smoke")
DOC = os.path.join(tempfile.gettempdir(), "smoke parts", "bracket.SLDASM")
STEM = "bracket_Default"

# (component id, path, persistent id, x, mesh name, size)
BASE = ("c001", "base-1", "pbase", 0.0, "base", 0.05)
ARM = ("c002", "arm-1", "parm", 0.2, "arm", 0.10)
PIN = ("c003", "pin-1", "ppin", 0.4, "pin", 0.15)
KNOB = ("c004", "knob-1", "pknob", 0.6, "knob", 0.08)
FIRST = [BASE, ARM, PIN]
# The arm moved 50 mm in the CAD application, and the knob is new.
SECOND = [BASE, ARM[:3] + (0.25,) + ARM[4:], PIN, KNOB]
JOINTS = [("j001", "c001", "c002", 0.2), ("j002", "c002", "c003", 0.4)]
JOINTS_2 = JOINTS + [("j003", "c001", "c004", 0.6)]


def fail(where, msg):
    raise SystemExit("native_follow_smoke: FAIL: %s: %s" % (where, msg))


def check(cond, where, msg):
    if not cond:
        fail(where, msg)


def write_mesh(path, parts):
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, len(parts), len(parts), 0)
    body += tl._text("gray") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += tl._text("") + struct.pack("<I", 0)
    for i, (_cid, _where, _pid, _x, mesh, size) in enumerate(parts):
        body += struct.pack("<i", i + 1) + tl._text(mesh)
        body += struct.pack("<II", 3, 1)
        body += struct.pack("<9f", 0, 0, 0, size, 0, 0, 0, size, 0)
        body += struct.pack("<3i", 0, 1, 2)
        body += struct.pack("<i", 0)
    for i, (cid, where, _pid, x, *_rest) in enumerate(parts):
        rows = [v for row in tl._t(x) for v in row]
        body += (struct.pack("<i", i + 1) + tl._text(cid)
                 + tl._text(where) + tl._text(where)
                 + struct.pack("<16d", *rows) + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def write_manifest(path, parts, joints):
    """With joints: one body for each part, as the top-level smoke writes
    it. With none: one fixed body that holds every part, which is an
    assembly with nothing that moves."""
    if joints:
        return tl.write_manifest(path, STEM, [p[:5] for p in parts], joints)
    tl.write_manifest(path, STEM, [p[:5] for p in parts], [])
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data["rigid_groups"] = [{
        "id": "g000", "name": "bracket",
        "components": [p[0] for p in parts],
        "grounded": True, "frame": None, "bbox_diag": 0.5}]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


def send(where, parts, joints, mode="FLAT", update=False, with_rig=True,
         rig_mode=None):
    os.makedirs(TMP, exist_ok=True)
    payload = {
        "step": None,
        "mesh": write_mesh(os.path.join(TMP, STEM + ".swmesh"), parts),
        "manifest": write_manifest(os.path.join(TMP, STEM + ".rig.json"),
                                   parts, joints),
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": with_rig,
                  "relink": with_rig, "cleanup": True},
        "import_options": {"hierarchy_types": mode, "up_as": "ZPOS"},
        "source_document": DOC, "configuration": "Default",
    }
    if rig_mode:
        payload["rig_mode"] = rig_mode
    result = bridge._run_job(payload)
    check(result.get("ok"), where, "the send failed: %s" % result.get("error"))
    bpy.context.view_layer.update()
    return result


def fresh():
    try:
        rig.unregister()
    except Exception:                                   # noqa: BLE001
        pass
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    rig_ui._reset_state()


def parts():
    return {o.get("SWMESH_path"): o for o in bpy.data.objects
            if o.get("SWMESH_file") == STEM and o.get("RIG_component_id")
            and not o.get("SWMESH_prototype")}


def at(obj):
    return obj.matrix_world.translation.copy()


def near(a, b):
    return (Vector(a) - Vector(b)).length < 1e-4


def the_rig():
    found = [o for o in bpy.data.objects if o.type == "ARMATURE"
             and o.get("RIG_rig") and o.get("RIG_import") == STEM]
    return found[0] if found else None


def expect(where, move, parent=None, moved_alone=None):
    """After the refresh: each part at `move` of its CAD place."""
    got = parts()
    check(set(got) == {"base-1", "arm-1", "pin-1", "knob-1"}, where,
          "the parts are %s" % sorted(got))
    for path, x in (("base-1", 0.0), ("arm-1", 0.25), ("pin-1", 0.4),
                    ("knob-1", 0.6)):
        if path == moved_alone:
            continue
        want = move @ Vector((x, 0.0, 0.0))
        check(near(at(got[path]), want), where,
              "%s is at %s, and the import is at %s"
              % (path, tuple(round(v, 4) for v in at(got[path])),
                 tuple(round(v, 4) for v in want)))
    if parent is not None:
        check(got["knob-1"].parent is parent, where,
              "the new part is under %s, not under the empty of the user"
              % (got["knob-1"].parent and got["knob-1"].parent.name))


def again(where, joints, mode="FLAT", with_rig=True, rig_mode=None):
    """A refresh with no change moves nothing."""
    before = {p: at(o) for p, o in parts().items()}
    send(where, SECOND, joints, mode, update=True, with_rig=with_rig,
         rig_mode=rig_mode)
    for path, obj in parts().items():
        check(near(at(obj), before[path]), where,
              "a refresh with no change moved %s" % path)


MOVE = Matrix.Translation((1.0, 2.0, 0.0)) @ Matrix.Rotation(
    math.radians(90.0), 4, "Z")


def empty_of_the_user():
    null = bpy.data.objects.new("Null", None)
    bpy.context.scene.collection.objects.link(null)
    return null


def put_under(null, objects):
    """As Ctrl+P does: the object keeps its place."""
    bpy.context.view_layer.update()
    for obj in objects:
        world = obj.matrix_world.copy()
        obj.parent = null
        obj.parent_type = "OBJECT"
        obj.matrix_parent_inverse = null.matrix_world.inverted()
        obj.matrix_world = world
    bpy.context.view_layer.update()
    null.matrix_world = MOVE
    bpy.context.view_layer.update()


def tops_of_the_import():
    """What the outliner shows at the top of the import: the rig, a part
    that is on no bone, and an empty of the import with no parent."""
    found = [o for o in bpy.data.objects if o.parent is None and (
        o is the_rig() or o.get("SWMESH_file") == STEM)]
    return [o for o in found if not o.get("SWMESH_prototype")]


AGAIN = Matrix.Translation((0.0, 0.0, 5.0))


def carried(where, null):
    """The empty of the user still carries the import: move it again."""
    before = {p: at(o) for p, o in parts().items()}
    null.matrix_world = AGAIN @ null.matrix_world
    bpy.context.view_layer.update()
    for path, obj in parts().items():
        check(near(at(obj), AGAIN @ before[path]), where,
              "after the refresh, %s does not follow the empty of the user"
              % path)
    null.matrix_world = AGAIN.inverted() @ null.matrix_world
    bpy.context.view_layer.update()


def on_the_rig(where):
    arm = the_rig()
    check(arm is not None, where, "the refresh left no rig")
    for path, obj in parts().items():
        check(obj.parent is arm and obj.parent_type == "BONE", where,
              "%s is not on a bone of the rig" % path)
    # A bone stands at its part: the pin turns about its joint.
    bone = arm.pose.bones.get("pin")
    if bone is not None:
        head = arm.matrix_world @ bone.head
        check(near(head, at(parts()["pin-1"])), where,
              "the bone of the pin is at %s, and the pin is at %s"
              % (tuple(round(v, 4) for v in head),
                 tuple(round(v, 4) for v in at(parts()["pin-1"]))))


def the_rig_under_an_empty(mode, joints, joints_2, rig_mode):
    where = "the rig under an empty of the user, %s, %d joint(s), %s" % (
        mode, len(joints), rig_mode)
    fresh()
    send(where, FIRST, joints, mode)
    null = empty_of_the_user()
    put_under(null, tops_of_the_import())
    check(near(at(parts()["pin-1"]), MOVE @ Vector((0.4, 0.0, 0.0))), where,
          "the parts did not follow the empty")
    send(where, SECOND, joints_2, mode, update=True, rig_mode=rig_mode)
    expect(where, MOVE)
    check(the_rig().parent is null, where,
          "the rig is no longer under the empty of the user")
    if rig_mode != "KEEP":
        on_the_rig(where)
    carried(where, null)
    again(where, joints_2, mode, rig_mode=rig_mode)


def the_parts_under_an_empty(rig_mode):
    """What the user did: the parts, and not the rig, under an empty."""
    where = "the parts under an empty of the user, %s" % rig_mode
    fresh()
    send(where, FIRST, JOINTS)
    null = empty_of_the_user()
    put_under(null, list(parts().values()))
    send(where, SECOND, JOINTS_2, update=True, rig_mode=rig_mode)
    expect(where, MOVE)
    if rig_mode != "KEEP":
        on_the_rig(where)
    carried(where, null)
    again(where, JOINTS_2, rig_mode=rig_mode)


def all_under_an_empty(rig_mode):
    where = "all the objects under an empty of the user, %s" % rig_mode
    fresh()
    send(where, FIRST, JOINTS)
    null = empty_of_the_user()
    put_under(null, list(parts().values()) + [the_rig()])
    send(where, SECOND, JOINTS_2, update=True, rig_mode=rig_mode)
    expect(where, MOVE)
    carried(where, null)
    again(where, JOINTS_2, rig_mode=rig_mode)


def no_rig(mode):
    where = "no rig, the parts under an empty of the user, %s" % mode
    fresh()
    send(where, FIRST, [], mode, with_rig=False)
    check(the_rig() is None, where, "the send made a rig")
    null = empty_of_the_user()
    tops = tops_of_the_import()
    free = [o for o in tops if o.get("RIG_component_id")]
    put_under(null, tops)
    send(where, SECOND, [], mode, update=True, with_rig=False)
    expect(where, MOVE, parent=null if free else None)
    carried(where, null)
    again(where, [], mode, with_rig=False)


def moved_as_one():
    where = "the parts moved as one"
    fresh()
    send(where, FIRST, [], with_rig=False)
    for obj in parts().values():
        obj.matrix_world = MOVE @ obj.matrix_world
    bpy.context.view_layer.update()
    send(where, SECOND, [], update=True, with_rig=False)
    expect(where, MOVE)
    again(where, [], with_rig=False)


def one_part_alone():
    where = "one part moved alone"
    fresh()
    send(where, FIRST, [], with_rig=False)
    pin = parts()["pin-1"]
    pin.matrix_world = Matrix.Translation((0.0, 3.0, 0.0)) @ pin.matrix_world
    bpy.context.view_layer.update()
    send(where, SECOND, [], update=True, with_rig=False)
    expect(where, Matrix.Identity(4), moved_alone="pin-1")
    check(near(at(parts()["pin-1"]), (0.4, 3.0, 0.0)), where,
          "the part that the user moved did not stay where it was put")


def the_rig_moved(rig_mode):
    where = "the rig moved, %s" % rig_mode
    fresh()
    send(where, FIRST, JOINTS)
    arm = the_rig()
    check(arm is not None, where, "the send made no rig")
    arm.matrix_world = MOVE @ arm.matrix_world
    bpy.context.view_layer.update()
    check(near(at(parts()["pin-1"]), MOVE @ Vector((0.4, 0.0, 0.0))), where,
          "the parts did not follow the rig")
    send(where, SECOND, JOINTS_2, update=True, rig_mode=rig_mode)
    expect(where, MOVE)
    if rig_mode != "KEEP":
        on_the_rig(where)
    again(where, JOINTS_2, rig_mode=rig_mode)


def not_moved(rig_mode):
    """An import that nobody moved stays where the send put it."""
    where = "not moved, %s" % rig_mode
    fresh()
    send(where, FIRST, JOINTS)
    send(where, SECOND, JOINTS_2, update=True, rig_mode=rig_mode)
    expect(where, Matrix.Identity(4))
    check(the_rig().parent is None, where, "the rig got a parent")
    if rig_mode != "KEEP":
        on_the_rig(where)


MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")
RIG_MODES = ("APPEND", "REGENERATE", "KEEP")
STEPS = []
for m in MODES:
    STEPS.append((lambda m=m: the_rig_under_an_empty(m, [], [], "APPEND"),
                  "the rig under an empty, %s, nothing moves" % m))
    STEPS.append((lambda m=m: the_rig_under_an_empty(m, JOINTS, JOINTS_2,
                                                     "APPEND"),
                  "the rig under an empty, %s, joints" % m))
    STEPS.append((lambda m=m: no_rig(m), "no rig, %s" % m))
for r in RIG_MODES:
    STEPS.append((lambda r=r: the_parts_under_an_empty(r),
                  "the parts under an empty, %s" % r))
    STEPS.append((lambda r=r: the_rig_under_an_empty("FLAT", JOINTS, JOINTS_2,
                                                     r),
                  "the rig under an empty, FLAT, joints, %s" % r))
    STEPS.append((lambda r=r: all_under_an_empty(r),
                  "all the objects under an empty, %s" % r))
    STEPS.append((lambda r=r: the_rig_moved(r), "the rig moved, %s" % r))
    STEPS.append((lambda r=r: not_moved(r), "not moved, %s" % r))
STEPS += [(moved_as_one, "the parts moved as one"),
          (one_part_alone, "one part moved alone")]
FAILED = []
for step, name in STEPS:
    try:
        step()
        print("   ok: %s" % name)
    except SystemExit as stop:
        FAILED.append(str(stop))
        print("   %s" % stop)
if FAILED:
    raise SystemExit("native_follow_smoke: %d FAIL(s)" % len(FAILED))
print("native_follow_smoke: OK")
