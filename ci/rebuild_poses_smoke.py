# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Rebuild from CAD > Poses, and the pose push.

    blender -b --factory-startup --python-exit-code 1 -P ci/rebuild_poses_smoke.py [-- case ...]

The CAD application says where the parts sit now, and the scene follows.
The CAD application is a stand-in that answers "poses" from an assembly
written here, so the whole Blender half runs for real.

  renumbered   A part deleted in CAD renumbers the parts after it. The reply
               names each pose under the new ids, so a pose is matched by
               the persistent id: before, one part took another's pose.
  two_link     A two-link arm, link 1 turned 90 degrees in CAD. The bones
               follow the parts: link 2 turns about pin B where pin B is
               NOW. Before, the rig was built again from the joints of the
               export, so link 2 turned about where pin B used to be.
  keeps_rig    The armature, its animation, a bone the user added and the
               place the user moved the rig to all stay. Before, the rig
               was thrown away and built again at the origin.
  new_session  After a restart there is no manifest in memory. The rig is
               still posed from what the file holds. Before, nothing moved
               and the status said "Moved 0 part(s)". A reply that names no
               part of the scene is an error, not a quiet zero.
  pose_push    The same poses pushed from SolidWorks through the bridge.
  every_joint  The demo rig of rig_smoke (a four-bar, a screw, a gear, balls,
               a cone on a plane, mirrors, a pin in a slot) is posed by
               hand, the poses of its parts are read as the CAD poses, and
               the rig at rest is posed back onto them.
