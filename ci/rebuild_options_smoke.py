# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: Rebuild from CAD in a new session asks for the assembly
with the options the scene was sent with.

    blender -b --factory-startup --python-exit-code 1 -P ci/rebuild_options_smoke.py

Refresh and Full Reimport ask the CAD application for the assembly again
and build it with the import options of the last send. Those options were
kept only in the memory of the session. After Blender restarted, a Refresh
of a scene sent Y-up with parented empties came back Z-up and flat: every
part on the rig turned 90 degrees, and the rig was built again in the
other frame. The options are now kept in the file, and a file saved
before that reads them off the scene itself.
"""

import os
import sys
import tempfile

import bpy
from mathutils import Matrix

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import rig  # noqa: E402
from CADder.ci import rebuild_fixture as fx  # noqa: E402
from CADder.rig import native_import, ui as rig_ui  # noqa: E402

TOL = 1e-5
NAMES = ("base", "arm", "clip", "bolt")


def assembly(new_part=False):
    parts = [
        fx.Part("c001", "base-1", "pbase", fx.translate(0.0), 0),
        fx.Part("c002", "armsub-1/arm-1", "parm", fx.translate(0.2), 1),
        fx.Part("c003", "armsub-1/pin-1", "ppin", fx.translate(0.3), 1),
        fx.Part("c004", "clip-1", "pclip", fx.translate(0.4), 2),
    ]
    if new_part:
        # A part added in CAD, inside a subassembly of its own. Where it
        # lands in the tree is what the hierarchy option decides.
        parts.append(fx.Part("c005", "boltsub-1/bolt-1", "pbolt",
                             fx.translate(0.5, 0.1), 3))
    joints = [fx.revolute("j001", 0, 1, (0.2, 0.0, 0.0)),
              fx.revolute("j002", 1, 2, (0.4, 0.0, 0.0))]
    if new_part:
        joints.append(fx.revolute("j003", 0, 3, (0.5, 0.1, 0.0)))
    return parts, joints


def fail(where, msg):
    raise SystemExit("rebuild_options_smoke: FAIL: %s: %s" % (where, msg))


def check_frame(where, up_as):
    frame = Matrix([tuple(r) for r in native_import.up_frame(up_as)])
    for cid, obj in sorted(fx.parts_by_component().items()):
        rows = [list(obj["SWMESH_transform"][i * 4:(i + 1) * 4])
                for i in range(4)]
        d = fx.off(obj.matrix_world, frame @ Matrix(rows))
        if d > TOL:
            fail(where, "%s (%s) is %.4f off its pose in the %s frame"
                 % (obj.name, cid, d, up_as))
    arm = fx.rig_object()
    if arm is None:
        fail(where, "no rig")
    got = Matrix([tuple(arm["RIG_frame"][i * 4:(i + 1) * 4]) for i in range(4)])
    if fx.off(got, frame) > TOL:
        fail(where, "the rig was built in another frame than %s" % up_as)


def under_branch(obj, path):
    holder = obj.parent
    while holder is not None:
        if holder.type == "EMPTY" and holder.get("SWMESH_path") == path:
            return True
        holder = holder.parent
    return False


def forget_session():
    """What a restart of Blender leaves: nothing in the module state."""
    rig_ui._STATE["import_options"] = None
    rig_ui._STATE["manifest"] = None
    rig_ui._STATE["match_report"] = None
    rig_ui._STATE["build"] = None


def refresh(tmp, where, what):
    parts, joints = assembly(new_part=True)

    def answer(fields):
        mesh, man = fx.export(tmp, parts, NAMES, joints)
        return {"ok": True, "mesh": mesh, "manifest": man}

    fx.select_none()
    with fx.FakeCad(export=answer) as cad:
        result = bpy.ops.cadlink.update_from_cad(what=what)
    if "FINISHED" not in result:
        fail(where, "Rebuild from CAD did not finish: %s" % (result,))
    if not cad.seen or cad.seen[-1]["op"] != "export":
        fail(where, "the CAD application was not asked for the assembly")


def run(tmp, what, keep_tag):
    where = "%s, %s" % (what, "options in the file" if keep_tag
                        else "a file saved before the options were kept")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    parts, joints = assembly()
    mesh, man = fx.export(tmp, parts, NAMES, joints)
    # The add-in's own defaults: Y up, parented empties.
    result = fx.send(mesh, man, "EMPTIES", "YPOS")
    if not result.get("ok"):
        fail(where, "the send failed: %s" % result.get("error"))
    check_frame(where + ", after the send", "YPOS")

    forget_session()
    if not keep_tag:
        for key in list(bpy.context.scene.keys()):
            if key.startswith("CADLINK_import"):
                del bpy.context.scene[key]

    refresh(tmp, where, what)
    check_frame(where + ", after the rebuild", "YPOS")
    bolt = fx.parts_by_component().get("c005")
    if bolt is None:
        fail(where, "the part added in CAD did not arrive")
    if not under_branch(bolt, "boltsub-1"):
        fail(where, "the part added in CAD is not under its subassembly's "
             "empty: the scene was not rebuilt with parented empties")
    return where


def read_back(tmp, hierarchy, up_as, rig_it):
    """A file saved before the options were kept: what the scene itself
    says it was sent with."""
    where = "read back %s %s %s" % (hierarchy, up_as,
                                    "with a rig" if rig_it else "no rig")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    parts, joints = assembly(new_part=True)
    mesh, man = fx.export(tmp, parts, NAMES, joints)
    steps = None if rig_it else {"build_rig": False, "relink": False}
    result = fx.send(mesh, man, hierarchy, up_as, steps=steps)
    if not result.get("ok"):
        fail(where, "the send failed: %s" % result.get("error"))
    forget_session()
    for key in list(bpy.context.scene.keys()):
        if key.startswith("CADLINK_import"):
            del bpy.context.scene[key]
    got = rig_ui._import_options(bpy.context, "app")
    if got.get("up_as") != up_as:
        fail(where, "read the up axis as %s" % got.get("up_as"))
    if got.get("hierarchy_types") != hierarchy:
        fail(where, "read the hierarchy as %s" % got.get("hierarchy_types"))
    return where


def main():
    tmp = os.path.join(tempfile.gettempdir(), "rebuild_options_smoke")
    os.makedirs(tmp, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    done = []
    for what in ("REFRESH", "EVERYTHING"):
        for keep_tag in (True, False):
            done.append(run(tmp, what, keep_tag))
    for hierarchy in native_import.HIERARCHIES:
        for up_as in ("XPOS", "YPOS", "ZPOS"):
            for rig_it in (True, False):
                done.append(read_back(tmp, hierarchy, up_as, rig_it))
    print("rebuild_options_smoke: OK: %d rebuilds and read-backs kept the up "
          "axis and the hierarchy the scene was sent with" % len(done))


main()
