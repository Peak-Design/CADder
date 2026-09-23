# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: Refresh with Build a New Rig keeps the mechanism input
the user chose.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_choice_refresh_smoke.py

The input of a mechanism is kept on the armature. Build Rig writes it
there, but a Refresh that builds a new rig did not, so the new rig came
without it and the next send went back to the default input. The
mechanism is rig_stretch_smoke's slider-crank with a second rod: two
candidate inputs, the crank (the default) and the slider.
"""

import json
import os
import sys
import tempfile

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from CADder import rig  # noqa: E402
from CADder.ci import rebuild_fixture as fx  # noqa: E402
from CADder.rig import ui  # noqa: E402


def _manifest():
    src = open(os.path.join(_HERE, "rig_stretch_smoke.py"), encoding="utf-8").read()
    src = src.replace("\nmain()\n", "\n")
    ns = {"__file__": os.path.join(_HERE, "rig_stretch_smoke.py"),
          "__name__": "smoke_manifest"}
    exec(compile(src, "rig_stretch_smoke.py", "exec"), ns)
    return ns["MANIFEST"]


def export(tmp):
    data = _manifest()
    data["step_export"]["file"] = "choice.step"
    parts = [fx.Part(c["id"], c["sw_path"], "p" + c["id"], c["transform"], 0,
                     name=c["step_name"])
             for c in data["components"]]
    man = os.path.join(tmp, "choice.rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return fx.write_mesh(os.path.join(tmp, "choice.swmesh"), parts), man


def fail(msg):
    raise SystemExit("rig_choice_refresh_smoke: FAIL: %s" % msg)


def main():
    tmp = os.path.join(tempfile.gettempdir(), "rig_choice_refresh_smoke")
    os.makedirs(tmp, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    scene = bpy.context.scene

    mesh, man = export(tmp)
    if not fx.send(mesh, man, "FLAT", "ZPOS").get("ok"):
        fail("the send failed")
    entry = scene.cad_link.mechanisms[0]
    if entry.driver != "j001":
        fail("the default input is %s, not the crank" % entry.driver)
    # The user picks the slider. The rig is built again with it.
    entry.driver = "j002"
    if not fx.rig_object().get("RIG_driver_choice"):
        fail("Build Rig did not keep the choice")

    mesh, man = export(tmp)
    result = fx.send(mesh, man, "FLAT", "ZPOS", update=True,
                     rig_mode="REGENERATE")
    if not result.get("ok"):
        fail("the refresh failed: %s" % result.get("error"))
    arm = fx.rig_object()
    stored = json.loads(arm.get("RIG_driver_choice") or "[]")
    if stored != ["ground-1|slider-1|prismatic"]:
        fail("the new rig keeps the choice %s" % stored)

    # The next send reads the choice off the new rig.
    mesh, man = export(tmp)
    if not fx.send(mesh, man, "FLAT", "ZPOS", update=True,
                   rig_mode="APPEND").get("ok"):
        fail("the second refresh failed")
    entry = scene.cad_link.mechanisms[0]
    if entry.driver != "j002":
        fail("the next send went back to the input %s" % entry.driver)
    print("rig_choice_refresh_smoke: OK: a rig built new by Refresh keeps "
          "the input the user chose, and the next send uses it")


main()
