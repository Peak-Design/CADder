# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for what the Rig panel keeps between operators.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_panel_state_smoke.py

  error  A CAD link error stays in the panel only until the next run of
         the button succeeds. Before, Rebuild from CAD with Geometry or
         Poses never cleared it, so the red box stayed after the CAD
         application was started and the rebuild worked.
  undo   The operators that change the scene push an undo step, so Ctrl+Z
         after Build Rig undoes the build and not also the step before it.
"""

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.ci import rebuild_fixture as fx  # noqa: E402
from CADder.rig import cad_link, ui as rig_ui  # noqa: E402


def fail(msg):
    raise SystemExit("rig_panel_state_smoke: FAIL: %s" % msg)


def run(what, answers):
    fx.select_none()
    with fx.FakeCad(**answers):
        try:
            return bpy.ops.cadlink.update_from_cad(what=what)
        except RuntimeError as exc:
            print("rig_panel_state_smoke: the operator said:", exc)
            return {"CANCELLED"}


def check_error(tmp):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # The whole add-on: a rebuild of the geometry reads the Mesh Quality
    # settings of the scene.
    bpy.ops.preferences.addon_enable(module="CADder")
    parts = [fx.Part("c001", "base-1", "pbase", fx.translate(0.0), 0),
             fx.Part("c002", "arm-1", "parm", fx.translate(0.2), 1)]
    joints = [fx.revolute("j001", 0, 1, (0.2, 0.0, 0.0))]
    mesh, man = fx.export(tmp, parts, ("base", "arm"), joints)
    if not fx.send(mesh, man, "FLAT", "ZPOS").get("ok"):
        fail("the send failed")

    def closed(fields):
        raise cad_link.CadLinkError(
            "No CAD application was found. Start SolidWorks with the "
            "CADder Bridge add-in enabled, and open the assembly.")

    moved = [fx.Part("c001", "base-1", "pbase", fx.translate(0.0), 0),
             fx.Part("c002", "arm-1", "parm", fx.translate(0.2), 1)]
    for what in ("POSES", "GEOMETRY"):
        run(what, {"poses": closed, "retessellate": closed})
        if not rig_ui._STATE["error"]:
            fail("%s: the CAD link error did not reach the panel" % what)

        def retessellate(fields):
            path = fx.write_mesh(os.path.join(tmp, "again.swmesh"), moved)
            return {"ok": True, "mesh": path, "triangles": 1,
                    "tolerance_m": 0.0005}

        result = run(what, {"poses": lambda f: fx.poses_reply(moved),
                            "retessellate": retessellate})
        if "FINISHED" not in result:
            fail("%s: the second run did not finish" % what)
        if rig_ui._STATE["error"]:
            fail("%s: the panel still shows %r after it worked"
                 % (what, rig_ui._STATE["error"]))


def check_undo():
    for name in ("CADLINK_OT_build_rig", "CADLINK_OT_relink_geometry",
                 "CADLINK_OT_sync_poses", "CADLINK_OT_match_geometry",
                 "CADLINK_OT_load_manifest"):
        cls = getattr(bpy.types, name)
        if "UNDO" not in getattr(cls, "bl_options", set()):
            fail("%s pushes no undo step" % name)


def main():
    tmp = os.path.join(tempfile.gettempdir(), "rig_panel_state_smoke")
    os.makedirs(tmp, exist_ok=True)
    check_error(tmp)
    check_undo()
    print("rig_panel_state_smoke: OK: the error clears on success, and the "
          "operators that change the scene push an undo step")


main()
