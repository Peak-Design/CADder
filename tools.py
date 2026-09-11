# Post-import tools for STEPper NEXT: state-preserving Regenerate,
# Prune/Restore hierarchy and mesh cleanup.

import json
from collections import defaultdict
import math
from math import radians

import bmesh
import bpy
import numpy as np

from . import uv as uv_mod


def _scale_mesh_verts(me, factor):
    vert_count = len(me.vertices)
    if vert_count == 0 or factor in (0.0, 1.0):
        return
    verts = np.empty(vert_count * 3, dtype=np.float32)
    me.vertices.foreach_get("co", verts)
    verts *= factor
    me.vertices.foreach_set("co", verts)
    me.update()


class STEPPER_OT_regenerate(bpy.types.Operator):
    """Tessellate the selected objects again from their source STEP file. The
    addon replaces only the mesh data. Transforms, parenting, modifiers,
    material database assignments, animation and custom properties all survive."""
    bl_idname = "stepper.regenerate"
    bl_label = "Regenerate Selected from STEP"
    bl_options = {"REGISTER", "UNDO"}

    use_scene_settings: bpy.props.BoolProperty(
        name="Use STEPper Panel Resolution",
        description="Use the resolution values from the STEPper panel. Turn "
                    "this off to use the original import settings of each "
                    "object",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and any(
            o.type == "MESH" and "STEP_file" in o and "STEP_tag" in o
            for o in context.selected_objects)

    def execute(self, context):
        from . import main as m
        from . import formats

        # Unique mesh datablocks (multi-user meshes regenerate once and all
        # users update automatically since geometry is replaced in place).
        targets = {}
        for obj in context.selected_objects:
            if obj.type == "MESH" and "STEP_file" in obj and "STEP_tag" in obj:
                targets.setdefault(obj.data, obj)
        if not targets:
            self.report({"WARNING"}, "No STEP objects selected")
            return {"CANCELLED"}

        prefs = m._get_addon_prefs()
        hacks = {"skip_solids"} if prefs.hack_skip_zero_solids else set()

        scene_lin = context.scene.stepper.lin_deflection
        scene_ang = context.scene.stepper.ang_deflection
        if prefs.simpler_parameters:
            scene_ang, scene_lin = m.calculate_detail_level(
                context.scene.stepper.detail_level)

        by_file = defaultdict(list)
        for me, obj in targets.items():
            by_file[obj["STEP_file"]].append(obj)

        wm = context.window_manager
        wm.progress_begin(0, len(targets))
        done = 0
        failed = []
        unwrap_objs = []
        smart_objs = []
        smart_pack = ("NONE", 4)
        all_objs = []
        for filepath, objs in by_file.items():
            reader = m._cache_get(filepath)
            if reader is None:
                self.report({"INFO"},
                            f"Re-parsing {filepath} (not in cache)")
                try:
                    reader = formats.make_reader(filepath)
                    m._cache_put(filepath, reader)
                except Exception as e:
                    failed.extend(o.name for o in objs)
                    print(f"Regenerate: cannot read {filepath}: {e}")
                    continue

            tag_to_node = {}
            for shp, node_index in reader.tree.get_shapes():
                if shp is not None:
                    tag_to_node.setdefault(
                        reader.tree.nodes[node_index].tag, (shp, node_index))

            # Force re-tessellation at the requested resolution
            reader._pre_tessellated = False

            for obj in objs:
                tag = obj["STEP_tag"]
                if tag not in tag_to_node:
                    failed.append(obj.name)
                    continue
                shp, node_index = tag_to_node[tag]
                node = reader.tree.nodes[node_index]

                stored = {}
                try:
                    stored = json.loads(obj.get("STEP_import_settings", "{}"))
                except Exception:
                    pass

                if self.use_scene_settings:
                    lin_def, ang_def = scene_lin, scene_ang
                else:
                    lin_def = stored.get("lin_deflection", scene_lin)
                    ang_def = stored.get("ang_deflection", scene_ang)

                # Restore per-import UV options for the apply path.
                # Older imports stored uv_surface/uv_unwrap/uv_box booleans
                # instead of uv_mode, so map them across.
                uv_mode = stored.get("uv_mode")
                if uv_mode is None:
                    if stored.get("uv_box"):
                        uv_mode = "BOX"
                    elif stored.get("uv_unwrap"):
                        uv_mode = "UNWRAP"
                    elif stored.get("uv_surface", True):
                        uv_mode = "SURFACE"
                    else:
                        uv_mode = "NONE"
                m._uv_options["mode"] = uv_mode
                m._uv_options["surface"] = uv_mode in ("SURFACE", "UNWRAP")
                m._uv_options["unwrap"] = uv_mode == "UNWRAP"
                m._uv_options["box"] = uv_mode == "BOX"
                m._uv_options["normalize"] = stored.get("uv_normalize", True)
                # Records written before the dropdown carry a boolean.
                m._uv_options["closed_seams"] = stored.get(
                    "uv_closed_seams",
                    "SPLIT" if stored.get("uv_split_closed", True) else "NONE")
                m._uv_options["merge_tangent"] = stored.get(
                    "uv_merge_tangent", "NONE")
                m._uv_options["box_scale"] = stored.get("box_uv_scale", 1.0)
                m._uv_options["unit_scale"] = stored.get(
                    "unit_scale", obj.get("STEP_applied_scale", 0.0) or 1.0)
                reader.uv_world_scale = (
                    None if m._uv_options["normalize"]
                    else m._uv_options["unit_scale"])

                try:
                    mesh, colors, mat_names, norms, uvs = m.precompute_mesh_data(
                        reader, shp, lin_def, ang_def, hacks,
                        part_name=obj.get("STEP_name", ""),
                        fallback_color=node.color_override)
                    m.apply_mesh_to_blender(
                        obj, mesh, colors, mat_names, norms, uvs,
                        build_materials=prefs.build_materials)
                except Exception as e:
                    failed.append(obj.name)
                    print(f"Regenerate failed for {obj.name}: {e}")
                    continue

                applied_scale = obj.get("STEP_applied_scale", 0.0)
                if applied_scale and applied_scale != 1.0:
                    _scale_mesh_verts(obj.data, applied_scale)

                if obj.data.materials:
                    obj["STEP_materials"] = json.dumps(
                        [mt.name if mt else "" for mt in obj.data.materials])
                obj.display_type = "TEXTURED"
                # Re-apply the engineering material if the original import
                # used it (metadata persists as object custom properties)
                if stored.get("eng_materials") and obj.get("STEP_material"):
                    m._assign_engineering_material(obj, {
                        "name": obj.get("STEP_material"),
                        "description": obj.get("STEP_material_desc", ""),
                        "density": obj.get("STEP_material_density", 0.0),
                    })
                if m._uv_options["unwrap"]:
                    unwrap_objs.append(obj)
                # Merge tangent, as the object was imported. Smart runs
                # before the unwrap, All after it on CAD Surface parts.
                merge = stored.get("uv_merge_tangent", "NONE")
                if uv_mode in ("SURFACE", "UNWRAP") and merge == "SMART":
                    smart_objs.append(obj)
                    smart_pack = (stored.get("uv_pack", "NONE"),
                                  stored.get("uv_pack_tiles", 4))
                elif uv_mode == "SURFACE" and merge == "ALL":
                    all_objs.append((obj, stored.get("uv_unwrap_method",
                                                     "CONFORMAL")))
                done += 1
                wm.progress_update(done)

        if smart_objs:
            m._smart_merge_objects(smart_objs, *smart_pack)

        if unwrap_objs:
            # Regenerated meshes are already scaled to scene units, so
            # real-world UV mode needs no extra unit conversion
            m._unwrap_uv_objects(
                unwrap_objs,
                world_scale=(None if m._uv_options.get("normalize", True)
                             else 1.0))

        for obj, method in all_objs:
            m._flatten_merged_objects([obj], method=method)

        wm.progress_end()

        # Re-apply active material database
        db_path = m._get_active_matdb_path()
        if db_path:
            mappings = m._ensure_matdb_materials(db_path)
            m._apply_matdb_to_objects(list(targets.values()), mappings)

        if failed:
            self.report({"WARNING"},
                        f"Regenerated {done}; failed: {', '.join(failed[:5])}")
        else:
            self.report({"INFO"}, f"Regenerated {done} mesh(es)")
        return {"FINISHED"}


class STEPPER_OT_prune_hierarchy(bpy.types.Operator):
    """Collapse chains of single child empties in the imported hierarchy. This
    works on every object imported from the same files as the selection. The
    addon records the removed levels on the children that stay. Restore Pruned
    Hierarchy puts them back."""
    bl_idname = "stepper.prune_hierarchy"
    bl_label = "Prune Hierarchy"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and any(
            "STEP_uuid" in o for o in context.selected_objects)

    def execute(self, context):
        selected = [o for o in context.selected_objects if "STEP_uuid" in o]
        # Operate on the whole file group(s) of the selection
        files = {o["STEP_file"] for o in selected if "STEP_file" in o}
        group = [o for o in bpy.data.objects
                 if o.get("STEP_file") in files and "STEP_uuid" in o]

        children = defaultdict(list)
        for o in group:
            if o.parent is not None:
                children[o.parent].append(o)

        def prunable(o):
            return (o.type == "EMPTY" and o.data is None
                    and o.instance_type != "COLLECTION"
                    and len(children.get(o, [])) == 1)

        removed_total = 0
        changed = True
        while changed:
            changed = False
            for o in list(group):
                if o.name not in bpy.data.objects or not prunable(o):
                    continue
                child = children[o][0]
                restore = []
                try:
                    restore = json.loads(child.get("STEP_prune_restore", "[]"))
                except Exception:
                    pass
                restore.append({
                    "name": o.name,
                    "matrix_world": [v for row in o.matrix_world for v in row],
                    "parent": o.parent.name if o.parent else None,
                    "props": {k: o[k] for k in o.keys()
                              if isinstance(o[k], (int, float, str))},
                })
                child["STEP_prune_restore"] = json.dumps(restore)

                child_mw = child.matrix_world.copy()
                new_parent = o.parent
                child.parent = new_parent
                if new_parent is not None:
                    child.matrix_parent_inverse = new_parent.matrix_world.inverted()
                child.matrix_world = child_mw

                # Bookkeeping
                if new_parent is not None:
                    children[new_parent].remove(o)
                    children[new_parent].append(child)
                children.pop(o, None)
                group.remove(o)
                bpy.data.objects.remove(o)
                removed_total += 1
                changed = True

        self.report({"INFO"}, f"Pruned {removed_total} empty level(s)")
        return {"FINISHED"}


class STEPPER_OT_prune_restore(bpy.types.Operator):
    """Recreate hierarchy levels removed by Prune Hierarchy"""
    bl_idname = "stepper.prune_restore"
    bl_label = "Restore Pruned Hierarchy"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and any(
            "STEP_prune_restore" in o for o in context.selected_objects)

    def execute(self, context):
        from mathutils import Matrix
        restored = 0
        # Worklist instead of a plain loop: a recreated empty can itself
        # carry restore metadata (cascade prunes record the deeper chain in
        # the removed empty's props). Those must be restored in the same
        # run, not left for a second click.
        worklist = list(context.selected_objects)
        while worklist:
            obj = worklist.pop(0)
            raw = obj.get("STEP_prune_restore")
            if not raw:
                continue
            try:
                chain = json.loads(raw)
            except Exception:
                continue
            child = obj
            # The chain is stored nearest-ancestor-first (prune order), which
            # is exactly the inside-out rebuild order: each recreated empty
            # becomes the parent of the previous one via the chaining below.
            for entry in chain:
                empty = bpy.data.objects.new(entry["name"], None)
                empty.empty_display_size = 2
                empty.empty_display_type = "PLAIN_AXES"
                for k, v in entry.get("props", {}).items():
                    empty[k] = v
                for col in child.users_collection:
                    col.objects.link(empty)
                parent = bpy.data.objects.get(entry.get("parent") or "")
                mw = Matrix([entry["matrix_world"][i * 4:i * 4 + 4]
                             for i in range(4)])
                if parent is not None:
                    empty.parent = parent
                    empty.matrix_parent_inverse = parent.matrix_world.inverted()
                empty.matrix_world = mw

                child_mw = child.matrix_world.copy()
                child.parent = empty
                child.matrix_parent_inverse = empty.matrix_world.inverted()
                child.matrix_world = child_mw
                if "STEP_prune_restore" in entry.get("props", {}):
                    worklist.append(empty)
                child = empty
                restored += 1
            del obj["STEP_prune_restore"]

        self.report({"INFO"}, f"Restored {restored} hierarchy level(s)")
        return {"FINISHED"}


NORMAL_STASH = "stepper_normal"


def stash_normals(me):
    """Copy the shading normals of a mesh into a plain corner attribute.

    Blender keeps custom normals as offsets from the normals it calculates
    itself. Join two triangles into a quad, or merge two faces, and the
    calculated normals move, so the stored offsets now point somewhere
    else. That is the shading damage a bmesh round trip does. Edit mode
    operators correct for this, and a bmesh operator does not.

    A float vector attribute holds the normal itself, and bmesh carries it
    through every join and merge. restore_normals() writes it back.
    Returns False when the mesh has no custom normals to keep.
    """
    if not me.has_custom_normals or not len(me.loops):
        return False
    old = me.attributes.get(NORMAL_STASH)
    if old is not None:
        me.attributes.remove(old)
    n = np.empty(len(me.loops) * 3, dtype=np.float32)
    me.corner_normals.foreach_get("vector", n)
    me.attributes.new(NORMAL_STASH, "FLOAT_VECTOR", "CORNER").data.foreach_set(
        "vector", n)
    return True


def sharpen_split_normals(bm, limit=math.radians(0.5)):
    """Mark an edge sharp where the stashed normals do not agree across it.

    Merge by distance can join two faces that met only at a split, such as
    a crease where the tessellation had doubled vertices. The new edge is
    smooth, and a smooth edge would blend the two normals into one. The
    stashed normals show where the split was, so the edge stays a split.
    """
    layer = bm.loops.layers.float_vector.get(NORMAL_STASH)
    if layer is None:
        return 0
    cos_lim = math.cos(limit)
    made = 0
    for e in bm.edges:
        if not e.smooth or len(e.link_loops) != 2:
            continue
        la, lb = e.link_loops
        # la runs v0 to v1 and lb runs the other way, so each corner of
        # la meets the next corner of lb at the same vertex.
        for a, b in ((la, lb.link_loop_next), (la.link_loop_next, lb)):
            if a[layer].dot(b[layer]) < cos_lim:
                e.smooth = False
                made += 1
                break
    return made


def restore_normals(me):
    """Write the stashed normals back as custom normals, then drop the copy."""
    a = me.attributes.get(NORMAL_STASH)
    if a is None:
        return False
    n = np.empty(len(me.loops) * 3, dtype=np.float32)
    a.data.foreach_get("vector", n)
    me.attributes.remove(a)
    if len(me.loops):
        me.normals_split_custom_set(n.reshape(-1, 3))
    return True


class STEPPER_OT_mesh_cleanup(bpy.types.Operator):
    """Merge close vertices and dissolve coplanar faces. Keep the sharp edges
    and the imported shading"""
    bl_idname = "stepper.mesh_cleanup"
    bl_label = "Cleanup selected meshes"
    bl_options = {"REGISTER", "UNDO"}

    merge_distance: bpy.props.FloatProperty(
        name="Merge distance", unit="LENGTH", default=0.00001, min=0.0,
        precision=6)
    dissolve_angle: bpy.props.FloatProperty(
        name="Dissolve angle", unit="ROTATION", default=radians(1.0),
        min=0.0, max=radians(30.0))
    protect_over_45: bpy.props.BoolProperty(
        name="Protect edges over 45°",
        description="Temporarily mark steep edges sharp so limited dissolve "
                    "cannot remove them",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        processed = set()
        total_removed_verts = 0
        total_removed_faces = 0
        for obj in context.selected_objects:
            if obj.type != "MESH" or obj.data in processed:
                continue
            me = obj.data
            processed.add(me)
            pre_v, pre_f = len(me.vertices), len(me.polygons)

            kept = stash_normals(me)
            bm = bmesh.new()
            bm.from_mesh(me)

            if self.merge_distance > 0.0:
                bmesh.ops.remove_doubles(
                    bm, verts=bm.verts[:], dist=self.merge_distance)

            temp_sharp = []
            if self.protect_over_45:
                for e in bm.edges:
                    if e.smooth and len(e.link_faces) == 2:
                        if e.calc_face_angle(0.0) > radians(45.0):
                            e.smooth = False
                            temp_sharp.append(e)

            if self.dissolve_angle > 0.0:
                bmesh.ops.dissolve_limit(
                    bm, angle_limit=self.dissolve_angle,
                    use_dissolve_boundaries=False,
                    verts=bm.verts[:], edges=bm.edges[:],
                    delimit={"SHARP", "MATERIAL", "SEAM"})

            for e in temp_sharp:
                if e.is_valid:
                    e.smooth = True

            if kept:
                sharpen_split_normals(bm)
            bm.to_mesh(me)
            bm.free()
            restore_normals(me)
            me.update()

            total_removed_verts += pre_v - len(me.vertices)
            total_removed_faces += pre_f - len(me.polygons)

        self.report({"INFO"},
                    f"Removed {total_removed_verts} verts, "
                    f"{total_removed_faces} faces on {len(processed)} mesh(es)")
        return {"FINISHED"}


class STEPPER_OT_add_box_uv(bpy.types.Operator):
    """Box project the UVMap layer of the selected meshes with a world unit tile
    size. The addon creates the layer if it does not exist."""
    bl_idname = "stepper.add_box_uv"
    bl_label = "Box Project UVs"
    bl_options = {"REGISTER", "UNDO"}

    box_uv_scale: bpy.props.FloatProperty(
        name="Box UV size", unit="LENGTH", default=1.0, min=0.0001)

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        n = 0
        processed = set()
        for obj in context.selected_objects:
            if obj.type != "MESH" or obj.data in processed:
                continue
            processed.add(obj.data)
            # Meshes carry baked (world-ish) coordinates post-import
            if uv_mod.add_box_uv(obj.data, scale=self.box_uv_scale):
                n += 1
        self.report({"INFO"}, f"Box-projected UVMap on {n} mesh(es)")
        return {"FINISHED"}


class STEPPER_OT_reapply_uv(bpy.types.Operator):
    """Make the UVMap layer of the selected parts again with the settings in
    this panel. Box Project works on the mesh as it is. The other modes need
    the CAD data, so the addon reads the source file again and replaces the
    mesh, the same way Regenerate does. The settings go on to each object, so
    a later Regenerate or Refresh keeps them."""
    bl_idname = "stepper.reapply_uv"
    bl_label = "Apply UVs to Selected"
    bl_options = {"REGISTER", "UNDO"}

    # Every UV key of the import record this panel is allowed to change.
    KEYS = ("uv_mode", "uv_normalize", "uv_unwrap_method", "uv_closed_seams",
            "uv_merge_tangent", "box_uv_scale", "uv_pack", "uv_pack_tiles",
            "uv_pack_margin")

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and any(
            o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        from . import main as m

        prg = context.scene.stepper
        want = {k: getattr(prg, k) for k in self.KEYS}

        targets = []
        seen = set()
        for obj in context.selected_objects:
            if obj.type != "MESH" or obj.data is None or obj.data in seen:
                continue
            seen.add(obj.data)
            targets.append(obj)
        if not targets:
            self.report({"WARNING"}, "Select a mesh first")
            return {"CANCELLED"}

        # The settings travel with the object, so a Regenerate or a Refresh
        # later keeps the UV map chosen here instead of the one the import
        # made.
        for obj in targets:
            try:
                rec = json.loads(obj.get("STEP_import_settings", "{}"))
            except Exception:
                rec = {}
            if not isinstance(rec, dict):
                rec = {}
            rec.update(want)
            obj["STEP_import_settings"] = json.dumps(rec)

        if want["uv_mode"] == "BOX":
            # Box projection reads the mesh and nothing else, so the CAD
            # file stays closed and the mesh the user has is kept.
            n = 0
            for obj in targets:
                if uv_mod.add_box_uv(obj.data, scale=want["box_uv_scale"]):
                    n += 1
            made = "box projected %d mesh(es)" % n
        else:
            from_cad = [o for o in targets
                        if "STEP_file" in o and "STEP_tag" in o]
            if not from_cad:
                self.report({"WARNING"},
                            "This mode needs the CAD data. Select parts that "
                            "came from a STEP file")
                return {"CANCELLED"}
            for obj in context.selected_objects:
                obj.select_set(obj in from_cad)
            context.view_layer.objects.active = from_cad[0]
            bpy.ops.stepper.regenerate(use_scene_settings=False)
            targets = from_cad
            made = "rebuilt %d mesh(es) from the CAD data" % len(from_cad)

        # The passes the import runs after the UV map is written. The quad
        # setting comes from the record, so a part keeps the pairing it was
        # imported with.
        try:
            rec = json.loads(targets[0].get("STEP_import_settings", "{}"))
        except Exception:
            rec = {}
        if rec.get("tris_to_quads") and want["uv_mode"] != "BOX":
            m._tris_to_quads_objects(targets)
        if want["uv_pack"] != "NONE":
            m._pack_uv_objects(targets, want["uv_pack"],
                               want["uv_pack_tiles"], want["uv_pack_margin"],
                               bool(want["uv_normalize"]))

        self.report({"INFO"}, "UV: " + made)
        return {"FINISHED"}


classes = (
    STEPPER_OT_regenerate,
    STEPPER_OT_prune_hierarchy,
    STEPPER_OT_prune_restore,
    STEPPER_OT_mesh_cleanup,
    STEPPER_OT_add_box_uv,
    STEPPER_OT_reapply_uv,
)
