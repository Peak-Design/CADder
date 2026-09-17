# Post-import tools for CADder: state-preserving Regenerate,
# Prune/Restore hierarchy and mesh cleanup.

import json
from collections import defaultdict
import math
from math import radians

import bmesh
import bpy
import numpy as np

from . import uv as uv_mod


def under(collection):
    """A collection and every collection inside it, however deep."""
    found = [collection]
    stack = [collection]
    guard = 0
    while stack and guard < 10000:
        guard += 1
        here = stack.pop()
        for child in here.children:
            if child in found:
                continue
            found.append(child)
            stack.append(child)
    return found


def scope_of(context, keep=None):
    """What an operator covers, and where it came from.

    Returns (parts, collection). The scope is READ from the selection
    rather than set on a panel, because the selection already says it.
    Parts that are selected are the parts the operator covers. With no part
    selected it is the collection that is active in the outliner, and every
    collection below it, so a whole subassembly is treated at once without
    picking its parts out. collection is that collection, and None when the
    parts came from the selection.
    """
    if keep is None:
        keep = _is_mesh
    selected = [o for o in context.selected_objects if keep(o)]
    holder = None
    if selected:
        found = selected
    else:
        holder = context.collection
        wanted = set(under(holder)) if holder is not None else set()
        found = [o for o in context.scene.objects
                 if keep(o) and any(c in wanted for c in o.users_collection)]
    seen = set()
    kept = []
    for obj in found:
        if obj.name in seen:
            continue
        seen.add(obj.name)
        kept.append(obj)
    return kept, holder


def _is_mesh(obj):
    return obj.type == "MESH"


def scope_objects(context, keep=None):
    """The parts an operator covers. See scope_of."""
    return scope_of(context, keep)[0]


def linked_parts(context, objects):
    """Those parts, and every other part of the scene that shares a mesh
    with one of them.

    Two placements of one part are ONE piece of geometry in Blender: both
    objects point at the same mesh, and that link is most of what makes a
    large assembly workable. Asking for one of them defeatured and not the
    other gives them two meshes, so the link is the unit: what is asked for
    is asked for together (Oscar, 2026-09-17).
    """
    shared = {_geometry_of(o) for o in objects}
    shared.discard(None)
    if not shared:
        return list(objects)
    found = list(objects)
    seen = {o.name for o in objects}
    for obj in context.scene.objects:
        if obj.name in seen or _geometry_of(obj) not in shared:
            continue
        if not (from_step(obj) or from_cad_link(obj)):
            continue
        seen.add(obj.name)
        found.append(obj)
    return found


def _geometry_of(obj):
    """What decides whether two parts are one piece of geometry: the mesh
    they share, or the collection they both instance."""
    collection = getattr(obj, "instance_collection", None)
    return collection if collection is not None else obj.data


def scope_hint(layout, context):
    """Says what the button below will cover, when that is not the
    selection. The scope is read from the selection rather than set, so the
    one case that needs saying is the one where nothing is selected."""
    if any(o.type == "MESH" for o in context.selected_objects):
        return
    holder = context.collection
    if holder is None:
        return
    layout.label(text='Covers the collection "%s"' % holder.name,
                 icon="OUTLINER_COLLECTION")


def simplify_settings(obj, scene):
    """What one part travels as: (leave small features out, size, curved).

    The rig subpackage owns the switch, and CADder loads that subpackage
    guarded so a rig failure never costs the STEP import. Asking for it here
    rather than importing it at the top keeps that true.
    """
    try:
        from .rig import simplify as simplify_mod
    except Exception:                               # noqa: BLE001
        return False, 0.0, False
    return simplify_mod.settings_for(obj, scene)


def from_step(obj):
    """A part this addon read out of a STEP file, which it can read again."""
    return obj.type == "MESH" and "STEP_file" in obj and "STEP_tag" in obj


