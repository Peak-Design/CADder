# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: the geometry the CAD application writes to the temp
folder for a rebuild is deleted once Blender has read it.

    blender -b --factory-startup --python-exit-code 1 -P ci/rebuild_temp_files_smoke.py

A request for geometry again names no file to write, so the add-in writes
cadlink-refine-<guid>.swmesh in the temp folder and answers with its path.
Nothing deleted it, so each Rebuild from CAD > Geometry and each send with
defeatured parts left one file there: hundreds of MB over a working
session at a fine quality. The three places that ask for geometry again
are covered: Rebuild from CAD, the defeature and UV buttons, and a send
that asks again for its defeatured parts.
"""

import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, tools  # noqa: E402
from CADder.ci import rebuild_fixture as fx  # noqa: E402

PARTS = [fx.Part("c001", "base-1", "pbase", fx.translate(0.0), 0),
         fx.Part("c002", "arm-1", "parm", fx.translate(0.2), 1)]


def fail(msg):
    raise SystemExit("rebuild_temp_files_smoke: FAIL: %s" % msg)


class Answers:
    """The add-in's answers, with a new file in the temp folder for every
    request for geometry, as the add-in writes one."""

    def __init__(self):
        self.written = []

    def retessellate(self, fields):
        path = os.path.join(tempfile.gettempdir(),
                            "cadlink-refine-%s.swmesh" % uuid.uuid4().hex)
        fx.write_mesh(path, PARTS)
        self.written.append(path)
        return {"ok": True, "mesh": path, "triangles": 2,
                "tolerance_m": 0.0005}


def left(answers, where):
    kept = [p for p in answers.written if os.path.exists(p)]
    for path in kept:
        os.unlink(path)
    if kept:
        fail("%s left %d file(s) in the temp folder: %s"
             % (where, len(kept), kept))
    if not answers.written:
        fail("%s asked the CAD application for nothing" % where)


def main():
    tmp = os.path.join(tempfile.gettempdir(), "rebuild_temp_files_smoke")
    os.makedirs(tmp, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    joints = [fx.revolute("j001", 0, 1, (0.2, 0.0, 0.0))]
    mesh, man = fx.export(tmp, PARTS, ("base", "arm"), joints)
    if not fx.send(mesh, man, "FLAT", "ZPOS").get("ok"):
        fail("the send failed")

    # Rebuild from CAD, with Geometry and with Geometry and Poses.
    for what in ("GEOMETRY", "GEOMETRY_POSES"):
        answers = Answers()
        fx.select_none()
        with fx.FakeCad(retessellate=answers.retessellate,
                        poses=lambda f: fx.poses_reply(PARTS)):
            result = bpy.ops.cadlink.update_from_cad(what=what)
        if "FINISHED" not in result:
            fail("Rebuild from CAD %s did not finish" % what)
        left(answers, "Rebuild from CAD " + what)

    # The defeature and UV buttons.
    answers = Answers()
    objs = list(fx.parts_by_component().values())
    with fx.FakeCad(retessellate=answers.retessellate):
        if not tools._ask_cad_link(bpy.context, objs):
            fail("the buttons' request brought nothing back")
    left(answers, "the defeature and UV buttons")

    # A send that asks again for its defeatured parts, on a timer that runs
    # here at once.
    answers = Answers()
    real = bridge.bpy
    bridge.bpy = SimpleNamespace(
        context=bpy.context,
        app=SimpleNamespace(timers=SimpleNamespace(
            register=lambda fn, first_interval=0.0: fn())))
    try:
        with fx.FakeCad(retessellate=answers.retessellate):
            bridge._ask_again_without_small_features(
                [{"component": "c002", "size_m": 0.002}], {}, None)
    finally:
        bridge.bpy = real
    left(answers, "a send that asks again for its defeatured parts")

    # A file the add-in did not name that way is not the reply's to delete.
    own = os.path.join(tmp, "mine.swmesh")
    fx.write_mesh(own, PARTS)
    with fx.FakeCad(retessellate=lambda f: {"ok": True, "mesh": own}):
        tools._ask_cad_link(bpy.context, objs)
    if not os.path.exists(own):
        fail("a file of another name was deleted")
    print("rebuild_temp_files_smoke: OK: every geometry reply was deleted "
          "once it was read, and a file of another name was kept")


main()
