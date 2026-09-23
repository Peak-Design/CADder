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
from CADder import quality as quality_mod  # noqa: E402

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
    coarse mesh from the refined one.

    Version 3 of the format, because that is the one that carries the
    occurrence PATH, and the path is what an update pairs the two
    assemblies up by. A component id does not do it: every part of a rigid
    subassembly travels under the subassembly's id, so one id can name a
    thousand placements (Conveyor12k-A00, Oscar, 2026-09-17).
    """
    n = triangles + 2
    verts = []
    for i in range(n):
        verts.extend([float(i), float(i * i % 3), 0.0])
    tris = []
    for i in range(triangles):
        tris.extend([0, i + 1, i + 2])

    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", tolerance)
    body += struct.pack("<IIII", 1, 1, 1, 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0) + _text("")
    body += struct.pack("<I", 0)              # no appearance JSON
    body += struct.pack("<i", 42) + _text("bracket")
    body += struct.pack("<II", n, triangles)
    body += struct.pack("<%df" % len(verts), *verts)
    body += struct.pack("<%di" % len(tris), *tris)
    body += struct.pack("<%di" % triangles, *([0] * triangles))
    rows = [1, 0, 0, 1.25, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    body += struct.pack("<i", 42) + _text("c009") + _text("bracket") \
        + _text("bracket-1") + struct.pack("<16d", *rows) \
        + struct.pack("<B", 0)                # no local transform
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


def rebuild(preset):
    """Rebuild from CAD at a Mesh Quality preset: the operator reads the
    quality from the scene."""
    bpy.context.scene.stepper.quality_preset = preset
    return bpy.ops.cadlink.update_from_cad()


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # The operator is the thing under test here, so the add-on has to be
    # registered rather than just imported.
    bpy.ops.preferences.addon_enable(module="CADder")
    # What comes back is counted in triangles here, so the quad pass is
    # off for that. It has a section of its own at the end.
    bpy.context.scene.stepper.tris_to_quads = False

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
        result = rebuild("FINE")
        assert "FINISHED" in result, result

        # 4. The request said what it should have.
        assert server.seen, "the server was never asked"
        asked = server.seen[-1]
        assert asked["op"] == "retessellate"
        assert asked["components"] == ["c009"], asked
        # The quality is the scene's Mesh Quality: its preset goes as the
        # dial an older add-in reads, and as the distance a newer one cuts to.
        assert asked["quality"] == quality_mod.DIAL["FINE"], asked["quality"]
        assert abs(asked["chord_m"] - quality_mod.PRESETS["FINE"][0]) < 1e-9, asked
        # And the PLACEMENT, not only the component id. Every part of a
        # rigid subassembly carries the subassembly's id, so the id alone
        # asks for every part of the branch (Oscar, 2026-09-17).
        assert asked.get("paths") == ["bracket-1"], asked.get("paths")

        # 5. Finer geometry, SAME object, same place, still on its bone.
        assert len(obj.data.polygons) == FINE_TRIS, len(obj.data.polygons)
        assert obj.parent is arm and obj.parent_bone == "part"
        assert (obj.matrix_world.translation - before_world.translation).length < 1e-9
        assert obj["SWMESH_tolerance_m"] == 0.00002

        # 6. The mesh it replaced is gone, not orphaned in the file.
        assert bpy.data.meshes.get(old_mesh_name) is None, \
            "the coarse mesh was left behind"
        assert len(bpy.data.meshes) == meshes_before

        # 7. With nothing selected the scope is the collection that is
        # active in the outliner, which at the root is the whole scene.
        for o in bpy.context.selected_objects:
            o.select_set(False)
        result = rebuild("BALANCED")
        assert "FINISHED" in result, result
        assert server.seen[-1]["components"] == ["c009"], server.seen[-1]
        # A rebuild that said nothing about defeaturing leaves the key out,
        # because a CAD application told nothing sends the geometry as it is.
        assert "defeature" not in server.seen[-1], server.seen[-1]

        # 7b. A part marked to be defeatured says so on every rebuild. This
        # is what keeps a scene consistent: the setting is Blender's, and it
        # has to ride the request or the holes come quietly back.
        obj.cad_defeature.enabled = True
        obj.cad_defeature.size = 0.008
        obj.cad_defeature.curved = True
        result = rebuild("BALANCED")
        assert "FINISHED" in result, result
        asked = server.seen[-1].get("defeature")
        assert asked and asked[0]["component"] == "c009", server.seen[-1]
        assert abs(asked[0]["size_m"] - 0.008) < 1e-6, asked
        assert asked[0]["curved"] is True, asked
        obj.cad_defeature.enabled = False

        # 7f. Two placements of one part are ONE mesh in Blender. Asking for
        # one of them defeatured and not the other would give them two
        # meshes, which is the link gone, so the link is the unit: the
        # request names both, and the button turns the switch on for both.
        twin = bpy.data.objects.new("bracket-2", obj.data)
        bpy.context.scene.collection.objects.link(twin)
        twin["RIG_component_id"] = "c010"
        for o in bpy.context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        assert "FINISHED" in bpy.ops.stepper.apply_defeature(), \
            "the button refused a part from the live link"
        asked = server.seen[-1]
        assert asked["op"] == "retessellate", asked
        assert set(asked["components"]) == {"c009", "c010"}, asked
        assert {row["component"] for row in asked.get("defeature") or []} \
            == {"c009", "c010"}, asked
        assert obj.cad_defeature.enabled and twin.cad_defeature.enabled, \
            "the button left one of the two switches off"
        obj.cad_defeature.enabled = False
        bpy.data.objects.remove(twin)

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

        # 7d. With nothing selected the collection that is active in the
        # outliner is the scope, so a whole subassembly is given one
        # treatment without picking its parts out.
        group = bpy.data.collections.new("uv group")
        bpy.context.scene.collection.children.link(group)
        group.objects.link(obj)
        for o in bpy.context.selected_objects:
            o.select_set(False)
        prg.uv_mode = "BOX"
        bpy.context.view_layer.active_layer_collection = (
            bpy.context.view_layer.layer_collection.children[group.name])
        assert "FINISHED" in bpy.ops.stepper.reapply_uv(), \
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
        result = bpy.ops.cadlink.update_from_cad(what="POSES")
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
        result = bpy.ops.cadlink.update_from_cad(what="EVERYTHING")
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

        # 9b. Refresh: the same question of the CAD application, and the
        #     scene is brought up to date rather than built again. A part
        #     that is still there keeps its object, and with it the work
        #     done in Blender on that object.
        part = rebuilt[0]
        part.name = "bracket I renamed"
        part["mine"] = 42
        part.modifiers.new("Bevel", "BEVEL")
        result = bpy.ops.cadlink.update_from_cad(what="REFRESH")
        assert "FINISHED" in result, result
        assert server.seen[-1]["op"] == "export", server.seen[-1]
        bpy.context.view_layer.update()
        kept = bpy.data.objects.get("bracket I renamed")
        assert kept is not None, "the refresh replaced the object"
        assert kept.get("mine") == 42, "the refresh dropped what was on it"
        assert [m.type for m in kept.modifiers] == ["BEVEL"], \
            "the refresh dropped the modifier: %s" % [m.type
                                                      for m in kept.modifiers]
        arms = [o for o in bpy.data.objects
                if o.type == "ARMATURE" and o.get("RIG_rig")]
        assert len(arms) == 1, "the refresh left %d rig(s)" % len(arms)

        # 10. The same round trip with the parts sent as collection
        #     instances. The object that carries the component id is then
        #     an empty, and the geometry sits on the prototype inside the
        #     collection it instances. Assigning the new mesh to the empty
        #     raised "Object.data expected a Image type" (Oscar,
        #     2026-09-16), so this holds that route open.
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="CADder")
        bpy.context.scene.stepper.tris_to_quads = False
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
        result = rebuild("FINE")
        assert "FINISHED" in result, result
        holder = next(o for o in empty.instance_collection.all_objects
                      if o.type == "MESH")
        assert len(holder.data.polygons) == FINE_TRIS,             "the prototype still holds the coarse mesh"
        assert (empty.matrix_world.translation
                - before_world.translation).length < 1e-9
        assert empty["SWMESH_tolerance_m"] == 0.00002

        # 11. Triangles to quads. The setting is the scene's, so a part
        #     from the live link comes back the same way a part from a STEP
        #     file does: the four triangles pair into two quads.
        bpy.context.scene.stepper.tris_to_quads = True
        fine = write_mesh(
            os.path.join(tempfile.gettempdir(), "refine_fine_ci.swmesh"),
            FINE_TRIS, 0.00002)
        native_import.refine(bpy.context, fine)
        holder = next(o for o in empty.instance_collection.all_objects
                      if o.type == "MESH")
        sides = [len(f.vertices) for f in holder.data.polygons]
        assert sides and all(n == 4 for n in sides), sides
        assert len(sides) == FINE_TRIS // 2, sides
        bpy.context.scene.stepper.tris_to_quads = False

        print("refine_smoke: OK: %d -> %d triangles, object kept its bone "
              "parent and world pose, a pose update moved it onto the new CAD "
              "transform, and a whole-assembly re-send rebuilt the scene from a "
              "fresh export, a Refresh brought the same export in without "
              "losing the object, and a part sent as a collection instance "
              "refines through its prototype" % (COARSE_TRIS, FINE_TRIS))
    finally:
        server.shutdown()
        cad_link._REGISTRY = real_registry
        try:
            os.unlink(registry)
        except OSError:
            pass


main()