def from_cad_link(obj):
    """A part the CAD application sent over the live link.

    In the collection instance hierarchy mode the object that carries the
    component is an EMPTY: the geometry sits on a prototype inside the
    collection it instances, shared by every placement of that part. That
    empty is the part as far as the CAD application is concerned, so it
    counts here and mesh_parts finds the geometry when geometry is wanted.
    """
    if not obj.get("RIG_component_id"):
        return False
    return obj.type == "MESH" or (
        obj.type == "EMPTY" and obj.instance_collection is not None)


def mesh_parts(objects):
    """The objects whose MESH is the geometry of these parts, once each.

    A part sent as a collection instance is an empty, and its geometry is
    on the prototypes inside the collection it instances. Every placement
    of that part instances the same collection, so the prototype is reached
    once however many placements there are.
    """
    found, seen = [], set()
    for obj in objects or []:
        if obj.type == "MESH":
            here = [obj]
        else:
            collection = getattr(obj, "instance_collection", None)
            here = [o for o in collection.all_objects
                    if o.type == "MESH"] if collection is not None else []
        for mesh_object in here:
            if mesh_object.name in seen:
                continue
            seen.add(mesh_object.name)
            found.append(mesh_object)
    return found


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
        name="CADder Panel Resolution",
        description="Use the resolution values from the CADder panel. Turn "
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
        simplified = 0
        features = [0, 0]
        failed = []
        quad_objs = []
        unwrap_objs = {}
        # Smart runs once for each set of settings, because the tile it
        # fits and the bend it allows come from the record of each object.
        smart_objs = {}
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

                # Small features come out of the SHAPE, before it is
                # tessellated. The direct link asks SolidWorks to leave them
                # out because SolidWorks holds the part; here the STEP file
                # holds it, so the same decision is made on the shape.
                want, size, curved = simplify_settings(obj, context.scene)
                if want:
                    from . import simplify_brep
                    lines = []
                    lighter, history, taken, left = simplify_brep.apply(
                        shp, size, curved, lines.append)
                    for line in lines:
                        print("[CADder] " + line)
                    if lighter is not None:
                        simplify_brep.carry_colors(reader, shp, lighter, history)
                        shp = lighter
                        simplified += 1
                    features[0] += taken
                    features[1] += left

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

                # Restore per-import UV options for the apply path. Older
                # records name the UV map in other terms (uv.migrate_settings).
                uv_mod.migrate_settings(stored)
                uv_mode = stored["uv_mode"]
                m._set_uv_options(
                    uv_mode, stored.get("uv_normalize", True),
                    # Records written before the dropdown carry a boolean.
                    stored.get("uv_closed_seams",
                               "SPLIT" if stored.get("uv_split_closed", True)
                               else "NONE"),
                    stored.get("box_uv_scale", 1.0))
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
                # The passes the import runs once the mesh exists, in the
                # same order: quads, then Smart, then the unwrap.
                if stored.get("tris_to_quads") and uv_mode != "BOX":
                    quad_objs.append(obj)
                if uv_mode in uv_mod.UNWRAP_MODES:
                    unwrap_objs.setdefault(uv_mode, []).append(obj)
                elif uv_mode == "SMART":
                    smart_objs.setdefault(
                        (stored.get("uv_pack", "NONE"),
                         stored.get("uv_pack_tiles", 4),
                         stored.get("uv_smart_distortion",
                                    m.UV_SMART_DISTORTION),
                         bool(stored.get("uv_smart_sharp", False)),
                         bool(stored.get("uv_smart_split", True))),
                        []).append(obj)
                done += 1
                wm.progress_update(done)

        if quad_objs:
            m._tris_to_quads_objects(quad_objs)

        for smart_set, objs_s in smart_objs.items():
            m._smart_merge_objects(objs_s, *smart_set)

        # One unwrap for each method, so every part gets the one it was
        # imported with. Regenerated meshes are already scaled to scene
        # units, so real-world UV mode needs no extra unit conversion.
        for method, objs_m in unwrap_objs.items():
            m._unwrap_uv_objects(
                objs_m,
                world_scale=(None if m._uv_options.get("normalize", True)
                             else 1.0),
                method=method)

        wm.progress_end()

        # Re-apply active material database
        db_path = m._get_active_matdb_path()
        if db_path:
            mappings = m._ensure_matdb_materials(db_path)
            m._apply_matdb_to_objects(list(targets.values()), mappings)

        note = ""
        if simplified:
            note = (", %d defeatured (%d feature(s) out, %d left alone)"
                    % (simplified, features[0], features[1]))
        if failed:
            self.report({"WARNING"},
                        f"Regenerated {done}{note}; "
                        f"failed: {', '.join(failed[:5])}")
        else:
            self.report({"INFO"}, f"Regenerated {done} mesh(es){note}")
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
    bl_label = "Clean Up Selected Meshes"
    bl_options = {"REGISTER", "UNDO"}

    merge_distance: bpy.props.FloatProperty(
        name="Merge Distance", unit="LENGTH", default=0.00001, min=0.0,
        precision=6)
    dissolve_angle: bpy.props.FloatProperty(
        name="Dissolve Angle", unit="ROTATION", default=radians(1.0),
        min=0.0, max=radians(30.0))
    protect_over_45: bpy.props.BoolProperty(
        name="Protect Edges over 45°",
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
        name="Box UV Size", unit="LENGTH", default=1.0, min=0.0001)

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
    """Make the UVMap layer of these parts again with the settings in this
    panel. Box Project works on the mesh as it is. The other modes need the
    CAD data: a part from a STEP file is read from the file again, and a part
    from the live link is asked of the CAD application again. The settings go
    on to each object, so a later Regenerate or Rebuild keeps them."""
    bl_idname = "stepper.reapply_uv"
    bl_label = "Apply UVs"
    bl_options = {"REGISTER", "UNDO"}

    # Every UV key of the import record this panel is allowed to change.
    KEYS = ("uv_mode", "uv_normalize", "uv_closed_seams",
            "uv_smart_distortion", "uv_smart_sharp", "uv_smart_split",
            "box_uv_scale", "uv_pack", "uv_pack_tiles", "uv_pack_margin")

    @classmethod
    def poll(cls, context):
        # Not "is something selected": with nothing selected the scope is
        # the collection that is active in the outliner.
        if context.mode != "OBJECT":
            cls.poll_message_set("Leave edit mode first")
            return False
        if not any(o.type == "MESH" for o in context.scene.objects):
            cls.poll_message_set("This scene holds no meshes")
            return False
        return True

    def execute(self, context):
        from . import main as m

        prg = context.scene.stepper
        want = {k: getattr(prg, k) for k in self.KEYS}

        covered = scope_objects(
            context, lambda o: _is_mesh(o) or from_cad_link(o))
        targets = []
        seen = set()
        for obj in mesh_parts(covered):
            if obj.data is None or obj.data in seen:
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
            # Put an older record in today's terms first. Its old keys would
            # otherwise overrule the mode chosen here.
            uv_mod.migrate_settings(rec)
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
            # The CAD data is where these modes start, and a part knows which
            # CAD data is its own: a STEP file on disk, or the CAD
            # application holding the live model. Both come back with one
            # island per CAD face, which is what every mode below builds on.
            step = [o for o in covered if from_step(o)]
            live = [o for o in covered if from_cad_link(o) and o not in step]
            if not step and not live:
                self.report({"WARNING"},
                            "This mode needs the CAD data. Select parts that "
                            "came from a STEP file or over the live link")
                return {"CANCELLED"}
            made = []
            if live:
                done = _ask_cad_link(context, live)
                if done is None:
                    self.report({"ERROR"},
                                "The CAD application could not be reached")
                    return {"CANCELLED"}
                made.append("%d from the CAD application" % done)
            if step:
                for obj in context.selected_objects:
                    obj.select_set(obj in step)
                context.view_layer.objects.active = step[0]
                bpy.ops.stepper.regenerate(use_scene_settings=False)
                made.append("%d from the STEP file" % len(step))
            # A part from the live link arrives with the coordinates
            # SolidWorks gave it and nothing else has been done to it, so the
            # modes that build on the CAD charts run here.
            if live:
                _uv_modes_on_live(m, mesh_parts(live), want)
            targets = step + mesh_parts(live)
            made = "rebuilt " + " and ".join(made)

        # Regenerate has paired the triangles again as the record asks, so
        # packing is the one pass left.
        if want["uv_pack"] != "NONE":
            m._pack_uv_objects(targets, want["uv_pack"],
                               want["uv_pack_tiles"], want["uv_pack_margin"],
                               bool(want["uv_normalize"]))

        self.report({"INFO"}, "UV: " + made)
        return {"FINISHED"}


