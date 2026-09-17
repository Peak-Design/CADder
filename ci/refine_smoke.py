# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the return leg: Blender asking the CAD side for finer
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

from CADder.rig import native_import, cad_link, swmesh  # noqa: E402

TOKEN = "smoke-token"
COARSE_TRIS = 1
FINE_TRIS = 4


RESEND_MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "refine.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [{"id": "c009", "sw_path": "bracket-1", "step_name": "bracket",
                    "step_occurrence_path": None,
                    "transform": [[1, 0, 0, 0.12], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}],
    "rigid_groups": [{"id": "g000", "name": "bracket", "components": ["c009"],
                      "grounded": True, "frame": None, "bbox_diag": 0.1}],
    "joints": [], "loops": [], "warnings": [],
}


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

    body = struct.pack("<III", swmesh.MAGIC, 1, 0)
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
            self._send(200, {"ok": True, "app": "Peak.Cadder"})
            return
        if self.headers.get("X-CADLink-Token") != TOKEN:
            self._send(403, {"ok": False, "error": "bad token"})
            return
        request = json.loads(body.decode("utf-8"))
        self.server.seen.append(request)
        if request.get("op") == "export":
            # The whole assembly again: the part is now 120 mm along X and
            # the manifest says so.
            mesh = os.path.join(tempfile.gettempdir(), "refine_resend.swmesh")
            write_mesh(mesh, FINE_TRIS, 0.00002)
            manifest = os.path.join(tempfile.gettempdir(), "refine_resend.rig.json")
            with open(manifest, "w", encoding="utf-8") as fh:
                json.dump(RESEND_MANIFEST, fh)
            self._send(200, {"ok": True, "mesh": mesh, "manifest": manifest})
            return
        if request.get("op") == "poses":
            # The part has been moved 50 mm along X in the CAD assembly.
            self._send(200, {"ok": True, "components": [
                {"id": "c009", "sw_path": "bracket-1", "sw_persistent_id": "abc",
                 "transform": [1, 0, 0, 0.05, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}]})
            return
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
    bpy.ops.preferences.addon_enable(module="CADder")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    # A PRIVATE registry directory for the duration. Pointing at the real
    # one would let discovery find a SolidWorks that is genuinely running
    # on this machine, and the test would then quietly measure that
    # instead, which is exactly what happened the first time.
    real_registry = cad_link._REGISTRY
    cad_link._REGISTRY = os.path.join(tempfile.gettempdir(), "cadlink-smoke-registry")
    os.makedirs(cad_link._REGISTRY, exist_ok=True)
    registry = os.path.join(cad_link._REGISTRY, "smoke.json")
    with open(registry, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "port": port, "token": TOKEN,
                   "addin_version": "smoke"}, fh)
    try:
        # 1. Discovery finds the stand-in and pings it.
        found = [i for i in cad_link.discover() if i.port == port]
        assert found, "discovery did not find the running server"

        # 2. A coarse import, then something that must survive refinement:
        #    an armature parent and a pose the part is sitting in.
        coarse = write_mesh(
            os.path.join(tempfile.gettempdir(), "refine_coarse.swmesh"),
            COARSE_TRIS, 0.002)
        objects, import_report = native_import.build(bpy.context, coarse)
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
        assert bpy.ops.cadlink.update_from_cad.poll(), "operator refused the selection"
        result = bpy.ops.cadlink.update_from_cad(quality=0.9, scope="SELECTED")
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

        # 7. A wider scope asks for more than the selection: with nothing
        # selected, the whole send is every object of this file.
        for o in bpy.context.selected_objects:
            o.select_set(False)
        result = bpy.ops.cadlink.update_from_cad(quality=0.5, scope="WHOLE")
        assert "FINISHED" in result, result
        assert server.seen[-1]["components"] == ["c009"], server.seen[-1]
        # A rebuild that said nothing about simplifying leaves the key out,
        # because a CAD application told nothing sends the geometry as it is.
        assert "simplify" not in server.seen[-1], server.seen[-1]

        # 7b. A part marked to be simplified says so on every rebuild. This
        # is what keeps a scene consistent: the setting is Blender's, and it
        # has to ride the request or the holes come quietly back.
        obj.cad_simplify.enabled = True
        obj.cad_simplify.size = 0.008
        obj.cad_simplify.curved = True
        result = bpy.ops.cadlink.update_from_cad(quality=0.5, scope="WHOLE")
        assert "FINISHED" in result, result
        asked = server.seen[-1].get("simplify")
        assert asked and asked[0]["component"] == "c009", server.seen[-1]
        assert abs(asked[0]["size_m"] - 0.008) < 1e-6, asked
        assert asked[0]["curved"] is True, asked
        obj.cad_simplify.enabled = False

        # 7c. The UV panel works on a part from the live link, not only on
        # one from a STEP file. Box Project reads the mesh and nothing else.
        # The other modes need the CAD data, and for this part the CAD data
        # is the CAD application, so the geometry is asked for again.
        prg = bpy.context.scene.stepper
        for o in bpy.context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        prg.uv_mode = "BOX"
        prg.box_uv_scale = 0.05
        assert bpy.ops.stepper.reapply_uv.poll(), "the UV operator refused a part"
        assert "FINISHED" in bpy.ops.stepper.reapply_uv(), "box project failed"
        assert obj.data.uv_layers.get("UVMap") is not None, "no UV map"

        # The modes that are not Box Project need the CAD data, and for a
        # part from the live link the CAD data is the CAD application. What
        # this fixture can show is that it is ASKED: the .swmesh written
        # here carries no surface coordinates, so what comes back is not a
        # UV map to check.
        before = len(server.seen)
        prg.uv_mode = "SURFACE"
        assert "FINISHED" in bpy.ops.stepper.reapply_uv(), "surface UVs failed"
        assert len(server.seen) > before, \
            "a mode that needs the CAD data asked nobody for it"
        assert server.seen[-1]["op"] == "retessellate", server.seen[-1]

        points = len(obj.data.vertices)
        prg.uv_mode = "SMART"
        assert "FINISHED" in bpy.ops.stepper.reapply_uv(), "smart UVs failed"
        assert len(obj.data.vertices) <= points, "the weld added points"

        # 7d. A collection is a scope of its own, so a whole subassembly can
        # be given one treatment without picking its parts out.
        group = bpy.data.collections.new("uv group")
        bpy.context.scene.collection.children.link(group)
        group.objects.link(obj)
        for o in bpy.context.selected_objects:
            o.select_set(False)
        prg.uv_mode = "BOX"
        bpy.context.view_layer.active_layer_collection = (
            bpy.context.view_layer.layer_collection.children[group.name])
        assert "FINISHED" in bpy.ops.stepper.reapply_uv(scope="COLLECTION"), \
            "the collection scope found nothing"
        bpy.context.view_layer.active_layer_collection = (
            bpy.context.view_layer.layer_collection)
        group.objects.unlink(obj)
        bpy.data.collections.remove(group)

        # 7e. The weld itself, which is what lets Smart work on a part from
        # the live link at all. A mesh from the CAD application carries
        # every CAD face's points twice, once for each face that meets
        # there, so no two faces share an edge and anything that walks from
        # face to face finds nothing.
        from CADder import tools as tools_mod
        me = bpy.data.meshes.new("split")
        me.from_pydata(
            [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),      # one face
             (1, 0, 0), (2, 0, 0), (2, 1, 0), (1, 1, 0)],     # and the next
            [], [(0, 1, 2, 3), (4, 5, 6, 7)])
        layer = me.uv_layers.new(name="UVMap")
        for i, uv in enumerate([(0, 0), (1, 0), (1, 1), (0, 1),
                                (5, 0), (6, 0), (6, 1), (5, 1)]):
            layer.data[i].uv = uv
        me.update()
        welded = tools_mod.weld(me)
        assert welded, "the weld found nothing to join"
        assert len(me.vertices) == 6, \
            "the weld left %d points, not 6" % len(me.vertices)
        shared = [e for e in me.edges if len(
            [p for p in me.polygons if tuple(sorted(e.vertices)) in
             [tuple(sorted(k)) for k in p.edge_keys]]) == 2]
        assert shared, "the two faces still do not share an edge"
        assert all(e.use_seam and e.use_edge_sharp for e in shared), \
            "the CAD face boundary was not marked"
        bpy.data.meshes.remove(me)

        # 8. Poses: the CAD side says the part has moved, and the object
        #    follows. The manifest keeps the new transform, so a rig built
        #    from it afterwards rests where the part now is.
        from CADder.rig import manifest as man_mod, ui as rig_ui
        data = {
            "manifest_version": "1.0.0",
            "generator": {"name": "Peak.Cadder", "version": "smoke"},
            "units": {"length": "meter", "angle": "radian"},
            "frame": {"handedness": "right", "up_axis": "Z",
                      "transform_convention": "row_major_4x4_global"},
            "step_export": {"file": "refine.step", "ap": "AP214",
                            "sha1": None, "occurrence_matching": None},
            "components": [{"id": "c009", "sw_path": "bracket-1", "step_name": "bracket",
                            "step_occurrence_path": None,
                            "transform": [[1, 0, 0, 0], [0, 1, 0, 0],
                                          [0, 0, 1, 0], [0, 0, 0, 1]]}],
            "rigid_groups": [{"id": "g000", "name": "bracket", "components": ["c009"],
                              "grounded": True, "frame": None, "bbox_diag": 0.1}],
            "joints": [], "loops": [], "warnings": [],
        }
        rig_ui._STATE["manifest"] = man_mod.parse(data)
        rig_ui._STATE["match_report"] = import_report
        # Pose sync moves objects, so the part is off its bone for this
        # part of the test. With a rig in the scene the operator rebuilds
        # it first, which releases the geometry the same way.
        obj.parent = None
        bpy.context.view_layer.update()
        before = obj.matrix_world.translation.copy()
        result = bpy.ops.cadlink.update_from_cad(scope="WHOLE", what="POSES")
        assert "FINISHED" in result, result
        assert server.seen[-1]["op"] == "poses", server.seen[-1]
        bpy.context.view_layer.update()
        now = obj.matrix_world.translation
        assert (now - before).length > 1e-6, "the part did not move at all"
        assert abs(now.x - 0.05) < 1e-6 and abs(now.y) < 1e-6 and abs(now.z) < 1e-6, \
            "the part is not on the CAD pose: %s" % list(now)
        rows = rig_ui._STATE["manifest"].components[0].transform
        assert abs(rows[0][3] - 0.05) < 1e-9, "the manifest kept the old pose: %s" % rows

        # 9. Everything: the CAD side exports the assembly again, and the
        #    scene is rebuilt from it. This is what catches parts added or
        #    removed and mates changed, which no geometry update can see.
        objects_before = len([o for o in bpy.data.objects if o.get("RIG_component_id")])
        result = bpy.ops.cadlink.update_from_cad(scope="WHOLE", what="EVERYTHING")
        assert "FINISHED" in result, result
        assert server.seen[-1]["op"] == "export", server.seen[-1]
        bpy.context.view_layer.update()
        rebuilt = [o for o in bpy.data.objects if o.get("RIG_component_id")]
        assert len(rebuilt) == objects_before, \
            "the re-send left %d objects, not %d" % (len(rebuilt), objects_before)
        assert abs(rebuilt[0].matrix_world.translation.x - 0.12) < 1e-6, \
            "the re-sent part is not where the new manifest says: %s" % list(
                rebuilt[0].matrix_world.translation)
        rows = rig_ui._STATE["manifest"].components[0].transform
        assert abs(rows[0][3] - 0.12) < 1e-9, "the new manifest was not loaded: %s" % rows
        arms = [o for o in bpy.data.objects if o.type == "ARMATURE" and o.get("RIG_rig")]
        assert len(arms) == 1, "the re-send did not rebuild exactly one rig: %s" % arms

        # 10. The same round trip with the parts sent as collection
        #     instances. The object that carries the component id is then
        #     an empty, and the geometry sits on the prototype inside the
        #     collection it instances. Assigning the new mesh to the empty
        #     raised "Object.data expected a Image type" (Oscar,
        #     2026-09-16), so this holds that route open.
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="CADder")
        rig_ui._STATE["manifest"] = None
        coarse = write_mesh(
            os.path.join(tempfile.gettempdir(), "refine_coarse_ci.swmesh"),
            COARSE_TRIS, 0.002)
        objects, _ = native_import.build(
            bpy.context, coarse, hierarchy="COLLECTION_INSTANCES")
        empty = objects[0]
        assert empty.type == "EMPTY" and empty.instance_collection is not None
        holder = next(o for o in empty.instance_collection.all_objects
                      if o.type == "MESH")
        assert len(holder.data.polygons) == COARSE_TRIS
        before_world = empty.matrix_world.copy()

        for o in bpy.context.selected_objects:
            o.select_set(False)
        empty.select_set(True)
        bpy.context.view_layer.objects.active = empty
        result = bpy.ops.cadlink.update_from_cad(quality=0.9, scope="SELECTED")
        assert "FINISHED" in result, result
        holder = next(o for o in empty.instance_collection.all_objects
                      if o.type == "MESH")
        assert len(holder.data.polygons) == FINE_TRIS,             "the prototype still holds the coarse mesh"
        assert (empty.matrix_world.translation
                - before_world.translation).length < 1e-9
        assert empty["SWMESH_tolerance_m"] == 0.00002

        print("refine_smoke: OK: %d -> %d triangles, object kept its bone "
              "parent and world pose, a pose update moved it onto the new CAD "
              "transform, and a whole-assembly re-send rebuilt the scene from a "
              "fresh export, and a part sent as a collection instance "
              "refines through its prototype" % (COARSE_TRIS, FINE_TRIS))
    finally:
        server.shutdown()
        cad_link._REGISTRY = real_registry
        try:
            os.unlink(registry)
        except OSError:
            pass


main()
