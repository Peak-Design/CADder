# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a request back to the CAD application names the document
the scene came from.

    blender -b --factory-startup --python-exit-code 1 -P ci/cad_link_document_smoke.py

A request from Blender (Rebuild from CAD, the pose read, the defeature
request) went to the ACTIVE document in SolidWorks. With a second document
open and active there, Rebuild from CAD read the wrong assembly into the
scene. Each send now names its document (source_document), the bridge
writes it on the scene, and every request carries it as document_path.

A stand-in for the add-in's listener records what Blender asks. It is
given to the client directly, so a SolidWorks that runs on this machine is
never found.
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import cad_link  # noqa: E402

TOKEN = "smoke-token"
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
        {"id": "c002", "sw_path": "arm-1", "step_name": "arm",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0.05], [0, 1, 0, 0], [0, 0, 1, 0],
                       [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.1},
        {"id": "g001", "name": "arm", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.08},
    ],
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [0.05, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
    ],
    "loops": [],
    "warnings": [],
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        self.server.seen.append(json.loads(raw.decode("utf-8")) if raw else None)
        data = json.dumps({"ok": True, "components": []}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def fail(msg):
    raise SystemExit("cad_link_document_smoke: FAIL: %s" % msg)


def asked(server, inst):
    """What the next request from the scene carries."""
    cad_link.poses(["c002"], instance=inst)
    return server.seen[-1]


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    inst = cad_link.Instance(os.getpid(), server.server_address[1], TOKEN,
                             "unused")
    scene = bpy.context.scene
    manifest_path = os.path.join(tempfile.gettempdir(),
                                 "cad_link_document_smoke.rig.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(MANIFEST, fh)
    try:
        # Before any send names a document, a request names none, and the
        # add-in uses its active document, as it always did.
        if "document_path" in asked(server, inst):
            fail("a request named a document before any send named one")

        result = bridge._run_job({
            "manifest": manifest_path, "source_document": LIFT,
            "steps": {"import": False, "sync_poses": False, "cleanup": False},
        })
        if not result.get("ok"):
            fail("the send failed: %s" % result.get("error"))
        if scene.get("CADLINK_document") != LIFT:
            fail("the send did not write its document on the scene: %r"
                 % scene.get("CADLINK_document"))
        if asked(server, inst).get("document_path") != LIFT:
            fail("the request did not name the document of the send: %s"
                 % server.seen[-1])

        # A pose push names its document too.
        result = bridge._run_job({
            "source_document": LIFT_B,
            "poses": {"components": [
                {"id": "c002", "sw_path": "arm-1",
                 "transform": [1, 0, 0, 0.06, 0, 1, 0, 0, 0, 0, 1, 0,
                               0, 0, 0, 1]}]},
        })
        if not result.get("ok"):
            fail("the pose push failed: %s" % result.get("error"))
        if asked(server, inst).get("document_path") != LIFT_B:
            fail("the request did not name the document of the pose push: "
                 "%s" % server.seen[-1])

        # An older add-in names no document. The scene keeps the one it has.
        result = bridge._run_job({
            "manifest": manifest_path,
            "steps": {"import": False, "sync_poses": False, "cleanup": False},
        })
        if not result.get("ok"):
            fail("the send without a document failed: %s" % result.get("error"))
        if asked(server, inst).get("document_path") != LIFT_B:
            fail("a send without a document changed the document of the "
                 "scene: %s" % server.seen[-1])
    finally:
        os.unlink(manifest_path)
        server.shutdown()
    print("cad_link_document_smoke: OK: a send and a pose push write their "
          "document on the scene, and a request back names it")


main()