class STEPPER_OT_apply_simplify(bpy.types.Operator):
    """Leave the small features out of these parts and ask for their
    geometry again. A part that came in over the live link is asked of the
    CAD application. A part that came from a STEP file is read from the file
    again. Nothing in the CAD document or the STEP file is changed."""
    # The idname keeps the older word. It is what a keymap or a macro
    # calls, and a rename would break those for a label.
    bl_idname = "stepper.apply_simplify"
    bl_label = "Apply Defeature"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        # Not "is something selected": with nothing selected the scope is
        # the collection that is active in the outliner. What the scope
        # found is reported by execute.
        if context.mode != "OBJECT":
            cls.poll_message_set("Leave edit mode first")
            return False
        if not any(from_step(o) or from_cad_link(o)
                   for o in context.scene.objects):
            cls.poll_message_set(
                "This scene holds no parts from a STEP file or the live link")
            return False
        return True

    def execute(self, context):
        from .rig import simplify as simplify_mod

        covered, holder = scope_of(
            context, lambda o: from_step(o) or from_cad_link(o))
        if not covered:
            self.report({"WARNING"},
                        "Select parts that came from a STEP file or over the "
                        "live link")
            return {"CANCELLED"}

        # Two placements of one part share one mesh, and asking for one of
        # them without the other would break that link.
        covered = linked_parts(context, covered)

        # The button is the plain way to say "these parts, defeatured", so
        # it turns the switch on as well as acting on it. A collection is
        # set once and covers everything below it, which also survives a
        # rebuild that replaces every object.
        groups, parts = simplify_mod.turn_on(holder, covered, context.scene)
        turned = "set on %d collection(s) and %d part(s)" % (groups, parts)             if groups else "set on %d part(s)" % parts

        step = [o for o in covered if from_step(o)]
        # A part is asked of the CAD application when it can be,
        # because that is the live copy.
        live = [o for o in covered if from_cad_link(o) and o not in step]

        said = []
        if live:
            done = _ask_cad_link(context, live)
            if done is None:
                return {"CANCELLED"}
            said.append("%d part(s) from the CAD application" % done)
        if step:
            for obj in context.selected_objects:
                obj.select_set(obj in step)
            context.view_layer.objects.active = step[0]
            bpy.ops.stepper.regenerate(use_scene_settings=False)
            said.append("%d part(s) from the STEP file" % len(step))
        if not said:
            self.report({"WARNING"}, "Nothing here can be asked for again")
            return {"CANCELLED"}
        self.report({"INFO"}, "Defeature: %s, %s"
                    % (turned, ", ".join(said)))
        return {"FINISHED"}