"""

import math
import os
import sys
import tempfile

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import rig  # noqa: E402
from CADder.ci import rebuild_fixture as fx  # noqa: E402
from CADder.rig import native_import, ui as rig_ui  # noqa: E402

TOL = 1e-4


def fail(where, msg):
    raise SystemExit("rebuild_poses_smoke: FAIL: %s: %s" % (where, msg))


def frame_of(up_as):
    return Matrix([tuple(r) for r in native_import.up_frame(up_as)])


def forget_session():
    """What a restart of Blender leaves: nothing in the module state."""
    for key in ("manifest", "plan", "match_report", "pose_report", "build",
                "parent_report", "import_options", "rig_snapshot"):
        rig_ui._STATE[key] = None
    rig_ui._STATE["error"] = ""


def run_poses(parts, select=None, expect="FINISHED"):
    """Rebuild from CAD > Poses, the CAD application answering with the
    poses of `parts`. Returns the stand-in, which kept the requests."""
    fx.select_none()
    for obj in select or []:
        obj.select_set(True)
    with fx.FakeCad(poses=lambda fields: fx.poses_reply(parts)) as cad:
        try:
            result = bpy.ops.cadlink.update_from_cad(what="POSES")
        except RuntimeError as exc:
            # An operator that reports an error raises when Python calls it.
            print("rebuild_poses_smoke: the operator said:", exc)
            result = {"CANCELLED"}
    if expect not in result:
        fail("poses", "Rebuild from CAD returned %s, not %s (%s)"
             % (result, expect, rig_ui._STATE.get("error")))
    bpy.context.view_layer.update()
    return cad


def world_x(obj):
    return obj.matrix_world.translation.x


# ── renumbered ──────────────────────────────────────────────────────────

def case_renumbered(tmp):
    where = "renumbered"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    names = ("base", "d", "x", "y")
    before = [fx.Part("c001", "base-1", "pbase", fx.translate(0.0), 0),
              fx.Part("c002", "d-1", "pd", fx.translate(0.2), 1),
              fx.Part("c003", "x-1", "px", fx.translate(0.3), 2),
              fx.Part("c004", "y-1", "py", fx.translate(0.4), 3)]
    joints = [fx.revolute("j%03d" % g, 0, g, (0.1 * g, 0.0, 0.0))
              for g in (1, 2, 3)]
    mesh, man = fx.export(tmp, before, names, joints)
    if not fx.send(mesh, man, "FLAT", "ZPOS",
                   steps={"build_rig": False, "relink": False}).get("ok"):
        fail(where, "the send failed")
    objs = fx.parts_by_component()
    d, x, y = objs["c002"], objs["c003"], objs["c004"]
    # D is deleted in CAD, so the walk now calls X c002 and Y c003. X and Y
    # moved too.
    after = [fx.Part("c001", "base-1", "pbase", fx.translate(0.0), 0),
             fx.Part("c002", "x-1", "px", fx.translate(0.5), 2),
             fx.Part("c003", "y-1", "py", fx.translate(0.7), 3)]
    cad = run_poses(after, select=[x])
    asked = cad.seen[-1]
    if asked.get("components") != ["c003"] or asked.get("persistent_ids") != ["px"]:
        fail(where, "the request was %s" % asked)
    for obj, want in ((d, 0.2), (x, 0.5), (y, 0.7)):
        if abs(world_x(obj) - want) > 1e-6:
            fail(where, "%s is at x=%.3f, not %.3f" % (obj.name, world_x(obj), want))
    return where


# ── the two-link arm ────────────────────────────────────────────────────

LINK_NAMES = ("base", "link1", "link2")
PIN_B = (0.2, 0.0, 0.0)
TURN_1 = math.radians(90.0)
TURN_2 = math.radians(30.0)


def two_link(moved):
    """The base, link 1 on pin A at the origin, link 2 on pin B. Link 1 is
    two parts in a subassembly, so the tree modes have a branch to keep."""
    one = fx.turn_z(TURN_1) if moved else fx.translate(0.0)
    two = (fx.mul(fx.turn_z(TURN_1), fx.turn_z(TURN_2, about=PIN_B))
           if moved else fx.translate(0.0))
    parts = [
        fx.Part("c001", "base-1", "pbase", fx.translate(0.0, -0.1), 0),
        fx.Part("c002", "link1sub-1/plate-1", "pplate",
                fx.mul(one, fx.translate(0.05)), 1),
        fx.Part("c003", "link1sub-1/boss-1", "pboss",
                fx.mul(one, fx.translate(0.15)), 1),
        fx.Part("c004", "link2-1", "plink2", fx.mul(two, fx.translate(0.3)), 2),
    ]
    joints = [fx.revolute("j001", 0, 1, (0.0, 0.0, 0.0)),
              fx.revolute("j002", 1, 2, PIN_B,
                          limits={"rotation": {"min": -2.0, "max": 2.0,
                                               "value_at_rest": 0.0},
                                  "translation": None})]
    return parts, joints


def send_two_link(tmp, hierarchy, up_as):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    parts, joints = two_link(False)
    mesh, man = fx.export(tmp, parts, LINK_NAMES, joints)
    result = fx.send(mesh, man, hierarchy, up_as)
    if not result.get("ok"):
        fail("two_link", "the send failed: %s" % result.get("error"))
    arm = fx.rig_object()
    if arm is None:
        fail("two_link", "no rig")
    return arm


def check_on_cad(where, parts, placed, frame):
    objs = fx.parts_by_component()
    for p in parts:
        obj = objs[p.cid]
        want = placed @ frame @ Matrix(p.rows)
        d = fx.off(obj.matrix_world, want)
        if d > TOL:
            fail(where, "%s (%s) is %.5f off its CAD pose" % (obj.name, p.cid, d))


def check_pin_b(where, arm, frame):
    """Link 2's bone turns about pin B where pin B now is, and link 2 turns
    with it about that point."""
    pb = fx.bone_of(arm, "g002")
    # Pin B at (0.2, 0, 0), turned 90 degrees about pin A with link 1.
    pin = arm.matrix_world @ frame @ Vector((0.0, 0.2, 0.0))
    head = arm.matrix_world @ pb.head
    if (head - pin).length > TOL:
        fail(where, "link 2's bone is at %s, not on pin B at %s"
             % (tuple(round(v, 4) for v in head), tuple(round(v, 4) for v in pin)))
    link2 = fx.parts_by_component()["c004"]
    local = link2.matrix_world.inverted() @ pin
    pb.rotation_euler.y += 0.3
    bpy.context.view_layer.update()
    moved = link2.matrix_world @ local
    pb.rotation_euler.y -= 0.3
    bpy.context.view_layer.update()
    if (moved - pin).length > TOL:
        fail(where, "turning link 2 moved it off pin B by %.5f"
             % (moved - pin).length)


def case_two_link(tmp):
    done = []
    for hierarchy in ("FLAT", "EMPTIES", "COLLECTION_INSTANCES"):
        for up_as in ("ZPOS", "YPOS"):
            where = "two_link %s %s" % (hierarchy, up_as)
            arm = send_two_link(tmp, hierarchy, up_as)
            parts, _joints = two_link(True)
            run_poses(parts)
            frame = frame_of(up_as)
            check_on_cad(where, parts, Matrix.Identity(4), frame)
            check_pin_b(where, fx.rig_object(), frame)
            if fx.rig_object() is not arm:
                fail(where, "the armature was replaced")
            done.append(where)
    return done


def case_keeps_rig(tmp):
    where = "keeps_rig"
    arm = send_two_link(tmp, "EMPTIES", "YPOS")
    arm_name = arm.name
    placed = Matrix.Translation((1.0, 2.0, 0.5)) @ Matrix.Rotation(0.4, 4, "Z")
    arm.matrix_world = placed
    # A bone of the user's own, and an animation on link 1.
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones.new("mine")
    eb.head = (0.0, 0.0, 0.0)
    eb.tail = (0.0, 0.1, 0.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    arm["users_note"] = "kept"
    scene = bpy.context.scene
    scene.frame_set(1)
    link1 = fx.bone_of(arm, "g001")
    link1.keyframe_insert("rotation_euler", index=1, frame=1)
    action = arm.animation_data.action
    bpy.context.view_layer.update()

    parts, _joints = two_link(True)
    run_poses(parts)
    now = fx.rig_object()
    if now is None or now.name != arm_name or now is not arm:
        fail(where, "the armature was replaced")
    if arm.animation_data is None or arm.animation_data.action is not action:
        fail(where, "the animation is gone")
    if "mine" not in arm.data.bones:
        fail(where, "the bone the user added is gone")
    if arm.get("users_note") != "kept":
        fail(where, "the armature lost its custom property")
    if fx.off(arm.matrix_world, placed) > 1e-6:
        fail(where, "the armature is no longer where the user placed it")
    check_on_cad(where, parts, placed, frame_of("YPOS"))
    # The key still drives link 1: at its frame, the rig goes back to rest.
    scene.frame_set(2)
    scene.frame_set(1)
    bpy.context.view_layer.update()
    rest, _joints = two_link(False)
    check_on_cad(where + ", keyed frame",
                 [p for p in rest if p.cid in ("c002", "c003")],
                 placed, frame_of("YPOS"))
    return where


def case_new_session(tmp):
    where = "new_session"
    send_two_link(tmp, "FLAT", "YPOS")
    forget_session()
    parts, _joints = two_link(True)
    run_poses(parts)
    check_on_cad(where, parts, Matrix.Identity(4), frame_of("YPOS"))
    check_pin_b(where, fx.rig_object(), frame_of("YPOS"))

    # A reply that names nothing in this scene.
    forget_session()
    strangers = [fx.Part("c099", "other-1", "pother", fx.translate(1.0), 0)]
    run_poses(strangers, expect="CANCELLED")
    if not rig_ui._STATE.get("error"):
        fail(where, "a reply that moved nothing left no error in the panel")
    return where


def case_pose_push(tmp):
    """The same poses pushed from SolidWorks, through the bridge."""
    from CADder import bridge
    where = "pose_push"
    arm = send_two_link(tmp, "EMPTIES", "YPOS")
    parts, _joints = two_link(True)
    result = bridge._run_job({"poses": fx.poses_reply(parts)})
    if not result.get("ok"):
        fail(where, "the push failed: %s" % result.get("error"))
    if result["stages"]["poses"]["moved"] < 3:
        fail(where, "the push moved %s part(s)" % result["stages"]["poses"]["moved"])
    bpy.context.view_layer.update()
    frame = frame_of("YPOS")
    check_on_cad(where, parts, Matrix.Identity(4), frame)
    check_pin_b(where, arm, frame)
    if fx.rig_object() is not arm:
        fail(where, "the armature was replaced")
    return where


# ── every joint of the demo rig ─────────────────────────────────────────

def case_every_joint(tmp):
    import json
    from CADder.ci import rig_smoke
    where = "every_joint"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    data = rig_smoke._demo_manifest()
    parts = []
    group_of = {}
    for g in data["rigid_groups"]:
        for cid in g["components"]:
            group_of[cid] = int(g["id"][1:])
    for comp in data["components"]:
        comp["sw_persistent_id"] = "p" + comp["id"]
        parts.append(fx.Part(comp["id"], comp["sw_path"], comp["sw_persistent_id"],
                             comp["transform"], group_of[comp["id"]]))
    man = os.path.join(tmp, "demo.rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    mesh = fx.write_mesh(os.path.join(tmp, "demo.swmesh"), parts)
    up_as = "YPOS"
    if not fx.send(mesh, man, "FLAT", up_as).get("ok"):
        fail(where, "the send failed")
    arm = fx.rig_object()
    frame = frame_of(up_as)
    # The user placed the machine somewhere.
    arm.matrix_world = (Matrix.Translation((0.3, -0.2, 0.1))
                        @ Matrix.Rotation(0.5, 4, "Z"))
    bpy.context.view_layer.update()

    # Pose every control by hand, a little in every channel it has.
    controls = arm.data.collections.get("SW_controls")
    values = (0.21, -0.13, 0.17)
    for bone in controls.bones:
        pb = arm.pose.bones[bone.name]
        for i in range(3):
            if not pb.lock_rotation[i]:
                pb.rotation_euler[i] = values[i]
            if not pb.lock_location[i]:
                pb.location[i] = 0.01 * (i + 1)
    bpy.context.view_layer.update()
    objs = fx.parts_by_component()
    posed = {cid: obj.matrix_world.copy() for cid, obj in objs.items()}
    inverse = (arm.matrix_world @ frame).inverted()
    cad = []
    for p in parts:
        rows = inverse @ posed[p.cid]
        cad.append(fx.Part(p.cid, p.path, p.pid, [list(r) for r in rows], p.group))
    moved = sum(1 for p in parts
                if fx.off(posed[p.cid], frame @ Matrix(p.rows)) > 1e-3)
    if moved < 8:
        fail(where, "posing the controls moved only %d part(s)" % moved)

    for pb in arm.pose.bones:
        pb.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    run_poses(cad)
    worst = []
    largest = 0.0
    for p in parts:
        d = fx.off(objs[p.cid].matrix_world, posed[p.cid])
        largest = max(largest, d)
        if d > TOL:
            worst.append("%s %.5f" % (objs[p.cid].name, d))
    if worst:
        fail(where, "parts off the pose the rig was in: %s" % ", ".join(worst))
    held = rig_ui._STATE["pose_report"].skipped
    if held:
        fail(where, "parts reported as held: %s" % held)
    return "%s (%d parts, worst %.1e)" % (where, len(parts), largest)


CASES = {
    "renumbered": case_renumbered,
    "two_link": case_two_link,
    "keeps_rig": case_keeps_rig,
    "new_session": case_new_session,
    "pose_push": case_pose_push,
    "every_joint": case_every_joint,
}


def main():
    wanted = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    tmp = os.path.join(tempfile.gettempdir(), "rebuild_poses_smoke")
    os.makedirs(tmp, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    done = []
    for name, case in CASES.items():
        if wanted and name not in wanted:
            continue
        got = case(tmp)
        done.extend(got if isinstance(got, list) else [got])
    print("rebuild_poses_smoke: OK: %s" % ", ".join(done))


main()
