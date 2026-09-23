# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Rebuild from CAD > Poses, and the pose push.

    blender -b --factory-startup --python-exit-code 1 -P ci/rebuild_poses_smoke.py [-- case ...]

The CAD application says where the parts sit now, and the scene follows.
The CAD application is a stand-in that answers "poses" from an assembly
written here, so the whole Blender half runs for real.

  renumbered   A part deleted in CAD renumbers the parts after it. The reply
               names each pose under the new ids, so a pose is matched by
               the persistent id: before, one part took another's pose.
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


CASES = {
    "renumbered": case_renumbered,
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