def _uv_modes_on_live(m, objs, want):
    """The UV passes a part from the live link needs after its geometry has
    come back.

    The STEP route runs these inside Regenerate, from the record each object
    carries. A part from the live link has no such record and no STEP file,
    so its geometry comes back from the CAD application and the same passes
    run here, in the same order.

    Smart needs to know which faces touch, and a mesh from the CAD
    application does not say: every CAD face carries its own copy of the
    points along its edges, so no two faces share an edge and Smart would
    find nothing to join. weld() puts that right without changing the shape
    or the shading.
    """
    mode = want["uv_mode"]
    if mode == "SMART":
        for obj in objs:
            weld(obj.data)
        m._smart_merge_objects(
            objs, want["uv_pack"], want["uv_pack_tiles"],
            want["uv_smart_distortion"], bool(want["uv_smart_sharp"]),
            bool(want["uv_smart_split"]))
    elif mode in uv_mod.UNWRAP_MODES:
        m._unwrap_uv_objects(
            objs,
            world_scale=None if want["uv_normalize"] else 1.0,
            method=mode)


def weld(me, distance=1e-6):
    """Joins the points a CAD mesh carries twice, and marks what was a CAD
    face boundary sharp and as a UV seam.

    A mesh from the live link is one patch of triangles per CAD face, each
    with its own copy of the points along its edges. That is what lets every
    point carry its own surface coordinates, and it also means the faces do
    not touch: anything that walks from face to face finds nothing.

    The normals are put back exactly as they were, so the shading does not
    change. Nothing moves: the points joined were already in the same place.
    """
    if me is None or not len(me.polygons):
        return False
    normals = [tuple(loop.normal) for loop in me.loops] \
        if me.has_custom_normals else None
    before = len(me.vertices)
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=distance)
        bm.to_mesh(me)
    finally:
        bm.free()
    if len(me.vertices) == before:
        return False
    # An edge whose two faces disagree about where its ends are in the UV
    # map was a CAD face boundary before the weld, and has to stay one.
    #
    # Compared FACE BY FACE rather than loop by loop. The two faces of an
    # edge walk it in opposite directions, so their loops on it start at
    # different ends and comparing them in loop order compares the wrong
    # pairs and finds no disagreement anywhere.
    layer = me.uv_layers.active
    if layer is not None:
        corners = []
        for poly in me.polygons:
            corners.append({
                me.loops[i].vertex_index: tuple(
                    round(v, 6) for v in layer.data[i].uv)
                for i in poly.loop_indices})
        number = {tuple(sorted(e.vertices)): e.index for e in me.edges}
        sides = {}
        for index, poly in enumerate(me.polygons):
            for key in poly.edge_keys:
                sides.setdefault(tuple(sorted(key)), []).append(index)
        for key, faces in sides.items():
            if len(faces) != 2 or key not in number:
                continue
            here, there = corners[faces[0]], corners[faces[1]]
            if all(here.get(v) == there.get(v) for v in key):
                continue
            edge = me.edges[number[key]]
            edge.use_seam = True
            edge.use_edge_sharp = True
    if normals is not None and len(normals) == len(me.loops):
        me.normals_split_custom_set(normals)
    return True


