# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the return leg: Blender asking SolidWorks for finer
geometry and swapping it in.

SolidWorks is stood in for by a small HTTP server that speaks the same
protocol (discovery file, token header, a .swmesh path in the reply), so
the whole Blender half is exercised for real: the client, the operator,
and the in-place mesh swap. The one thing it cannot test is the
tessellation itself, which needs SolidWorks.

What the swap has to preserve is the point of the feature: the object, its
transform, and its bone parenting all survive, because refining a part
must not cost the pose it is in.

Run:  blender -b --factory-startup -P refine_smoke.py
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

from STEPper_NEXT.rig import native_import, sw_link, swmesh  # noqa: E402

TOKEN = "smoke-token"
COARSE_TRIS = 1
FINE_TRIS = 4


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_mesh(path, triangles, tolerance):
    """A fan of `triangles` triangles: the count is how the test tells the
    coarse mesh from the refined one."""
    n = triangles + 2
    verts = []
    for i in range(n):
        verts.extend([float(i), float(i * i % 3), 0.0])
    tris = []
    for i in range(triangles):
        tris.extend([0, i + 1, i + 2])

    body = struct.pack("<III", swmesh.MAGIC, swmesh.VERSION, 0)
    body += struct.pack("<d", tolerance)
    body += struct.pack("<III", 1, 1, 1)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0) + _text("")
    body += struct.pack("<i", 42) + _text("bracket")
    body += struct.pack("<II", n, triangles)
    body += struct.pack("<%df" % len(verts), *verts)
    body += struct.pack("<%di" % len(tris), *tris)
    body += struct.pack("<%di" % triangles, *([0] * triangles))
    rows = [1, 0, 0, 1.25, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    body += struct.pack("<i", 42) + _text("c009") + _text("bracket-1") \
        + struct.pack("<16d", *rows)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


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
        body = self.rfile.read(length) if length else b""
        if self.path.rstrip("/") == "/ping":
            self._send(200, {"ok": True, "app": "Peak.SwToBlender"})
            return
        if self.headers.get("X-SWTB-Token") != TOKEN:
            self._send(403, {"ok": False, "error": "bad token"})
            return
        request = json.loads(body.decode("utf-8"))
        self.server.seen.append(request)
        if request.get("op") != "retessellate":
            self._send(200, {"ok": False, "error": "unknown op"})
            return
        path = os.path.join(tempfile.gettempdir(), "refine_fine.swmesh")
        write_mesh(path, FINE_TRIS, 0.00002)
        self._send(200, {"ok": True, "mesh": path, "definitions": 1,
                         "instances": 1, "triangles": FINE_TRIS,
                         "tolerance_m": 0.00002})


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # The operator is the thing under test here, so the add-on has to be
    # registered rather than just imported.
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    # A PRIVATE registry directory for the duration. Pointing at the real
    # one would let discovery find a SolidWorks that is genuinely running
    # on this machine, and the test would then quietly measure that
    # instead, which is exactly what happened the first time.
    real_registry = sw_link._REGISTRY
    sw_link._REGISTRY = os.path.join(tempfile.gettempdir(), "swtb-smoke-registry")
    os.makedirs(sw_link._REGISTRY, exist_ok=True)
    registry = os.path.join(sw_link._REGISTRY, "smoke.json")
    with open(registry, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "port": port, "token": TOKEN,
                   "addin_version": "smoke"}, fh)
    try:
        # 1. Discovery finds the stand-in and pings it.
        found = [i for i in sw_link.discover() if i.port == port]
        assert found, "discovery did not find the running server"

        # 2. A coarse import, then something that must survive refinement:
        #    an armature parent and a pose the part is sitting in.
        coarse = write_mesh(
            os.path.join(tempfile.gettempdir(), "refine_coarse.swmesh"),
            COARSE_TRIS, 0.002)
        objects, _ = native_import.build(bpy.context, coarse)
        obj = objects[0]
        assert len(obj.data.polygons) == COARSE_TRIS

        arm_data = bpy.data.armatures.new("rig")
        arm = bpy.data.objects.new("rig", arm_data)
        bpy.context.scene.collection.objects.link(arm)
        bpy.context.view_layer.objects.active = arm
        bpy.ops.object.mode_set(mode="EDIT")
        eb = arm_data.edit_bones.new("part")
        eb.head = (0.0, 0.0, 0.0)
        eb.tail = (0.0, 0.1, 0.0)
        bpy.ops.object.mode_set(mode="OBJECT")
        obj.parent = arm
        obj.parent_type = "BONE"
        obj.parent_bone = "part"
        # Bone parenting is not applied to matrix_world until the depsgraph
        # runs, so capturing it first would compare against a stale pose.
        bpy.context.view_layer.update()
        before_world = obj.matrix_world.copy()
        old_mesh_name = obj.data.name
        meshes_before = len(bpy.data.meshes)

        # 3. The round trip, through the real operator.
        for o in bpy.context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        assert bpy.ops.swtb.refine_selected.poll(), "operator refused the selection"
        result = bpy.ops.swtb.refine_selected(quality=0.9)
        assert "FINISHED" in result, result

        # 4. The request said what it should have.
        assert server.seen, "the server was never asked"
        asked = server.seen[-1]
        assert asked["op"] == "retessellate"
        assert asked["components"] == ["c009"], asked
        # Blender's FloatProperty is float32, so 0.9 arrives as 0.89999998.
        assert abs(asked["quality"] - 0.9) < 1e-6, asked["quality"]

        # 5. Finer geometry, SAME object, same place, still on its bone.
        assert len(obj.data.polygons) == FINE_TRIS, len(obj.data.polygons)
        assert obj.parent is arm and obj.parent_bone == "part"
        assert (obj.matrix_world.translation - before_world.translation).length < 1e-9
        assert obj["SWMESH_tolerance_m"] == 0.00002

        # 6. The mesh it replaced is gone, not orphaned in the file.
        assert bpy.data.meshes.get(old_mesh_name) is None, \
            "the coarse mesh was left behind"
        assert len(bpy.data.meshes) == meshes_before

        print("refine_smoke: OK: %d -> %d triangles, object kept its bone "
              "parent and world pose" % (COARSE_TRIS, FINE_TRIS))
    finally:
        server.shutdown()
        sw_link._REGISTRY = real_registry
        try:
            os.unlink(registry)
        except OSError:
            pass


main()
