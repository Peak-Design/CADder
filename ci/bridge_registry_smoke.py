# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: the bridge's registry file says which CAD documents the
scenes of this Blender hold.

    blender -b --factory-startup --python-exit-code 1 -P ci/bridge_registry_smoke.py

The CAD add-in enables Refresh Model from the registry files, without a
ping: the button is grey unless a running Blender holds a scene of the
active document. A Blender that crashed or closed, or a new Blender that
never got the send, held the button on before, and a refresh then went to a
scene without the model. The list comes from the document tag a send
writes on its scene, so a saved file opened again says the same.

The registry goes to a temporary folder: a CAD add-in on this machine
never finds this Blender.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import cad_link  # noqa: E402

LIFT = r"C:\cad\lift.SLDASM"
LIFT_B = r"C:\cad\lift rev B.SLDASM"

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "lift.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "base-1", "step_name": "base",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.1},
    ],
    "joints": [],
    "loops": [],
    "warnings": [],
}


def fail(msg):
    raise SystemExit("bridge_registry_smoke: FAIL: %s" % msg)


def registry(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def documents(path):
    reg = registry(path)
    if "documents" not in reg:
        fail("the registry file has no documents list: %s" % reg)
    return reg["documents"]


def ping(port, token):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/cadlink/ping" % port,
        headers={"X-CADLink-Token": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    folder = tempfile.mkdtemp(prefix="cadder_registry_")
    os.environ["LOCALAPPDATA"] = folder
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    manifest_path = os.path.join(folder, "lift.rig.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(MANIFEST, fh)
    bridge.start()
    try:
        path = os.path.join(bridge.registry_dir(), "%d.json" % os.getpid())
        if not path.startswith(folder):
            fail("the registry is not in the temporary folder: %s" % path)
        if documents(path) != []:
            fail("an empty file lists documents: %s" % documents(path))

        # A scene tagged by hand, as a saved file opened again has it.
        scene = bpy.context.scene
        scene[cad_link.DOCUMENT_TAG] = LIFT
        bridge._keep_registry(force=True)
        if documents(path) != [LIFT]:
            fail("the tagged scene is not listed: %s" % documents(path))

        # A send through the pump, the way the add-in's request runs. The
        # file must be right before the reply, so the check that runs at
        # the start of the pump is kept from running.
        bridge._registry_checked = time.monotonic()
        job = bridge._Job({
            "manifest": manifest_path, "source_document": LIFT_B,
            "steps": {"import": False, "sync_poses": False, "cleanup": False},
        })
        bridge._state["queue"].put(job)
        bridge._pump()
        if not job.done.is_set() or not job.result.get("ok"):
            fail("the send did not finish: %s" % job.result)
        if documents(path) != [LIFT_B]:
            fail("the registry did not follow the send before the reply: %s"
                 % documents(path))

        # The ping says the same, for Discover.
        reg = registry(path)
        answer = ping(reg["port"], reg["token"])
        if answer.get("documents") != [LIFT_B]:
            fail("the ping does not list the document: %s" % answer)

        # Two scenes, two documents.
        other = bpy.data.scenes.new("Other")
        other[cad_link.DOCUMENT_TAG] = LIFT
        bridge._keep_registry(force=True)
        if documents(path) != sorted([LIFT, LIFT_B]):
            fail("two tagged scenes are not both listed: %s" % documents(path))

        # The scenes go: nothing is held any more, and the button goes grey.
        bpy.data.scenes.remove(other)
        del scene[cad_link.DOCUMENT_TAG]
        bridge._keep_registry(force=True)
        if documents(path) != []:
            fail("removed scenes are still listed: %s" % documents(path))

        # The add-in deletes the file of a Blender that missed a ping. It
        # comes back with the list.
        scene[cad_link.DOCUMENT_TAG] = LIFT
        os.remove(path)
        bridge._keep_registry(force=True)
        if not os.path.exists(path) or documents(path) != [LIFT]:
            fail("the registry file did not come back with its list")
    finally:
        bridge.stop()
    if os.path.exists(path):
        fail("stop() left the registry file")
    shutil.rmtree(folder, ignore_errors=True)
    print("bridge_registry_smoke: OK: the registry lists the documents the "
          "scenes hold, follows a send before the reply, and the ping says "
          "the same")


main()