def _ask_cad_link(context, objs):
    """Asks the CAD application for these parts again, with whatever the
    scene now holds them simplified to. Returns how many came back, or None
    when the CAD application could not be reached."""
    from .rig import cad_link, native_import, simplify as simplify_mod, ui as rig_ui

    ids, persistent = [], []
    for obj in objs:
        component = obj.get("RIG_component_id")
        if component and component not in ids:
            ids.append(component)
        found = obj.get("SWMESH_persistent_id")
        if found and found not in persistent:
            persistent.append(found)
    if not ids:
        return 0
    try:
        reply = cad_link.retessellate(
            ids, rig_ui.quality_dial(context.scene.cad_link),
            persistent_ids=persistent,
            simplify=simplify_mod.orders(objs, context.scene))
        return len(native_import.refine(context, reply["mesh"]))
    except cad_link.CadLinkError as exc:
        _report(context, str(exc))
        return None
    except (OSError, ValueError) as exc:
        _report(context, "Could not read what the CAD application sent: %s" % exc)
        return None


def _report(context, message):
    print("[CADder] " + message)


classes = (
    STEPPER_OT_regenerate,
    STEPPER_OT_prune_hierarchy,
    STEPPER_OT_prune_restore,
    STEPPER_OT_mesh_cleanup,
    STEPPER_OT_add_box_uv,
    STEPPER_OT_reapply_uv,
    STEPPER_OT_apply_simplify,
)
