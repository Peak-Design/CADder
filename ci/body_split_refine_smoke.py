# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for asking the CAD side for a body-split part again.

    blender -b --factory-startup -P ci/body_split_refine_smoke.py

With "one object per solid body" a part arrives as one object per body,
all carrying the part's component id. Asking for it again has to bring it
back in the SAME pieces. It did not: the request never said which way the
part came in, the CAD side answered with the whole part as one piece, and
the consumer put that whole part on every one of its body objects, so the
part was drawn over itself once per body (Oscar, 2026-09-16).

SolidWorks is stood in for by a server that answers the way the add-in
now does: split when the request asks for split.
"""

import json
import os
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import cad_link, native_import, swmesh  # noqa: E402

TOKEN = "smoke-token"
COARSE = 1          # triangles per body, coarse
FINE = 4            # triangles per body, refined


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def _fan(triangles, offset):
    n = triangles + 2
    verts = []
    for i in range(n):
        verts.extend([float(i) + offset, float(i * i % 3), 0.0])
    tris = []
    for i in range(triangles):
        tris.extend([0, i + 1, i + 2])
    return n, verts, tris


def write_mesh(path, pieces, tolerance):
    """A version-3 file. `pieces` is (definition id, name, path, triangles),
    so one call writes either the part in its bodies or the part whole."""
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", tolerance)
    body += struct.pack("<IIII", 1, len(pieces), len(pieces), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)

    for index, (did, name, _where, triangles) in enumerate(pieces):
        n, verts, tris = _fan(triangles, index)
        body += struct.pack("<i", did) + _text(name)
        body += struct.pack("<II", n, triangles)
        body += struct.pack("<%df" % len(verts), *verts)
        body += struct.pack("<%di" % len(tris), *tris)
        body += struct.pack("<%di" % triangles, *([0] * triangles))

    rows = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    for did, name, where, _triangles in pieces:
        body += (struct.pack("<i", did) + _text("c009") + _text(name)
                 + _text(where) + struct.pack("<16d", *rows)
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


SPLIT = [(1, "bracket.body001", "bracket-1.body001", FINE),
         (2, "bracket.body002", "bracket-1.body002", FINE)]
WHOLE = [(1, "bracket", "bracket-1", FINE)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path.rstrip("/") == "/ping":
            self._send(200, {"ok": True, "app": "Peak.Cadder"})
            return
        request = json.loads(raw.decode("utf-8"))
        self.server.seen.append(request)
        if request.get("op") != "retessellate":
            self._send(200, {"ok": False, "error": "unknown op"})
            return
        # What the add-in does: the pieces the request asks for.
        pieces = SPLIT if request.get("separate_solids") else WHOLE
        path = os.path.join(tempfile.gettempdir(), "body_split_fine.swmesh")
        write_mesh(path, pieces, 0.00002)
        self._send(200, {"ok": True, "mesh": path, "definitions": len(pieces),
                         "instances": len(pieces), "triangles": FINE * len(pieces),
                         "tolerance_m": 0.00002})


def _check(cond, msg):
    if not cond:
        raise SystemExit("body_split_refine_smoke: FAIL: " + msg)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    # This counts triangles, so the quad pass stays off. It is
    # tested in refine_smoke.
    bpy.context.scene.stepper.tris_to_quads = False

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()

    real_registry = cad_link._REGISTRY
    cad_link._REGISTRY = os.path.join(tempfile.gettempdir(), "cadlink-split-registry")
    os.makedirs(cad_link._REGISTRY, exist_ok=True)
    with open(os.path.join(cad_link._REGISTRY, "smoke.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "port": server.server_address[1],
                   "token": TOKEN, "addin_version": "smoke"}, fh)
    try:
        coarse = write_mesh(
            os.path.join(tempfile.gettempdir(), "body_split_coarse.swmesh"),
            [(d, n, w, COARSE) for d, n, w, _t in SPLIT], 0.002)
        objects, _report = native_import.build(bpy.context, coarse)
        _check(len(objects) == 2, "the part did not arrive in two bodies")
        first, second = objects
        _check(first.data is not second.data, "the two bodies share one mesh")
        _check(all(len(o.data.polygons) == COARSE for o in objects),
               "the coarse bodies are not coarse")

        for o in bpy.context.selected_objects:
            o.select_set(False)
        for o in objects:
            o.select_set(True)
        bpy.context.view_layer.objects.active = first
        result = bpy.ops.cadlink.update_from_cad(quality=0.9,
                                                 what="GEOMETRY")
        _check("FINISHED" in result, "the update failed: %s" % (result,))

        asked = server.seen[-1]
        _check(asked.get("separate_solids") is True,
               "the request did not ask for the part in its bodies: %s" % asked)

        # Each body took its OWN geometry: two meshes, one each, at the
        # fine count. The bug put the whole part on both.
        _check(first.data is not second.data,
               "the two bodies ended up sharing one mesh")
        for obj in (first, second):
            _check(len(obj.data.polygons) == FINE,
                   "%s has %d triangle(s), want %d"
                   % (obj.name, len(obj.data.polygons), FINE))

        # And geometry that cannot say which body it is never lands on all
        # of them: an older add-in answering with the whole part leaves the
        # scene as it was rather than drawing it over itself twice.
        whole = write_mesh(
            os.path.join(tempfile.gettempdir(), "body_split_whole.swmesh"),
            [(9, "bracket", "bracket-1", 16)], 0.00001)
        before = [o.data.name for o in (first, second)]
        changed = native_import.refine(bpy.context, whole)
        _check(not changed, "the whole part was swapped in: %s" % changed)
        _check([o.data.name for o in (first, second)] == before,
               "the bodies lost their own geometry")
    finally:
        cad_link._REGISTRY = real_registry
        server.shutdown()

    print("body_split_refine_smoke: OK: a body-split part is asked for in its "
          "bodies, each body takes its own geometry, and a whole-part answer "
          "is left out rather than drawn over every body")


main()
