# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Created Date: Thursday, April 15th 2021, 4:38:48 pm
# Copyright: Tommi Hyppänen
#
# Modified 2026 by Peak-Design:
#   - Ported to Blender 5.0 API
#   - Added failed parts tracking and popup warnings
#   - Import summary improvements

import dataclasses
import json
import ntpath
import os
import time
import sys
from collections import Counter, OrderedDict

import numpy as np
import bmesh  # type: ignore
import bpy  # type: ignore
from bpy.props import StringProperty  # type: ignore
from bpy_extras.io_utils import ImportHelper  # type: ignore
from mathutils import Matrix, Vector  # type: ignore

from .trimesh import TriMesh
from .importer import NativeMeshData
from . import import_ui
from . import uv as uv_mod
from . import tools as tools_mod
from . import curves as curves_mod
from . import analyzer as analyzer_mod
from . import refresh as refresh_mod
from . import background as background_mod
from . import updater as updater_mod
from .formats import classes as formats_classes

# Active UV options for the current import (set by load_step)
# Per-import UV generation state (a single "UVMap" layer; the booleans are
# derived from the operator's uv_mode enum in load_step)
_uv_options = {"surface": True, "unwrap": False, "box": False,
               "box_scale": 1.0}

# LRU file cache with max entry limit
MAX_FILE_CACHE = 10
global_file_cache = OrderedDict()
must_have_python = (3, 13)

# Color quantization precision for material merging (~1.5% tolerance)
_COLOR_MERGE_PRECISION = 64


def _quantize_color(col):
    """Round color components to merge near-identical materials."""
    return tuple(round(c * _COLOR_MERGE_PRECISION) / _COLOR_MERGE_PRECISION for c in col)


# Freshness stamp per cached path. The cache used to key on path alone,
# which served YESTERDAY'S geometry whenever a file was re-exported to the
# same path (an export loop that rewrites one file does exactly that,
# live 2026-08-23, when a re-posed assembly kept importing at the old pose).
global_file_cache_meta = {}


def _file_signature(filepath):
    try:
        st = os.stat(filepath)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _cache_put(filepath, step_reader):
    """Add to file cache with LRU eviction."""
    if filepath in global_file_cache:
        global_file_cache.move_to_end(filepath)
    global_file_cache[filepath] = step_reader
    global_file_cache_meta[filepath] = _file_signature(filepath)
    while len(global_file_cache) > MAX_FILE_CACHE:
        evicted_path, _ = global_file_cache.popitem(last=False)
        global_file_cache_meta.pop(evicted_path, None)
        print(f"Cache evicted: {os.path.basename(evicted_path)}")


def _cache_get(filepath):
    """Get from file cache, updating LRU order. Returns None if not found
    or if the file on disk changed since it was cached."""
    if filepath in global_file_cache:
        sig = _file_signature(filepath)
        if sig is None or sig != global_file_cache_meta.get(filepath):
            del global_file_cache[filepath]
            global_file_cache_meta.pop(filepath, None)
            print(f"Cache stale (file changed on disk): {os.path.basename(filepath)}")
            return None
        global_file_cache.move_to_end(filepath)
        return global_file_cache[filepath]
    return None


def _record_parse_calibration(step_reader, seconds):
    """Update the per-machine parse-speed estimate (entities/sec, EMA).

    Used by the pre-import analyzer to predict import times.
    """
    try:
        xr = getattr(step_reader, "_xcaf_reader", None)
        if xr is None or seconds <= 0.05:
            return
        model = xr.ChangeReader().StepModel()
        if model is None:
            return
        eps = model.NbEntities() / seconds
        prefs = _get_addon_prefs()
        try:
            cal = json.loads(prefs.perf_calibration or "{}")
        except Exception:
            cal = {}
        prev = cal.get("parse_eps")
        cal["parse_eps"] = eps if prev is None else prev * 0.7 + eps * 0.3
        prefs.perf_calibration = json.dumps(cal)
    except Exception:
        pass


def scalemat(mat, sl):
    scaling = np.zeros_like(mat)
    scaling[np.diag_indices(4)] = sl
    # print(scaling)
    return np.matmul(scaling, mat)


def obj_unlink_all(obj):
    """Unlink object from all collections"""
    old_col = obj.users_collection

    # bugfix: not in master collection bug
    # collection_name.objects.unlink(obj)
    if len(old_col) > 0:
        for c in old_col:
            c.objects.unlink(obj)


def add_material(name, color, link_vertex_color=False, overwrite=False):
    assert len(color) == 3
    assert isinstance(color, tuple)
    if len(name) > 60:
        name = name[:60]
    if name not in bpy.data.materials.keys() or overwrite:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True

        # TODO: If language is set to slovensky, this will fail
        # seems to not be issue for other languages tested so far
        # bsdf = mat.node_tree.nodes["Principled BSDF"]
        for node in mat.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                bsdf = node
                break

        # Set base color
        bsdf.inputs["Base Color"].default_value = (*color, 1.0)

        # # Connect alpha
        # a = mat.node_tree.nodes["Principled BSDF"].inputs["Alpha"]
        # mat.node_tree.links.new(sn.outputs["Alpha"], a)

        vcol = mat.node_tree.nodes.new(type="ShaderNodeVertexColor")
        vcol.location = [-400.0, 300.0]
        vcol.layer_name = "Colors"

        if link_vertex_color:
            mat.node_tree.links.new(vcol.outputs[0], bsdf.inputs[0])
    else:
        mat = bpy.data.materials[name]

    # mat.blend_method = "BLEND"
    # mat.shadow_method = "CLIP"
    # mat.node_tree.nodes["Image Texture"].image = image
    return mat


def _material_base_color(mat, fallback=(0.8, 0.8, 0.8)):
    """Read back a material's base color as an (r, g, b) tuple.

    add_material writes the CAD color to the Principled BSDF's Base Color
    input and never touches mat.diffuse_color (the viewport swatch), so
    reading diffuse_color would always return Blender's default grey.
    Materials the addon did not build (e.g. a Material Database
    replacement using some other shader) fall back to diffuse_color.
    """
    if mat is None:
        return fallback
    if mat.use_nodes and mat.node_tree is not None:
        for node in mat.node_tree.nodes:
            if node.type != "BSDF_PRINCIPLED":
                continue
            # Named lookup can fail under some UI translations; input 0 of
            # a Principled BSDF is Base Color either way.
            inp = node.inputs.get("Base Color")
            if inp is None and len(node.inputs):
                inp = node.inputs[0]
            if inp is not None:
                return tuple(inp.default_value[:3])
            break
    return tuple(mat.diffuse_color[:3])


def bpy_update_object_data(objdata, bm, vcol_name, colors, uvs, norms, mat_names, build_materials=True):
    if build_materials:
        # set colors and mats
        obj_mats = {}
        for obi, ob_mat in enumerate(objdata.materials):
            obj_mats[ob_mat.name] = obi
        mat_counter = 0

    write_uvs = (_uv_options.get("surface", True)
                 and uvs is not None and len(uvs) > 0)
    if len(colors) > 0:
        color_layer = bm.loops.layers.color.get(vcol_name)
        if color_layer is None:
            color_layer = bm.loops.layers.color.new(vcol_name)
        if write_uvs:
            uv_layer = bm.loops.layers.uv.get("UVMap")
            if uv_layer is None:
                uv_layer = bm.loops.layers.uv.new("UVMap")
        i = 0
        for face in bm.faces:
            mat_col = (0.5, 0.5, 0.5)
            mat_col_name = None
            for loop in face.loops:
                if write_uvs:
                    loop[uv_layer].uv = uvs[i]
                if colors[i][0] >= 0.0:
                    loop[color_layer] = (*colors[i], 1.0)
                    mat_col = colors[i]
                    mat_col_name = mat_names[i]
                else:
                    # No color: set it to default gray
                    loop[color_layer] = (0.5, 0.5, 0.5, 1.0)
                i += 1

            if build_materials:
                # Translate color into name, if not defined
                if mat_col_name is None:
                    # Quantize color to merge near-identical materials
                    mat_col = _quantize_color(mat_col)
                    mat_col_name = "STEP_" + "".join("{0:0{1}x}".format(int(mat_col[i] * 255), 2) for i in range(3))

                # add_material truncates to 60 chars, so truncate here too or
                # the bpy.data.materials lookups below KeyError on long
                # CAD material names
                mat_col_name = mat_col_name[:60]

                # If material doesn't exist, create it
                if mat_col_name not in bpy.data.materials:
                    add_material(mat_col_name, mat_col, link_vertex_color=False)

                # If material exists but it's not yet in object material slot, add it
                if mat_col_name not in obj_mats:
                    obj_mats[mat_col_name] = mat_counter
                    objdata.materials.append(bpy.data.materials[mat_col_name])
                    mat_counter += 1

                face.material_index = obj_mats[mat_col_name]
    else:
        # TODO: if no colors defined, create and apply default material
        pass

    # print("Polys: {}, Verts: {}".format(len(bm.faces), len(bm.verts)))

    # Save face situation so we can adjust accordingly later
    # pre_faces = bm.faces[:]

    # # Merge verts near each other
    # if merge_distance > 0.0:
    #     print("Removing doubles at distance:", merge_distance)
    #     bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=merge_distance)

    # Remove normals from array which don't exist in the mesh anymore
    # removed = set()
    # for fi, f in enumerate(pre_faces):
    #     if not f.is_valid:
    #         for i in range(fi * 3, fi * 3 + 3):
    #             removed.add(i)

    # Update mesh from Bmesh
    # Only switch mode if not already in OBJECT mode (e.g. during rebuild)
    active = bpy.context.object
    prev_mode = active.mode if active else "OBJECT"
    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    bm.to_mesh(objdata)

    if len(norms) > 0:
        objdata.normals_split_custom_set(np.array(norms))

    if _uv_options.get("box", False):
        # Same file-unit correction as _apply_uv_layers: verts are still in
        # file units here (scale is baked later)
        unit_scale = _uv_options.get("unit_scale", 1.0) or 1.0
        uv_mod.add_box_uv(
            objdata, scale=_uv_options.get("box_scale", 1.0) / unit_scale)

    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode=prev_mode)


def calculate_detail_level(dlev):
    """Angular deflection, Linear deflection"""
    if dlev < 100:
        l_def = 100.0 / float(dlev)
    else:
        l_def = (100.0 / float(dlev)) ** 2.0
    return 0.8, l_def


def set_obj_matrix_world(obj, mtx):
    """
    Copy Numpy matrix into Blender matrix
    """
    for row in range(mtx.shape[0]):
        for col in range(mtx.shape[1]):
            obj.matrix_world[row][col] = mtx[row][col]


def create_new_obj_with_mesh(name, set_active=True):
    """
    Create new empty object and mesh, link them, and optionally set to active
    """
    empty_mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, empty_mesh)
    bpy.context.collection.objects.link(obj)
    if set_active:
        bpy.context.view_layer.objects.active = obj
    return obj


def choose_hierarchy_types(htypes):
    """
    Return hierarchy types selection from input string
    """
    hierarchy_flat = False
    hierarchy_tree = False
    hierarchy_empties = False
    hierarchy_instances = False

    if htypes == "FLAT_AND_TREE":
        hierarchy_flat = True
        hierarchy_tree = True
    elif htypes == "TREE":
        hierarchy_tree = True
    elif htypes == "FLAT":
        hierarchy_flat = True
    elif htypes == "EMPTIES":
        hierarchy_empties = True
    elif htypes == "COLLECTION_INSTANCES":
        hierarchy_instances = True
    else:
        assert False, "Invalid input parameter"

    return hierarchy_flat, hierarchy_tree, hierarchy_empties, hierarchy_instances


def transform_to_up(up, chosen_objects, scale, to_cursor=True, apply_scale=True):
    """
    Set all chosen_objects transforms <up>["X", "Y", "Z"] as up
    Optionally move to cursor <to_cursor>
    Set scale to scale
    """

    # transforms and processing of objects
    # bpy.ops.object.select_all(action="DESELECT")

    cursor_pos = bpy.context.scene.cursor.location

    # up
    # up_as = self.up_as
    up_axis = {"X": 0, "Y": 1, "Z": 2}[up]

    # forward
    # fw_as = self.prg.fw_as
    # fw_axis = {"X": 0, "Y": 1, "Z": 2}[fw_as[0]]

    for obj in chosen_objects:
        # up, forward
        mat = np.array(obj.matrix_world)

        # blender default: Y(1) = forward, Z(2) = up
        if up_axis != 2:
            # if negate axis, do mirror
            # if up_as[1] == "N":
            #     dg = [1, 1, 1, 1]
            #     dg[up_axis] = -1
            #     mat = _scalemat(mat, dg)

            mat[[up_axis, 2]] = mat[[2, up_axis]]
            mat[up_axis] *= -1

        # scale
        mat = scalemat(mat, [*([scale] * 3), 1])

        # move to cursor position
        mat[0][3] += cursor_pos.x
        mat[1][3] += cursor_pos.y
        mat[2][3] += cursor_pos.z

        # apply
        set_obj_matrix_world(obj, mat)

    # Apply scale: bake scale into mesh vertices so obj.scale = (1,1,1)
    # Uses direct vertex scaling instead of bpy.ops.object.transform_apply
    # to avoid "Cannot apply to a multi user" errors on instanced meshes.
    if apply_scale and scale != 1.0:
        processed_meshes = set()
        for obj in chosen_objects:
            if obj.data is None:
                continue
            mesh = obj.data
            if mesh not in processed_meshes:
                if obj.type == "CURVE":
                    for spline in mesh.splines:
                        n_pts = len(spline.points)
                        if n_pts > 0:
                            co = np.empty(n_pts * 4, dtype=np.float32)
                            spline.points.foreach_get('co', co)
                            co = co.reshape(-1, 4)
                            co[:, :3] *= scale
                            spline.points.foreach_set('co', co.ravel())
                elif hasattr(mesh, "vertices"):
                    vert_count = len(mesh.vertices)
                    if vert_count > 0:
                        verts = np.empty(vert_count * 3, dtype=np.float32)
                        mesh.vertices.foreach_get('co', verts)
                        verts *= scale
                        mesh.vertices.foreach_set('co', verts)
                        mesh.update()
                processed_meshes.add(mesh)

        # Reset scale to 1 on EVERY object, hierarchy empties included (the
        # scale now lives in the mesh data). Snapshot the target world
        # matrices first and assign parents-before-children through
        # matrix_basis, since resetting only the meshes would leave a 0.001-scaled
        # top empty with 1000-scaled children (world-correct, locally wrong),
        # and naive matrix_world writes mid-loop read stale parent state.
        one = Vector((1.0, 1.0, 1.0))
        desired = {}
        for obj in chosen_objects:
            loc, rot, _ = obj.matrix_world.decompose()
            desired[obj] = Matrix.LocRotScale(loc, rot, one)

        def _depth(o):
            d = 0
            while o.parent is not None:
                d += 1
                o = o.parent
            return d

        for obj in sorted(chosen_objects, key=_depth):
            parent = obj.parent
            if parent is not None and parent in desired:
                obj.matrix_parent_inverse = Matrix.Identity(4)
                obj.matrix_basis = desired[parent].inverted() @ desired[obj]
            else:
                obj.matrix_basis = desired[obj]


# Debug timing flag, set from addon settings at import start
_debug_timing = False

# Cumulative timing accumulators for profiling Phase 2
_phase2_times = {
    "build_trimesh": 0.0,
    "fuse_verts": 0.0,
    "filter_zero_area": 0.0,
    "filter_same_face": 0.0,
    "fill_empty_color": 0.0,
    "get_all_loop_data": 0.0,
    "add_to_bm": 0.0,
    "bpy_update": 0.0,
}


def _reset_phase2_times():
    for k in _phase2_times:
        _phase2_times[k] = 0.0


def _print_phase2_times():
    print("\n--- Phase 2 timing breakdown ---")
    total = sum(_phase2_times.values())
    for k, v in sorted(_phase2_times.items(), key=lambda x: -x[1]):
        pct = (v / total * 100) if total > 0 else 0
        print(f"  {k:20s}: {v:7.2f}s  ({pct:4.1f}%)")
    print(f"  {'TOTAL':20s}: {total:7.2f}s")


def precompute_mesh_data(step_reader, shp, lind, angd, hacks, part_name="",
                         fallback_color=None, relative=False):
    """Compute mesh + loop data from OCC shape.

    Returns (mesh, colors, mat_names, norms, uvs).
    mesh is either TriMesh (Python fallback) or NativeMeshData (C++ path).
    """
    if _debug_timing:
        t0 = time.time()

    mesh = step_reader.build_trimesh(shp, lin_def=lind, ang_def=angd, hacks=hacks,
                                     part_name=part_name,
                                     fallback_color=fallback_color,
                                     relative=relative)

    if _debug_timing:
        t1 = time.time()

    if isinstance(mesh, NativeMeshData):
        # Native path: numpy vectorized operations
        mesh.fuse_verts()
        if _debug_timing: t2 = time.time()
        mesh.filter_zero_area()
        if _debug_timing: t3 = time.time()
        mesh.filter_same_face()
        if _debug_timing: t4 = time.time()
        mesh.fill_empty_color()
        if _debug_timing: t5 = time.time()
        # Data stays in numpy arrays, no conversion needed
        colors = mesh.get_loop_colors()
        mat_names = mesh.get_loop_mat_names()
        norms = mesh.get_loop_norms()
        uvs = mesh.get_loop_uvs()
        if _debug_timing: t6 = time.time()
    else:
        # Python fallback: TriMesh path
        mesh.fuse_verts()
        if _debug_timing: t2 = time.time()
        mesh.filter_zero_area()
        if _debug_timing: t3 = time.time()
        mesh.filter_same_face()
        if _debug_timing: t4 = time.time()
        mesh.fill_empty_color()
        if _debug_timing: t5 = time.time()
        colors, mat_names, norms, uvs = mesh.get_all_loop_data()
        if _debug_timing: t6 = time.time()

    if _debug_timing:
        _phase2_times["build_trimesh"] += t1 - t0
        _phase2_times["fuse_verts"] += t2 - t1
        _phase2_times["filter_zero_area"] += t3 - t2
        _phase2_times["filter_same_face"] += t4 - t3
        _phase2_times["fill_empty_color"] += t5 - t4
        _phase2_times["get_all_loop_data"] += t6 - t5

    return mesh, colors, mat_names, norms, uvs


def apply_mesh_to_blender(obj, mesh, colors, mat_names, norms, uvs,
                          vcol_name="Colors", build_materials=True):
    """Main-thread only: push precomputed mesh data into a Blender object."""
    if isinstance(mesh, NativeMeshData):
        return _apply_native_mesh(obj, mesh, colors, mat_names, norms, uvs,
                                  vcol_name, build_materials)
    else:
        return _apply_trimesh(obj, mesh, colors, mat_names, norms, uvs,
                              vcol_name, build_materials)


def _apply_uv_layers(me, uvs):
    """Write the single 'UVMap' layer per the active import UV options."""
    if _uv_options.get("surface", True) and uvs is not None and len(uvs) > 0:
        uv_mod.write_uv_layer(me, "UVMap", uvs)
    if _uv_options.get("box", False):
        # Mesh verts are still in file units here (scale is baked later),
        # so convert the world-unit tile size into file units.
        unit_scale = _uv_options.get("unit_scale", 1.0) or 1.0
        uv_mod.add_box_uv(
            me, scale=_uv_options.get("box_scale", 1.0) / unit_scale)


def _assign_engineering_material(obj, mat_info):
    """Replace the object's material slots with ONE Blender material named
    after the CAD engineering material (e.g. "AISI 304 Steel").

    The part's imported color is kept as the material's base color when the
    material is first created; density/description are stored as custom
    properties on the material. Slots live on the mesh datablock, so linked
    copies pick this up automatically.
    """
    me = obj.data
    if me is None or not mat_info or not mat_info.get("name"):
        return
    name = mat_info["name"][:60]
    mat = bpy.data.materials.get(name)
    if mat is None:
        color = (0.8, 0.8, 0.8)
        if len(me.materials) and me.materials[0] is not None:
            color = _material_base_color(me.materials[0], color)
        mat = add_material(name, color)
    if mat_info.get("density"):
        mat["STEP_density"] = float(mat_info["density"])
    if mat_info.get("description"):
        mat["STEP_description"] = mat_info["description"]
    me.materials.clear()
    me.materials.append(mat)
    n_polys = len(me.polygons)
    if n_polys:
        me.polygons.foreach_set(
            "material_index", np.zeros(n_polys, dtype=np.int32))


def _unwrap_uv_objects(objs, world_scale=None):
    """Angle-based unwrap with packed islands into the 'UVMap' layer.

    Runs Blender's unwrap operator once over all given objects in
    multi-object edit mode. The imported seams (marked at sharp normal
    discontinuities between CAD faces) define the islands; the unwrap
    operator packs them into the 0-1 square per mesh.

    world_scale: None keeps the packed 0-1 layout (Normalize UVs on).
    Otherwise the packed islands are uniformly rescaled so 1 UV unit
    matches 1 scene unit as closely as possible (median of the per-triangle
    3D/UV area ratios); the value is the mesh-unit -> scene-unit factor
    (file-unit meshes at import time, 1.0 for already-scaled meshes).
    """
    targets = []
    seen = set()
    for o in objs:
        if (o is not None and getattr(o, "type", None) == "MESH"
                and o.data is not None and len(o.data.polygons) > 0
                and o.data not in seen):
            seen.add(o.data)
            # The surface-UV pass usually created the layer already (its
            # content doubles as a fallback if the unwrap operator fails)
            layer = o.data.uv_layers.get("UVMap")
            if layer is None:
                layer = o.data.uv_layers.new(name="UVMap", do_init=False)
            if layer is None:  # Blender's 8-UV-layer limit
                continue
            o.data.uv_layers.active = layer
            targets.append(o)
    if not targets:
        return
    view_layer = bpy.context.view_layer
    try:
        for o in view_layer.objects:
            try:
                o.select_set(False)
            except RuntimeError:
                pass
        for o in targets:
            o.select_set(True)
        view_layer.objects.active = targets[0]
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0.005)
        bpy.ops.object.mode_set(mode="OBJECT")
        if world_scale is not None:
            for o in targets:
                _scale_unwrap_to_world(o.data, world_scale)
        print(f"UVMap unwrap: {len(targets)} mesh(es)"
              + ("" if world_scale is None else " (real-world scale)"))
    except Exception as e:
        print(f"UVMap unwrap failed: {e}")
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass


def _scale_unwrap_to_world(me, world_scale):
    """Rescale the packed unwrapped 'UVMap' so 1 UV unit ~= 1 scene unit.

    The unwrap is near-isometric per island, so a single uniform factor
    (the median of per-triangle sqrt(3D area / UV area)) restores physical
    scale while keeping the packed island arrangement.
    """
    layer = me.uv_layers.get("UVMap")
    n_loops = len(me.loops)
    if layer is None or n_loops == 0:
        return
    uv = np.empty(n_loops * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", uv)
    uv = uv.reshape(-1, 2)

    starts = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("loop_start", starts)
    a, b, c = uv[starts], uv[starts + 1], uv[starts + 2]
    uv_area = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                           - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))

    verts = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    li = np.empty(n_loops, dtype=np.int32)
    me.loops.foreach_get("vertex_index", li)
    p = verts[li[starts]]
    q = verts[li[starts + 1]]
    r = verts[li[starts + 2]]
    area3d = 0.5 * np.linalg.norm(np.cross(q - p, r - p), axis=1)

    valid = (uv_area > 1e-14) & (area3d > 1e-14)
    if not np.any(valid):
        return
    factor = float(np.median(np.sqrt(area3d[valid] / uv_area[valid])))
    factor *= world_scale  # mesh units -> scene units
    if factor <= 0.0:
        return
    layer.uv.foreach_set("vector", (uv * factor).ravel())


def _apply_native_mesh(obj, mesh, colors, mat_names, norms, uvs,
                       vcol_name, build_materials):
    """Fast path: from_pydata + foreach_set, no bmesh for geometry."""
    n_verts = len(mesh.verts)
    n_faces = len(mesh.faces)
    if _debug_timing:
        print(f"[bm] {n_verts}", end="")
        t0 = time.time()

    me = obj.data

    # Ensure OBJECT mode for mesh updates
    active = bpy.context.object
    prev_mode = active.mode if active else "OBJECT"
    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    # Clear existing geometry and material slots (needed for Rebuild Selected)
    me.clear_geometry()
    me.materials.clear()

    # Direct mesh creation via foreach_set (avoids numpy→list conversion)
    me.vertices.add(n_verts)
    me.vertices.foreach_set("co", mesh.verts.astype(np.float32).ravel())
    me.loops.add(n_faces * 3)
    me.loops.foreach_set("vertex_index", mesh.faces.astype(np.int32).ravel())
    me.polygons.add(n_faces)
    loop_starts = np.arange(0, n_faces * 3, 3, dtype=np.int32)
    me.polygons.foreach_set("loop_start", loop_starts)
    loop_totals = np.full(n_faces, 3, dtype=np.int32)
    me.polygons.foreach_set("loop_total", loop_totals)
    me.update()
    me.validate()

    if _debug_timing:
        t1 = time.time()

    # -- Vertex colors via foreach_set --
    if n_faces > 0 and colors is not None and len(colors) > 0:
        color_attr = me.color_attributes.new(
            name=vcol_name, type='FLOAT_COLOR', domain='CORNER')
        # colors is (T*3, 3) float32, need RGBA (T*3, 4)
        n_loops = len(colors)
        rgba = np.ones((n_loops, 4), dtype=np.float32)
        rgba[:, :3] = colors
        color_attr.data.foreach_set("color", rgba.ravel())

    # -- Materials --
    if build_materials and n_faces > 0:
        # Build per-face material names (use tri_mat_names directly, not per-loop)
        face_mat_names = mesh.tri_mat_names  # list[str|None] len=n_faces
        face_colors = mesh.tri_colors        # (n_faces, 3) float32

        # Resolve None names to auto-generated names
        # Pre-compute quantized color hex for unnamed faces using numpy
        q = np.round(face_colors * _COLOR_MERGE_PRECISION) / _COLOR_MERGE_PRECISION
        q_bytes = np.clip((q * 255).astype(np.int32), 0, 255)
        resolved_names = []
        for fi in range(n_faces):
            mn = face_mat_names[fi]
            if mn is None:
                r, g, b = q_bytes[fi]
                mn = f"STEP_{r:02x}{g:02x}{b:02x}"
            # Match add_material's 60-char truncation so the
            # bpy.data.materials lookups below never miss
            resolved_names.append(mn[:60])

        # Get unique material names and assign indices
        unique_names = list(dict.fromkeys(resolved_names))  # preserves order
        name_to_idx = {n: i for i, n in enumerate(unique_names)}

        # Ensure materials exist and attach to object
        for mn in unique_names:
            if mn not in bpy.data.materials:
                # Find the color for this material
                fi = resolved_names.index(mn)
                col = tuple(float(x) for x in face_colors[fi])
                add_material(mn, col[:3], link_vertex_color=False)
            me.materials.append(bpy.data.materials[mn])

        # Vectorized index assignment
        mat_indices = np.array([name_to_idx[n] for n in resolved_names], dtype=np.int32)
        me.polygons.foreach_set("material_index", mat_indices)

    # -- Seam/sharp edges: numpy computation + direct attribute API --
    # IMPORTANT: we must NOT use a bmesh round-trip here because
    # bm.from_mesh → bm.to_mesh can reorder loops, which breaks the
    # normals_split_custom_set mapping (norms follow from_pydata order).
    sharp_keys, seam_keys, max_v = _compute_edge_attributes(mesh)

    n_edges = len(me.edges)
    if n_edges > 0 and (len(sharp_keys) > 0 or len(seam_keys) > 0):
        # Get edge vertex pairs via foreach_get
        edge_verts = np.zeros(n_edges * 2, dtype=np.int32)
        me.edges.foreach_get('vertices', edge_verts)
        edge_verts = edge_verts.reshape(-1, 2)

        v0 = np.minimum(edge_verts[:, 0], edge_verts[:, 1])
        v1 = np.maximum(edge_verts[:, 0], edge_verts[:, 1])
        edge_keys = v0.astype(np.int64) * max_v + v1.astype(np.int64)

        if len(seam_keys) > 0:
            seam_arr = np.isin(edge_keys, seam_keys).astype(bool)
            me.edges.foreach_set('use_seam', seam_arr)

        if len(sharp_keys) > 0:
            sharp_arr = np.isin(edge_keys, sharp_keys).astype(bool)
            sharp_attr = me.attributes.get('sharp_edge')
            if sharp_attr is None:
                sharp_attr = me.attributes.new(
                    name='sharp_edge', type='BOOLEAN', domain='EDGE')
            sharp_attr.data.foreach_set('value', sharp_arr)

    if norms is not None and len(norms) > 0:
        me.normals_split_custom_set(norms)

    _apply_uv_layers(me, uvs)

    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode=prev_mode)

    if _debug_timing:
        t2 = time.time()
        _phase2_times["add_to_bm"] += t1 - t0
        _phase2_times["bpy_update"] += t2 - t1

    return mesh.matrix


def _compute_edge_attributes(mesh):
    """Vectorized computation of seam and sharp edge sets.

    Returns (sharp_keys_packed, seam_keys_packed, max_v) where keys are
    int64-packed edge keys: min_v * max_v + max_v_of_edge.
    Caller uses these to look up Blender edges efficiently.
    """
    margin = 0.02
    faces = mesh.faces          # (T, 3) int32
    loop_norms = mesh.get_loop_norms()  # (T*3, 3) float32
    verts = mesh.verts          # (V, 3) float32
    batches = mesh.tri_batches  # (T,) int32

    T = len(faces)
    if T == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64), 1

    max_v = int(np.max(faces)) + 1

    # Build half-edge table: 3 edges per face
    fi = np.arange(T, dtype=np.int32)

    va = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2]])
    vb = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0]])
    corner_a = np.concatenate([fi * 3, fi * 3 + 1, fi * 3 + 2])
    corner_b = np.concatenate([fi * 3 + 1, fi * 3 + 2, fi * 3])
    face_of = np.concatenate([fi, fi, fi])

    edge_min = np.minimum(va, vb)
    edge_max = np.maximum(va, vb)
    edge_keys = edge_min.astype(np.int64) * max_v + edge_max.astype(np.int64)

    unique_keys, inverse, counts = np.unique(
        edge_keys, return_inverse=True, return_counts=True)

    # Interior edges only (shared by exactly 2 faces)
    interior_idx = np.where(counts == 2)[0]
    if len(interior_idx) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64), max_v

    group_starts = np.zeros(len(unique_keys) + 1, dtype=np.int64)
    np.cumsum(counts, out=group_starts[1:])
    order = np.argsort(edge_keys)

    int_starts = group_starts[interior_idx]
    he0 = order[int_starts]
    he1 = order[int_starts + 1]

    # --- Normal discontinuity test (cross-batch edges only) ---
    cross_batch = batches[face_of[he0]] != batches[face_of[he1]]
    int_edge_keys = unique_keys[interior_idx]

    ev0 = edge_min[he0]
    ev1 = edge_max[he0]

    # Map half-edge corners to sorted edge vertices
    he0_a_is_ev0 = (va[he0] == ev0)
    n0_ev0_c = np.where(he0_a_is_ev0, corner_a[he0], corner_b[he0])
    n0_ev1_c = np.where(he0_a_is_ev0, corner_b[he0], corner_a[he0])
    he1_a_is_ev0 = (va[he1] == ev0)
    n1_ev0_c = np.where(he1_a_is_ev0, corner_a[he1], corner_b[he1])
    n1_ev1_c = np.where(he1_a_is_ev0, corner_b[he1], corner_a[he1])

    # Get normal vectors (N_interior, 3)
    norm0_ev0 = loop_norms[n0_ev0_c]
    norm1_ev0 = loop_norms[n1_ev0_c]
    norm0_ev1 = loop_norms[n0_ev1_c]
    norm1_ev1 = loop_norms[n1_ev1_c]

    # Edge direction as projection plane normal
    plane = verts[ev0] - verts[ev1]
    plane_len = np.linalg.norm(plane, axis=1, keepdims=True)
    plane = plane / np.maximum(plane_len, 1e-12)

    def _batch_prjtest(plane, n0, n1):
        dot0 = np.sum(plane * n0, axis=1, keepdims=True)
        prj0 = n0 - plane * dot0
        prj0 = prj0 / np.maximum(np.linalg.norm(prj0, axis=1, keepdims=True), 1e-12)
        dot1 = np.sum(plane * n1, axis=1, keepdims=True)
        prj1 = n1 - plane * dot1
        prj1 = prj1 / np.maximum(np.linalg.norm(prj1, axis=1, keepdims=True), 1e-12)
        return np.sum(prj0 * prj1, axis=1) < (1.0 - margin)

    sharp_ev0 = _batch_prjtest(plane, norm0_ev0, norm1_ev0)
    sharp_ev1 = _batch_prjtest(plane, norm0_ev1, norm1_ev1)
    # Sharp/seam only between different OCC faces WITH normal discontinuity.
    # Smooth boundaries (e.g. cylinder halves) get neither sharp nor seam.
    discontinuous = cross_batch & sharp_ev0 & sharp_ev1
    sharp_keys = int_edge_keys[discontinuous]
    seam_keys = sharp_keys  # seams only where normals actually split

    # --- Parametric closure seams (closed cylinders/cones/tori/splines) ---
    # A closed face has NO sharp edge along its parametric seam, so unwrap
    # has nowhere to cut and produces a degenerate result. Detection: OCC
    # duplicates the seam vertices in parameter space (u=0 and u=period);
    # the position-based vertex fuse welds them, but the per-corner UV
    # snapshot still disagrees across the edge. So an interior edge WITHIN
    # one OCC face whose corner UVs differ at BOTH endpoints lies on the
    # closure (the both-endpoints rule keeps collapsed poles, where every
    # touching edge has one discontinuous corner, out of the seam set).
    # Marked as UV seam only, never sharp: shading stays smooth.
    if _uv_options.get("split_closed", True):
        loop_uvs = None
        get_uvs = getattr(mesh, "get_loop_uvs", None)
        if get_uvs is not None:
            loop_uvs = get_uvs()
        if loop_uvs is not None and len(loop_uvs) == T * 3:
            same_batch = ~cross_batch
            jump0 = (np.abs(loop_uvs[n0_ev0_c] - loop_uvs[n1_ev0_c]).sum(axis=1)
                     > 1e-5)
            jump1 = (np.abs(loop_uvs[n0_ev1_c] - loop_uvs[n1_ev1_c]).sum(axis=1)
                     > 1e-5)
            closure_keys = int_edge_keys[same_batch & jump0 & jump1]
            if len(closure_keys):
                seam_keys = np.unique(
                    np.concatenate([seam_keys, closure_keys]))

        # Multi-face closed regions: a hole modeled as two half-cylinders
        # has no closed face, but the smooth-joined pair still forms a
        # closed tube that unwrap cannot flatten. Find smooth-connected
        # triangle regions that are not topological disks (Euler
        # characteristic V - E + F != 1: tubes, rings, sphere halves) and
        # seam their internal CAD-face boundaries, splitting them into
        # flattenable patches. UV seam only: shading is untouched.
        smooth_pair = ~np.isin(int_edge_keys, seam_keys)
        a = face_of[he0][smooth_pair]
        b = face_of[he1][smooth_pair]
        if len(a):
            # Connected components via label propagation + pointer jumping
            labels = np.arange(T, dtype=np.int64)
            while True:
                prev = labels
                m = np.minimum(labels[a], labels[b])
                labels = labels.copy()
                np.minimum.at(labels, a, m)
                np.minimum.at(labels, b, m)
                labels = labels[labels]
                if np.array_equal(labels, prev):
                    break
            # Per-component V, E, F (edge ids from the earlier unique():
            # packing with small index domains cannot overflow int64)
            n_unique_edges = np.int64(len(unique_keys))
            comp_of_he = labels[face_of]
            cv = np.unique(np.repeat(labels, 3) * np.int64(max_v)
                           + faces.ravel().astype(np.int64))
            v_ids, v_counts = np.unique(cv // np.int64(max_v),
                                        return_counts=True)
            ce = np.unique(comp_of_he * n_unique_edges + inverse)
            e_ids, e_counts = np.unique(ce // n_unique_edges,
                                        return_counts=True)
            f_ids, f_counts = np.unique(labels, return_counts=True)
            # v_ids/e_ids/f_ids are identical sorted component id sets
            chi = (v_counts.astype(np.int64) - e_counts.astype(np.int64)
                   + f_counts.astype(np.int64))
            bad_ids = v_ids[chi != 1]
            if len(bad_ids):
                cand = smooth_pair & cross_batch
                edge_comp = labels[face_of[he0]]
                extra = int_edge_keys[cand & np.isin(edge_comp, bad_ids)]
                if len(extra):
                    seam_keys = np.unique(
                        np.concatenate([seam_keys, extra]))

    return sharp_keys, seam_keys, max_v


def _apply_trimesh(obj, mesh, colors, mat_names, norms, uvs,
                   vcol_name, build_materials):
    """Original bmesh path for TriMesh objects."""
    if _debug_timing:
        print(f"[bm] {len(mesh.verts)}", end="")
        t0 = time.time()
    bm = bmesh.new()
    mesh.add_to_bm(bm, edges_as_seams=True, discontinuity_as_sharp=True)
    if _debug_timing:
        t1 = time.time()
    bpy_update_object_data(
        obj.data, bm, vcol_name, colors, uvs, norms, mat_names,
        build_materials=build_materials,
    )
    if _debug_timing:
        t2 = time.time()
        _phase2_times["add_to_bm"] += t1 - t0
        _phase2_times["bpy_update"] += t2 - t1

    return mesh.matrix


def build_mesh(step_reader, obj, shp, lind, angd, vcol_name="Colors"):
    hacks = set([])
    if _get_addon_prefs().hack_skip_zero_solids:
        hacks.add("skip_solids")

    mesh, colors, mat_names, norms, uvs = precompute_mesh_data(
        step_reader, shp, lind, angd, hacks)

    return apply_mesh_to_blender(
        obj, mesh, colors, mat_names, norms, uvs, vcol_name,
        build_materials=_get_addon_prefs().build_materials,
    )


def build_nurbs(step_reader, shp, name):
    nurbs_data = step_reader.build_nurbs(shp)
    debug_faces = False
    if debug_faces:
        obj = create_new_obj_with_mesh(name)
        bm = bmesh.new()
        for nb in nurbs_data:
            nb_u = nb.uv_points
            uw, vw = len(nb_u), len(nb_u[0])
            for u in range(uw - 1):
                nb_v0 = nb_u[u]
                nb_v1 = nb_u[u + 1]
                for v in range(vw - 1):
                    a = bm.verts.new(nb_v0[v].location())
                    b = bm.verts.new(nb_v0[v + 1].location())
                    c = bm.verts.new(nb_v1[v + 1].location())
                    d = bm.verts.new(nb_v1[v].location())
                    bm.faces.new((d, c, b, a))
        prev_mode = bpy.context.object.mode
        bpy.ops.object.mode_set(mode="OBJECT")
        bm.to_mesh(obj.data)
        bpy.ops.object.mode_set(mode=prev_mode)
        # obj.display_type = 'WIRE'
        return obj
    else:
        blender_nurbs = []
        for nb in nurbs_data:
            surface_data = bpy.data.curves.new("wook", "SURFACE")
            surface_data.dimensions = "3D"

            upoints = nb.uv_points

            usize, vsize = len(upoints), len(upoints[0])

            splines = []
            for v in range(usize):
                spline = surface_data.splines.new(type="NURBS")
                spline.points.add(vsize - 1)
                splines.append(spline)

            for ui, vpoints in enumerate(upoints):
                for vi, p in enumerate(vpoints):
                    # points have weight attribute
                    splines[ui].points[vi].co = p.as_vector()

            blender_nurbs.append(surface_data)

        # print(dir(nurbs[0].splines[0])) =>
        # 'bezier_points', 'bl_rna', 'calc_length', 'character_index', 'hide', 'material_index',
        # 'order_u', 'order_v', 'point_count_u', 'point_count_v', 'points', 'radius_interpolation',
        # 'resolution_u', 'resolution_v', 'rna_type', 'tilt_interpolation', 'type', 'use_bezier_u',
        # 'use_bezier_v', 'use_cyclic_u', 'use_cyclic_v', 'use_endpoint_u',
        # 'use_endpoint_v', 'use_smooth'
        created_objs = []
        for ni, n in enumerate(blender_nurbs):
            occ_nurb = nurbs_data[ni]
            surface_object = bpy.data.objects.new(name, n)
            bpy.context.collection.objects.link(surface_object)
            for s in surface_object.data.splines:
                for p in s.points:
                    p.select = True

            bpy.context.view_layer.objects.active = surface_object
            prev_mode = bpy.context.object.mode
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.curve.make_segment()
            bpy.ops.object.mode_set(mode=prev_mode)
            created_objs.append(surface_object)

        for obi, ob in enumerate(created_objs):
            occ_nurb = nurbs_data[obi]
            for s in ob.data.splines:
                s.use_endpoint_u = True
                s.use_endpoint_v = True
                # s.use_endpoint_u = occ_nurb.u_closed
                # s.use_endpoint_v = occ_nurb.v_closed
                # s.use_cyclic_u = occ_nurb.u_periodic
                # s.use_cyclic_v = occ_nurb.v_periodic
                s.order_u = occ_nurb.u_degree + 1
                s.order_v = occ_nurb.v_degree + 1
                # print(s.order_u, s.order_v, occ_nurb.u_degree, occ_nurb.v_degree)

        # Join objects
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for o in created_objs:
            o.select_set(True)
        bpy.ops.object.join()
        return bpy.context.view_layer.objects.active


def _show_import_issues_popup(failed_parts, recovered_parts):
    """Show a Blender popup dialog listing parts that had import problems."""

    def draw(self, context):
        layout = self.layout
        if recovered_parts:
            layout.label(text=f"{len(recovered_parts)} part(s) had corrupted geometry and were recovered:")
            col = layout.column(align=True)
            for name in recovered_parts[:20]:
                col.label(text=f"    {name}", icon="FILE_REFRESH")
            if len(recovered_parts) > 20:
                col.label(text=f"    ... and {len(recovered_parts) - 20} more")
            col.label(text="    Recovered parts may have missing faces or appearance.", icon="INFO")
            layout.separator()
        if failed_parts:
            layout.label(text=f"{len(failed_parts)} part(s) produced no geometry:")
            col = layout.column(align=True)
            for name in failed_parts[:20]:
                col.label(text=f"    {name}", icon="ERROR")
            if len(failed_parts) > 20:
                col.label(text=f"    ... and {len(failed_parts) - 20} more")
            col.label(text="    Usually caused by unresolved references in the STEP file.", icon="INFO")

    icon = "ERROR" if failed_parts else "INFO"
    bpy.context.window_manager.popup_menu(draw, title="STEPper NEXT Import Warning", icon=icon)


# ---------------------------------------------------------------------------
# Material Database helpers
# ---------------------------------------------------------------------------

_MATDB_TEXT_NAME = "STEPper_MaterialDB"
_ADDON_DIR = os.path.dirname(os.path.realpath(__file__))


def _get_matdb_dir():
    """The folder holding the material databases.

    The addon's own MaterialDB folder by default, and a folder the user
    names in preferences when they set one. A database inside the addon is
    wiped by every reinstall and cannot be shared between machines or with a
    team, which is exactly what a material library is for."""
    custom = ""
    try:
        custom = (_get_addon_prefs().matdb_dir or "").strip()
    except Exception:
        # Called from an enum callback before the preferences exist.
        custom = ""
    if custom:
        d = bpy.path.abspath(custom)
        # A path that is not there yet is created, the same as the built-in
        # one; a path that cannot be made is reported once and falls back,
        # because losing the databases is worse than ignoring the setting.
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError as exc:
            global _matdb_dir_warned
            if _matdb_dir_warned != d:
                _matdb_dir_warned = d
                print("STEPper NEXT: material database folder %r is "
                      "unusable (%s); using the addon's own folder" % (d, exc))

    d = os.path.join(_ADDON_DIR, "MaterialDB")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        # Read-only install (system-wide): callers treat a missing dir as
        # "no databases"; raising here would crash the enum callback
        pass
    return d


_matdb_dir_warned = ""


def _sanitize_db_name(name):
    """Strip path separators and other filesystem-hostile characters from a
    user-typed database name."""
    cleaned = "".join(c for c in name if c not in '<>:"/\\|?*').strip(" .")
    return cleaned


def _list_matdb_files():
    """Return list of (filename_without_ext, full_path) for all .blend files in MaterialDB/."""
    d = _get_matdb_dir()
    result = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".blend"):
                result.append((f[:-6], os.path.join(d, f)))
    return result


def _matdb_enum_items(self, context):
    """Dynamic enum items for material database selection."""
    global _matdb_enum_cache
    items = [("NONE", "None", "Do not use a material database")]
    for name, path in _list_matdb_files():
        items.append((name, name, f"Use material database: {name}"))
    # Blender requires the returned strings to stay referenced from Python;
    # module-level cache prevents garbage values in the dropdown
    _matdb_enum_cache = items
    return items


_matdb_enum_cache = []


def _get_active_matdb_path(db_name=None):
    """Return the full path of the active material database, or empty string."""
    if db_name is None:
        db_name = _get_addon_prefs().active_matdb
    if not db_name or db_name == "NONE":
        return ""
    path = os.path.join(_get_matdb_dir(), db_name + ".blend")
    return path if os.path.isfile(path) else ""


def _write_material_database(filepath, mappings_dict):
    """Write material mappings and replacement materials to a .blend database file."""
    # Remove any leftover temporary text datablock
    while True:
        text = bpy.data.texts.get(_MATDB_TEXT_NAME)
        if text:
            bpy.data.texts.remove(text)
        else:
            break

    text = bpy.data.texts.new(_MATDB_TEXT_NAME)
    text.write(json.dumps(mappings_dict, indent=2))

    # Collect datablocks to write: the text + all replacement materials.
    # Linked/library materials (e.g. from asset browser) cannot be written
    # directly, so make temporary local copies for those.
    datablocks = {text}
    temp_copies = []
    for replacement_name in set(mappings_dict.values()):
        mat = bpy.data.materials.get(replacement_name)
        if not mat:
            print(f"STEPper MatDB: Material '{replacement_name}' not found, skipping")
            continue
        if mat.library or mat.override_library:
            # Create a full local copy so it can be written to the database
            local_copy = mat.copy()
            local_copy.name = replacement_name  # keep the expected name
            datablocks.add(local_copy)
            temp_copies.append(local_copy)
        else:
            datablocks.add(mat)

    print(f"STEPper MatDB: Writing {len(datablocks)} datablocks to {filepath}")
    bpy.data.libraries.write(filepath, datablocks, fake_user=True)

    # Clean up temporary text datablock and any temporary material copies
    bpy.data.texts.remove(text)
    for tmp in temp_copies:
        bpy.data.materials.remove(tmp)


def _read_matdb_mappings(filepath):
    """Read ONLY the JSON mappings from a database .blend file.

    Does NOT append materials.  Returns dict {original_name: replacement_name}.
    """
    abs_path = bpy.path.abspath(filepath)
    if not abs_path or not os.path.isfile(abs_path):
        return {}

    # Clean up any leftover text block from a previous read
    while True:
        text = bpy.data.texts.get(_MATDB_TEXT_NAME)
        if text:
            bpy.data.texts.remove(text)
        else:
            break

    # Append only the text datablock
    with bpy.data.libraries.load(abs_path, link=False) as (data_from, data_to):
        if _MATDB_TEXT_NAME in data_from.texts:
            data_to.texts = [_MATDB_TEXT_NAME]

    mappings = {}
    text = bpy.data.texts.get(_MATDB_TEXT_NAME)
    if text:
        try:
            mappings = json.loads(text.as_string())
        except json.JSONDecodeError:
            print("STEPper MatDB: Invalid JSON in database file")
        bpy.data.texts.remove(text)
    else:
        print("STEPper MatDB: No mapping text block found in database file")

    return mappings


def _append_matdb_materials(filepath):
    """Append all materials from a database .blend into the current file.

    Materials that already exist locally (by name) are skipped to avoid
    duplicates.  Returns the number of materials appended.
    """
    abs_path = bpy.path.abspath(filepath)
    if not abs_path or not os.path.isfile(abs_path):
        return 0

    with bpy.data.libraries.load(abs_path, link=False) as (data_from, data_to):
        to_append = [m for m in data_from.materials
                     if m not in bpy.data.materials]
        data_to.materials = to_append

    count = len(to_append)
    if count:
        print(f"STEPper MatDB: Appended {count} material(s) from database")
    return count


def _scan_scene_materials():
    """Scan scene for STEP objects and determine material mappings.

    For each original STEP material name, find what replacement material is
    currently assigned.  When an original name maps to multiple different
    replacements, pick the one with the highest instance count.

    Returns dict {original_name: replacement_name}.
    """
    original_to_replacements = {}  # {orig: Counter({repl: count})}

    for obj in bpy.data.objects:
        if obj.data is None or not hasattr(obj.data, 'materials'):
            continue

        step_mats_json = obj.get("STEP_materials")
        if step_mats_json:
            try:
                original_names = json.loads(step_mats_json)
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            # Backward compat: treat current materials as identity mapping
            original_names = [m.name for m in obj.data.materials if m]

        current_mats = [m.name if m else None for m in obj.data.materials]

        for i, orig_name in enumerate(original_names):
            if i < len(current_mats) and current_mats[i]:
                repl_name = current_mats[i]
                if orig_name not in original_to_replacements:
                    original_to_replacements[orig_name] = Counter()
                original_to_replacements[orig_name][repl_name] += 1

    # Resolve conflicts: majority wins
    mappings = {}
    for orig_name, counter in original_to_replacements.items():
        mappings[orig_name] = counter.most_common(1)[0][0]

    return mappings


def _ensure_matdb_materials(db_path):
    """Ensure all materials from a database .blend exist locally.

    Call this before any operation that needs to reference database materials.
    Returns the mappings dict.
    """
    if not db_path:
        return {}
    _append_matdb_materials(db_path)
    return _read_matdb_mappings(db_path)


def _apply_matdb_to_objects(objects, mappings):
    """Replace STEP materials on *objects* according to *mappings* dict.

    Uses the STEP_materials custom property to determine original material
    names so replacements work even when materials were already swapped by
    a previous database apply.

    Returns the number of material slots replaced.
    """
    if not mappings:
        return 0

    print(f"Applying material database ({len(mappings)} mappings)")

    replaced = 0
    processed_meshes = set()
    for obj in objects:
        if obj.data is None or not hasattr(obj.data, 'materials'):
            continue
        mesh = obj.data
        if mesh in processed_meshes:
            continue
        processed_meshes.add(mesh)

        # Read original STEP material names from the custom property
        step_mats_json = obj.get("STEP_materials")
        if step_mats_json:
            try:
                original_names = json.loads(step_mats_json)
            except (json.JSONDecodeError, TypeError):
                original_names = None
        else:
            original_names = None

        for slot_idx in range(len(mesh.materials)):
            # Determine the original STEP name for this slot
            if original_names and slot_idx < len(original_names):
                orig_name = original_names[slot_idx]
            else:
                # Fallback: use current material name
                mat = mesh.materials[slot_idx]
                orig_name = mat.name if mat else None

            if orig_name and orig_name in mappings:
                replacement_name = mappings[orig_name]
                replacement_mat = bpy.data.materials.get(replacement_name)
                current_mat = mesh.materials[slot_idx]
                if replacement_mat and replacement_mat != current_mat:
                    mesh.materials[slot_idx] = replacement_mat
                    replaced += 1

    return replaced


def _cleanup_unused_step_materials(known_names=None):
    """Remove zero-user materials that were generated by STEP import.

    known_names: additional material names known to come from the import
    (e.g. the original-name keys of the active matdb mappings); CAD color
    names like "GRAY" don't carry the STEP_ prefix.
    """
    removed = 0
    # Iterate over a snapshot since we're modifying the collection
    for mat in list(bpy.data.materials):
        if mat.users != 0:
            continue
        if mat.name.startswith("STEP_") or (known_names
                                            and mat.name in known_names):
            bpy.data.materials.remove(mat)
            removed += 1
    if removed:
        print(f"STEPper MatDB: Removed {removed} unused material(s)")
    return removed


def _cache_drop(filepath):
    """Forget a file so the next read comes off disk. A refresh exists to
    pick up a changed file, and the cache would hand back the old one."""
    global_file_cache.pop(filepath, None)
    global_file_cache_meta.pop(filepath, None)


def import_defaults():
    """The import settings load_step would use right now, as keyword
    arguments. Used when a blend predates the import registry and a refresh
    has nothing recorded to reproduce."""
    prefs = _get_addon_prefs()
    lin, ang = calculate_detail_level(bpy.context.scene.stepper.detail_level)
    return {
        "up_as": prefs.preferred_up_axis,
        "htypes": prefs.preferred_hierarchy,
        "apply_scale": True,
        "lin_deflection": lin,
        "ang_deflection": ang,
    }


def _own_collection(name, filepath, role):
    """A new collection that says which import made it, and what for.

    Refreshing a file has to take its own collections away and leave the
    user's alone, and there is no way to tell them apart after the fact:
    names collide, and a user is free to move, rename or nest anything. So
    the importer marks its own as it creates them.
    """
    col = bpy.data.collections.new(name)
    col["STEP_file"] = filepath
    col["STEP_role"] = role
    return col


def _split_solids(entries, step_reader):
    """One entry per BODY, for shapes that hold more than one.

    A multibody part exports as ONE product whose shape is a compound of
    several solids, and there is no assembly structure to tell them apart.
    the importer builds one object with every body merged into it. This
    hands each body back as its own entry so it becomes its own object.

    Only a shape holding two or more bodies is touched; a single-body shape
    keeps its sub-index of None and is byte-for-byte the same import as
    before. Solids are preferred, and free shells are used only where a
    shape has no solids at all, which is the surface-body case (the option's
    own description names solids, shells and surfaces).

    Identity is ShapeKey, not an OCCT map: per-entity handle lookups are
    broken in OCP 7.9.3.1, which is why curves.py does the same.
    """
    from OCP.TopAbs import TopAbs_SOLID, TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from .ocp_utils import ShapeKey

    def inherit_colour(parent, body):
        """A body carries its product's colour. Its FACES are the same OCC
        faces the whole shape had, so per-face colour resolves unchanged; it
        is only the shape-level fallback that has to be carried across."""
        pkey, bkey = ShapeKey(parent), ShapeKey(body)
        if pkey in step_reader.face_colors:
            step_reader.face_colors.setdefault(
                bkey, step_reader.face_colors[pkey])
        prio = getattr(step_reader, "face_color_priority", None)
        if prio is not None and pkey in prio:
            prio.setdefault(bkey, prio[pkey])

    def bodies(shape, kind):
        found, seen = [], set()
        ex = TopExp_Explorer(shape, kind)
        while ex.More():
            cur = ex.Current()
            key = ShapeKey(cur)
            if key not in seen:
                seen.add(key)
                found.append(cur)
            ex.Next()
        return found

    out = []
    split_shapes = 0
    for shp, idx, _sub in entries:
        parts = []
        if shp is not None:
            parts = bodies(shp, TopAbs_SOLID)
            if len(parts) < 2 and not parts:
                parts = bodies(shp, TopAbs_SHELL)
        if len(parts) < 2:
            out.append((shp, idx, None))
            continue
        split_shapes += 1
        for k, body in enumerate(parts):
            inherit_colour(shp, body)
            out.append((body, idx, k))

    if split_shapes:
        print("Separate solids: %d shape(s) became %d bodies"
              % (split_shapes, len(out) - (len(entries) - split_shapes)))
    return out


def load_step(
    context,
    filepath,
    custom_scale=None,
    lin_deflection=0.8,
    ang_deflection=0.5,
    # merge_distance=0.001,
    up_as="Y",
    htypes="TREE",
    apply_scale=True,
    material_database="NONE",
    deflection_spec=None,
    skip_construction=False,
    uv_mode="SURFACE",
    uv_normalize=True,
    uv_split_closed=True,
    box_uv_scale=1.0,
    import_curves=False,
    eng_materials=False,
    group_in_collection=False,
    separate_solids=False,
):
    from . import importer

    global _debug_timing
    _debug_timing = _get_addon_prefs().debug_timing

    # One "UVMap" layer; its content is chosen by uv_mode. UNWRAP writes
    # the surface UVs first (fallback content) and unwraps in place.
    _uv_options["mode"] = uv_mode
    _uv_options["surface"] = uv_mode in ("SURFACE", "UNWRAP")
    _uv_options["unwrap"] = uv_mode == "UNWRAP"
    _uv_options["box"] = uv_mode == "BOX"
    _uv_options["normalize"] = uv_normalize
    _uv_options["split_closed"] = uv_split_closed
    _uv_options["box_scale"] = box_uv_scale

    (hierarchy_flat, hierarchy_tree, hierarchy_empties,
     hierarchy_instances) = choose_hierarchy_types(htypes)

    # splitext, not split("."): multi-dot names ("part.rev2.step") must keep
    # their dots, and extensionless names must not collapse to ""
    filename = os.path.splitext(ntpath.basename(filepath))[0] or "STEP"

    # Everything this import creates goes under ONE collection, so that a
    # second import does not interleave with the first and so the whole
    # assembly can be moved, hidden or deleted as a unit. Off by default:
    # the scene collection is where every earlier version put things.
    _dest_col = []

    def destination():
        if not _dest_col:
            if group_in_collection:
                col = _own_collection(filename, filepath, "wrapper")
                bpy.context.scene.collection.children.link(col)
            else:
                col = bpy.context.scene.collection
            _dest_col.append(col)
        return _dest_col[0]

    # Curves are their own collection, because a sketch is not a part: it is
    # reference geometry a viewport wants to switch off in one gesture.
    # Created on demand, so an import with no free edges makes no empty
    # collection.
    curve_objs = set()
    _curve_col = []

    def curves_collection():
        if not _curve_col:
            col = _own_collection("Cad Curves", filepath, "curves")
            destination().children.link(col)
            _curve_col.append(col)
        return _curve_col[0]

    def link_created(obj, collection):
        """Links one created object where it belongs: a curve into the Cad
        Curves collection, everything else where the hierarchy says. Parenting
        and transforms are untouched. Collection membership is orthogonal to
        both, so a curve still rides its assembly."""
        if obj in curve_objs:
            curves_collection().objects.link(obj)
        else:
            collection.objects.link(obj)

    skip_prefixes = ()
    if skip_construction:
        skip_prefixes = tuple(
            _get_addon_prefs().construction_filter_names.split(","))

    cached = _cache_get(filepath)
    if cached is not None and cached._filter_key != frozenset(
            p.strip().lower() for p in skip_prefixes if p.strip()):
        cached = None  # different construction filter → re-read
    if cached is None:
        from . import formats
        try:
            parse_t0 = time.time()
            step_reader = formats.make_reader(
                filepath, skip_name_prefixes=skip_prefixes)
            _record_parse_calibration(step_reader, time.time() - parse_t0)
            _cache_put(filepath, step_reader)
        except AssertionError as e:
            print(e)
            return False
    else:
        step_reader = cached
        print("Loaded file from cache")

    tree = step_reader.tree
    scale = step_reader.scale

    # Resolve deflection spec now that the file's unit scale is known
    # (physical mode: same real-world deflection regardless of file units).
    tessellation_relative = bool(
        deflection_spec and deflection_spec.get("mode") == "relative")
    if deflection_spec is not None:
        lin_deflection, ang_deflection = import_ui.resolve_deflections(
            deflection_spec, step_reader.scale, calculate_detail_level)
        unit = "(relative factor)" if tessellation_relative else "file units"
        print(f"Deflection resolved: {lin_deflection:.4f} {unit} "
              f"/ {ang_deflection:.3f} rad")

    if custom_scale is not None:
        scale = custom_scale

    # divide by Blender unit length
    scale /= context.scene.unit_settings.scale_length
    print("Current Blender scale set at:", context.scene.unit_settings.scale_length)

    # BoxUV tile size is specified in world units; meshes are built in file
    # units, so the apply path needs the conversion factor.
    _uv_options["unit_scale"] = scale

    # SurfaceUV mode for this import: 0-1 normalized (default) or scaled to
    # real-world scene units for consistent texel density across parts.
    step_reader.uv_world_scale = None if uv_normalize else scale

    wm = bpy.context.window_manager

    created_objs = []
    created_names = {}
    created_uuid = {}

    # traverse shapes, render in "face" mode
    start_time = time.time()
    # Every entry is (shape, node index, sub-index). The sub-index is None
    # for a shape taken whole and 0..n-1 for one body of a shape that was
    # separated, and it is what keeps the mesh cache, the object names and
    # the instancing apart afterwards.
    all_shapes = [(shp, idx, None) for shp, idx in tree.get_shapes()]
    if separate_solids:
        all_shapes = _split_solids(all_shapes, step_reader)
    total = len(all_shapes)

    # Tessellation is done on-demand inside build_trimesh (per shape).
    # Pre-tessellation was removed: the threading it used caused race
    # conditions leading to costly [re-tess] retessellations later.

    # Eagerly build recovery compounds (only needed for corrupt STEP files
    # with unresolved references).  Running it here makes the cost visible
    # instead of hiding it inside the first build_trimesh call.
    if step_reader.import_problems.get("Unresolved refs", 0) > 0 or True:
        # Always try: the reader checks internally if recovery is needed
        rec_start = time.time()
        step_reader._build_recovery_compound()
        rec_dt = time.time() - rec_start
        if rec_dt > 0.5:
            n_compounds = len(step_reader._recovery_compounds) if step_reader._recovery_compounds else 0
            print(f"\n--- Recovery compounds built in {rec_dt:.2f}s ({n_compounds} compounds) ---")

    # Pre-compute mesh data for all unique shapes before the bpy loop.
    # This lets us batch-create materials upfront and keeps the Phase 2
    # loop focused on the fast bmesh/bpy work.
    _reset_phase2_times()
    hacks = set()
    if _get_addon_prefs().hack_skip_zero_solids:
        hacks.add("skip_solids")
    build_materials = _get_addon_prefs().build_materials

    def _variant_name(tag, node, sub_index=None):
        """Mesh dedup key: shape tag, plus the instance color override when
        present (instances with different override colors can't share mesh
        data since materials are baked per face), plus which body of the
        shape this is when the shape was separated. The bodies share a tag
        and would otherwise all collapse onto the first one's mesh."""
        base = "tt_" + repr(tag)
        if node.color_override is not None:
            base += "|oc" + repr(node.color_override)
        if sub_index is not None:
            base += "|b" + repr(sub_index)
        return base

    # Identify unique shapes (first occurrence per variant)
    unique_shapes = {}  # variant_name -> (shp, part_name, color_override)
    for shp, node_index, sub_index in all_shapes:
        if shp is None:
            continue
        node = tree.nodes[node_index]
        _, _, tag, part_name, _, _, _ = node.get_values()
        shape_name = _variant_name(tag, node, sub_index)
        if shape_name not in unique_shapes:
            unique_shapes[shape_name] = (shp, part_name, node.color_override)

    precomputed = {}  # shape_name -> (mesh, colors, mat_names, norms, uvs)
    n_unique = len(unique_shapes)
    if n_unique > 0:
        print(f"\n--- Phase 1/3: Pre-computing {n_unique} unique meshes ---")
        precomp_start = time.time()

        last_pct_10 = 0
        for si, (sname, (shp, part_name, color_override)) in enumerate(unique_shapes.items()):
            try:
                if _debug_timing:
                    t_shape = time.time()
                precomputed[sname] = precompute_mesh_data(
                    step_reader, shp, lin_deflection, ang_deflection, hacks,
                    part_name=part_name, fallback_color=color_override,
                    relative=tessellation_relative)
                if _debug_timing:
                    dt_shape = time.time() - t_shape
                    if dt_shape > 2.0:
                        pname = part_name or sname
                        print(f"\n  [{pname}: {dt_shape:.1f}s]", end="", flush=True)
            except Exception as e:
                print(f"\nWarning: precompute failed for {sname} ({part_name}): {e}")

            pct_10 = (100 * (si + 1) // n_unique) // 10 * 10
            if pct_10 > last_pct_10:
                # Newline-terminated so the background reader thread sees
                # each percentage immediately (end="" would delay every
                # update until the next print's leading "\n")
                print(f"\n  Phase 1: {pct_10}%", flush=True)
                last_pct_10 = pct_10

        print(f"\nPre-compute done in {time.time() - precomp_start:.2f}s")

    # Pre-create all materials so bpy_update_object_data doesn't do it per-face
    if build_materials and precomputed:
        all_mat_info = {}  # mat_name -> color
        for mesh, colors, mat_names, norms, uvs in precomputed.values():
            for ci, mname in enumerate(mat_names):
                if mname:
                    if mname not in all_mat_info:
                        c = colors[ci]
                        all_mat_info[mname] = tuple(float(x) for x in c)
                else:
                    c = colors[ci]
                    col = tuple(float(x) for x in c) if ci < len(colors) and c[0] >= 0.0 else (0.5, 0.5, 0.5)
                    col = _quantize_color(col)
                    auto_name = "STEP_" + "".join("{0:0{1}x}".format(int(col[i] * 255), 2) for i in range(3))
                    if auto_name not in all_mat_info:
                        all_mat_info[auto_name] = col
        for mname, col in all_mat_info.items():
            if mname not in bpy.data.materials:
                add_material(mname, col, link_vertex_color=False)

    print(f"\n--- Phase 2/3: Building {total} Blender objects ---")
    instance_prototypes = {}  # variant_name -> prototype mesh object (instances mode)
    _no_curve_shapes = set()  # variants with no free edges (curves mode)
    wm.progress_begin(0, total)
    for i, (shp, node_index, sub_index) in enumerate(all_shapes):
        node = tree.nodes[node_index]
        parent_uuid, self_uuid, tag, name, _, local_t, global_t = node.get_values()

        if name == "root":
            name = filename + ".empties"
        if sub_index is not None:
            name = "%s.body%03d" % (name, sub_index + 1)

        shape_name = _variant_name(tag, node, sub_index)
        wm.progress_update(i)
        obj = None

        # Shape found in leaf
        if shp:
            if _debug_timing:
                print("\nBuilding ({}/{}): {} ".format(i + 1, total, name), end="", flush=True)
                print("[T" + repr(shp.ShapeType()) + "]", end="", flush=True)

            # If object already built, just copy it using linked mesh data
            if shape_name in created_names and created_names[shape_name] is None:
                # Earlier occurrence produced no geometry and was removed
                # (skip_empty_objects), so don't rebuild and re-report it for
                # every further occurrence of the same shape.
                pass
            elif shape_name in created_names:
                if _debug_timing:
                    print("[Link]", end="", flush=True)

                if hierarchy_instances:
                    # Occurrence becomes a collection-instance empty later;
                    # just record which prototype it instances.
                    obj = bpy.data.objects.new(name, None)
                    obj.empty_display_size = 0.0001
                    obj["STEP_instance_of"] = shape_name
                    created_objs.append(obj)
                else:
                    source_obj = created_names[shape_name]
                    obj = source_obj.copy()
                    created_objs.append(obj)
            else:
                if _debug_timing:
                    print("[Build]", end="", flush=True)

                obj = create_new_obj_with_mesh(name)

                if shape_name in precomputed:
                    # Use pre-computed data: only bpy/bmesh work on main thread
                    mesh, colors, mat_names, norms, uvs = precomputed[shape_name]
                    apply_mesh_to_blender(
                        obj, mesh, colors, mat_names, norms, uvs,
                        build_materials=build_materials)
                else:
                    # Fallback for shapes that failed precompute
                    build_mesh(step_reader, obj, shp, lin_deflection, ang_deflection)

                # Track parts that produced no geometry
                if obj.data is not None and len(obj.data.vertices) == 0:
                    step_reader.failed_parts.append(name)
                    if _get_addon_prefs().skip_empty_objects:
                        mesh_data = obj.data
                        bpy.data.objects.remove(obj)
                        bpy.data.meshes.remove(mesh_data)
                        obj = None
                        # Remember the shape as known-empty so repeated
                        # occurrences are skipped instead of rebuilt
                        created_names[shape_name] = None

                if obj is not None:
                    created_names[shape_name] = obj
                    # One Blender material per part, named after the CAD
                    # engineering material (AP242/AP214 metadata), if present
                    if eng_materials:
                        _assign_engineering_material(
                            obj, getattr(node, "material", None))
                    if hierarchy_instances:
                        # First occurrence: keep the mesh object as a hidden
                        # prototype; this occurrence becomes an instance empty.
                        proto = obj
                        proto["STEP_tag"] = tag
                        proto["STEP_file"] = filepath
                        proto["STEP_name"] = name
                        proto["STEP_tree_location"] = node_index
                        proto["STEP_applied_scale"] = scale if apply_scale else 0.0
                        if proto.data is not None and proto.data.materials:
                            proto["STEP_materials"] = json.dumps(
                                [m.name if m else "" for m in proto.data.materials])
                        instance_prototypes[shape_name] = proto
                        obj = bpy.data.objects.new(name, None)
                        obj.empty_display_size = 0.0001
                        obj["STEP_instance_of"] = shape_name
                        created_objs.append(obj)
                    else:
                        created_objs.append(obj)

            # Free-edge curves (sketches, construction wires) as curve
            # objects, also for parts whose mesh came out empty (sketch-only
            # parts are exactly the ones that need this).
            if import_curves and not hierarchy_instances:
                ckey = shape_name + "|curves"
                cobj = None
                if ckey in created_names:
                    cobj = created_names[ckey].copy()  # linked curve data
                elif ckey not in _no_curve_shapes:
                    polylines = curves_mod.extract_free_curves(
                        shp, lin_deflection, ang_deflection)
                    if polylines:
                        cobj = curves_mod.build_curve_object(
                            bpy, name + ".curves", polylines)
                        created_names[ckey] = cobj
                    else:
                        _no_curve_shapes.add(ckey)
                if cobj is not None:
                    cobj["STEP_tag"] = tag
                    cobj["STEP_parent"] = parent_uuid
                    cobj["STEP_file"] = filepath
                    cobj["STEP_name"] = name + ".curves"
                    cobj["STEP_tree_location"] = node_index
                    cobj["STEP_applied_scale"] = scale if apply_scale else 0.0
                    curve_objs.add(cobj)
                    created_objs.append(cobj)

        # No shape in leaf, empty creation enabled, do this
        elif hierarchy_empties or hierarchy_instances:
            # Create empty
            obj = bpy.data.objects.new(name, None)
            obj.empty_display_size = 2
            obj.empty_display_type = "PLAIN_AXES"
            created_objs.append(obj)
            # set_obj_matrix_world(obj, global_t)

        # Object has been created
        if obj:
            # assign property to obj
            obj["STEP_tag"] = tag
            obj["STEP_parent"] = parent_uuid
            obj["STEP_uuid"] = self_uuid
            obj["STEP_file"] = filepath
            obj["STEP_name"] = name
            obj["STEP_tree_location"] = node_index
            obj["STEP_applied_scale"] = scale if apply_scale else 0.0
            obj["STEP_import_settings"] = json.dumps({
                "lin_deflection": lin_deflection,
                "ang_deflection": ang_deflection,
                "uv_mode": _uv_options["mode"],
                "uv_normalize": _uv_options["normalize"],
                "uv_split_closed": _uv_options["split_closed"],
                "box_uv_scale": _uv_options["box_scale"],
                "unit_scale": scale,
                "eng_materials": eng_materials,
            })
            # Engineering material metadata (AP242/AP214) as custom
            # properties, regardless of the assignment option
            mat_info = getattr(node, "material", None)
            if mat_info and mat_info.get("name"):
                obj["STEP_material"] = mat_info["name"]
                if mat_info.get("description"):
                    obj["STEP_material_desc"] = mat_info["description"]
                if mat_info.get("density"):
                    obj["STEP_material_density"] = float(mat_info["density"])
            # Store original STEP material names for material database feature
            if obj.data is not None and hasattr(obj.data, 'materials') and obj.data.materials:
                obj["STEP_materials"] = json.dumps([m.name if m else "" for m in obj.data.materials])
            # Children of this node parent to ONE object, so the first
            # body speaks for a separated shape. A node with children is an
            # assembly node and carries no shape of its own, so this only
            # ever arises for a leaf someone has parented to.
            if sub_index in (None, 0):
                created_uuid[self_uuid] = obj

    # assert len(created_objs) == len(shapes_labels)
    if _debug_timing:
        _print_phase2_times()
    print("\n" + repr(step_reader.import_problems))

    # Optional packed angle-based unwrap (unique meshes only; linked
    # copies share the datablock and get it for free). Meshes are still in
    # file units here, so real-world mode converts through unit_scale.
    if _uv_options.get("unwrap"):
        _unwrap_uv_objects(
            created_names.values(),
            world_scale=(None if _uv_options.get("normalize", True)
                         else _uv_options["unit_scale"]))

    # remove all temporary links
    for tobj in created_objs:
        obj_unlink_all(tobj)

    # build flat collection
    if hierarchy_flat:
        flat_collection = _own_collection(filename + ".flat", filepath, "flat")
        destination().children.link(flat_collection)

        created_collections = {}
        for obj in created_objs:
            group_name = obj["STEP_name"]

            # max collection name len = 61
            if len(group_name) > 50:
                group_name = group_name[:25] + "_" + group_name[-25:]

            # TODO: check dupe collections for dupe imports
            if group_name not in created_collections:
                group_collection = _own_collection(group_name, filepath, "group")
                created_collections[group_name] = group_collection
                flat_collection.children.link(group_collection)
            else:
                group_collection = created_collections[group_name]

            global_t = tree.nodes[obj["STEP_tree_location"]].global_transform
            set_obj_matrix_world(obj, global_t)
            link_created(obj, group_collection)

    # build tree of collections
    if hierarchy_tree:
        tree_collection = _own_collection(
            filename + ".hierarchy", filepath, "hierarchy")
        destination().children.link(tree_collection)
        hierarchy_collections = {}
        hierarchy_collections[-1] = tree_collection

        root = tree.nodes[0]
        # Map root node to tree collection so objects with STEP_parent=0 resolve
        hierarchy_collections[root.index] = tree_collection

        if len(root.children) > 0:
            # Iterative tree traversal (avoids recursion limit on deep assemblies)
            stack = list(reversed([(c, 0, tree_collection) for c in root.children]))
            while stack:
                node_idx, level, parent_collection = stack.pop()
                node = tree.nodes[node_idx]
                if len(node.children) > 0:
                    collection_node = _own_collection(
                        node.name, filepath, "node")
                    assert node.index not in hierarchy_collections
                    hierarchy_collections[node.index] = collection_node
                    parent_collection.children.link(collection_node)
                    for c in reversed(node.children):
                        stack.append((c, level + 1, collection_node))

            # link objects to tree
            if len(hierarchy_collections.items()) > 0:
                for obj in created_objs:
                    parent_id = obj.get("STEP_parent", -1)
                    if parent_id in hierarchy_collections:
                        link_created(obj, hierarchy_collections[parent_id])
                    elif -1 in hierarchy_collections:
                        link_created(obj, hierarchy_collections[-1])
                    else:
                        link_created(obj, destination())
                    global_t = tree.nodes[obj["STEP_tree_location"]].global_transform
                    set_obj_matrix_world(obj, global_t)

    # build hierarchy with empties
    if hierarchy_empties:
        for obj in created_objs:
            global_t = tree.nodes[obj["STEP_tree_location"]].global_transform
            set_obj_matrix_world(obj, global_t)
            link_created(obj, destination())

            # Parent objs
            parent_id = obj["STEP_parent"]
            if parent_id in created_uuid:
                parent = created_uuid[parent_id]
                obj.parent = parent
                obj.matrix_parent_inverse = parent.matrix_world.inverted()

    # build collection-instance hierarchy: prototypes live in a hidden
    # ".components" collection; every occurrence is an instancing empty
    # parented like the EMPTIES mode.
    if hierarchy_instances:
        components_col = _own_collection(
            filename + ".components", filepath, "components")
        destination().children.link(components_col)

        part_collections = {}
        for vname, proto in instance_prototypes.items():
            obj_unlink_all(proto)
            col_name = proto["STEP_name"]
            if len(col_name) > 50:
                col_name = col_name[:25] + "_" + col_name[-25:]
            part_col = _own_collection(col_name, filepath, "part")
            components_col.children.link(part_col)
            part_col.objects.link(proto)
            proto.matrix_world = Matrix.Identity(4)
            part_collections[vname] = part_col

        for obj in created_objs:
            global_t = tree.nodes[obj["STEP_tree_location"]].global_transform
            set_obj_matrix_world(obj, global_t)
            link_created(obj, destination())

            vname = obj.get("STEP_instance_of")
            if vname is not None and vname in part_collections:
                obj.instance_type = "COLLECTION"
                obj.instance_collection = part_collections[vname]

            parent_id = obj["STEP_parent"]
            if parent_id in created_uuid:
                parent = created_uuid[parent_id]
                obj.parent = parent
                obj.matrix_parent_inverse = parent.matrix_world.inverted()

        # Hide the components collection in the view layer
        def _find_layer_col(layer_col, target):
            if layer_col.collection == target:
                return layer_col
            for child in layer_col.children:
                found = _find_layer_col(child, target)
                if found is not None:
                    return found
            return None

        lc = _find_layer_col(bpy.context.view_layer.layer_collection, components_col)
        if lc is not None:
            lc.exclude = True

    # Apply material database replacements (before transforms)
    db_path = _get_active_matdb_path(material_database)
    if db_path:
        mappings = _ensure_matdb_materials(db_path)
        matdb_targets = list(created_objs)
        if hierarchy_instances:
            matdb_targets.extend(instance_prototypes.values())
        _apply_matdb_to_objects(matdb_targets, mappings)
        _cleanup_unused_step_materials(known_names=set(mappings))

    print(f"\n--- Phase 3/3: Applying transforms ---")
    transform_to_up(up_as[0], created_objs, scale, apply_scale=apply_scale)

    if hierarchy_instances and apply_scale and scale != 1.0:
        # transform_to_up reset every object's scale to 1 (prototypes are
        # not in created_objs), so bake the scene scale into the prototype
        # meshes to match apply_scale semantics.
        processed_meshes = set()
        for proto in instance_prototypes.values():
            me = proto.data
            if me is not None and me not in processed_meshes:
                vert_count = len(me.vertices)
                if vert_count > 0:
                    verts = np.empty(vert_count * 3, dtype=np.float32)
                    me.vertices.foreach_get("co", verts)
                    verts *= scale
                    me.vertices.foreach_set("co", verts)
                    me.update()
                processed_meshes.add(me)

    # What this file was imported with, so a refresh reproduces it rather
    # than falling back to whatever the dialog happens to hold later.
    try:
        refresh_mod.record_import(context.scene if context else None, filepath, {
            "up_as": up_as if isinstance(up_as, str) else up_as[0],
            "htypes": htypes,
            "apply_scale": apply_scale,
            "custom_scale": custom_scale,
            "lin_deflection": lin_deflection,
            "ang_deflection": ang_deflection,
            "material_database": material_database,
            "skip_construction": skip_construction,
            "uv_mode": uv_mode,
            "uv_normalize": uv_normalize,
            "uv_split_closed": uv_split_closed,
            "box_uv_scale": box_uv_scale,
            "import_curves": import_curves,
            "eng_materials": eng_materials,
            "group_in_collection": group_in_collection,
            "separate_solids": separate_solids,
        })
    except Exception as exc:
        print("STEPper NEXT: could not record the import:", exc)

    wm.progress_end()
    elapsed = time.time() - start_time

    # Import summary report
    n_objects = len(created_objs)
    n_unique = len(created_names)
    n_linked = n_objects - n_unique
    print(f"\n{'='*50}")
    print(f"  STEPper NEXT Import Summary")
    print(f"{'='*50}")
    print(f"  File:    {filename}")
    print(f"  Objects: {n_objects} ({n_unique} unique, {n_linked} linked copies)")
    print(f"  Scale:   {scale:.6f} m/unit")
    print(f"  Time:    {elapsed:.2f}s")
    has_problems = False
    for k, v in step_reader.import_problems.items():
        if v > 0:
            print(f"  Warning - {k}: {v}")
            has_problems = True
    if step_reader.skipped_shapes:
        print(f"  Skipped: {len(step_reader.skipped_shapes)} shapes")
        has_problems = True
    if step_reader.filtered_labels:
        print(f"  Construction filter skipped: {len(step_reader.filtered_labels)} subtrees")
    if step_reader.recovered_parts:
        print(f"  Recovered parts ({len(step_reader.recovered_parts)}):")
        for rp_name in step_reader.recovered_parts:
            print(f"    - {rp_name}")
        has_problems = True
    if step_reader.failed_parts:
        print(f"  Failed parts ({len(step_reader.failed_parts)}):")
        for fp_name in step_reader.failed_parts:
            print(f"    - {fp_name}")
        has_problems = True
    if not has_problems:
        print(f"  No warnings")
    print(f"{'='*50}")

    # Return lists of failed/recovered part names (empty = full success)
    return step_reader.failed_parts, step_reader.recovered_parts


class PG_MaterialMapping(bpy.types.PropertyGroup):
    """A single original-name -> replacement-material mapping entry."""
    original_name: bpy.props.StringProperty(name="Original Name")
    replacement_name: bpy.props.StringProperty(name="Replacement")


class PG_Stepper(bpy.types.PropertyGroup):
    """Per-scene properties (import parameters, file paths).

    Persistent settings (build_materials, debug_timing, etc.) live on
    STEP_AddonPreferences and are accessed via _get_addon_prefs().
    """
    detail_level: bpy.props.IntProperty(
        name="Mesh detail",
        description="Tessellation detail level (100 = balanced default). "
                    "Higher values produce more polygons",
        default=100,
        min=1,
    )

    lin_deflection: bpy.props.FloatProperty(
        name="Linear deflection",
        description="Maximum distance between the mesh and the true "
                    "surface, in file units. Smaller values produce more "
                    "polygons",
        default=0.8,
        min=0.002,
        # max=2.0,
    )

    ang_deflection: bpy.props.FloatProperty(
        name="Angular deflection",
        description="Maximum angle between adjacent facets, in radians. "
                    "Smaller values produce more polygons",
        default=0.5,
        min=0.002,
        # max=2.0,
    )

    fix_ascii_file: bpy.props.StringProperty(
        name="File",
        description="Path to problematic STEP file",
        default="",
        maxlen=1024,
        subtype="FILE_PATH",
    )

    # Material database UI state
    mat_db_mappings: bpy.props.CollectionProperty(type=PG_MaterialMapping)
    mat_db_active_index: bpy.props.IntProperty(default=0)
    mat_db_apply_selection_only: bpy.props.BoolProperty(
        name="Selection only",
        description="Apply material mappings only to selected objects",
        default=False,
    )


class ImportStepCADOperator(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.occ_import_step"
    bl_label = "Import STEP"
    bl_description = "Import a STEP, IGES or BREP CAD file"
    bl_options = {"PRESET"}

    filter_glob: StringProperty(default="*.step;*.stp;*.st;*.iges;*.igs;*.brep;*.brp", options={"HIDDEN"})
    # SKIP_SAVE on every file-selection property: drops and dialogs must
    # never inherit a previous invocation's file list (a stale `files` entry
    # made drag & drop re-import the previously imported file).
    filepath: StringProperty(subtype="FILE_PATH", options={"HIDDEN", "SKIP_SAVE"})
    # OperatorFileListElement (not a plain PropertyGroup): required for the
    # FileHandler to populate multiple dropped files into the operator
    files: bpy.props.CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})
    override_file: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    fw_as: bpy.props.EnumProperty(
        items=[
            ("XPOS", "X", "", 0),
            # ("XNEG", "X-", "", 1),
            ("YPOS", "Y", "", 2),
            # ("YNEG", "Y-", "", 3),
            ("ZPOS", "Z", "", 4),
            # ("ZNEG", "Z-", "", 5),
        ],
        name="Forward",
        default="ZPOS",
        description="Forward axis of the imported model",
    )

    up_as: bpy.props.EnumProperty(
        items=[
            ("XPOS", "X", "The file's X axis becomes Blender's up axis", 0),
            # ("XNEG", "X-", "", 1),
            ("YPOS", "Y", "The file's Y axis becomes Blender's up axis "
             "(most CAD packages)", 2),
            # ("YNEG", "Y-", "", 3),
            ("ZPOS", "Z", "The file's Z axis becomes Blender's up axis "
             "(no rotation applied)", 4),
            # ("ZNEG", "Z-", "", 5),
        ],
        name="Up",
        default="YPOS",
        description="Up axis of the imported model",
    )

    hierarchy_types: bpy.props.EnumProperty(
        items=[
            ("FLAT", "Flat collection",
             "All objects in a single collection, no hierarchy", 2),
            ("TREE", "Tree collection",
             "Nested collections mirroring the CAD assembly tree", 4),
            ("EMPTIES", "Parented empties",
             "Objects parented under empties mirroring the CAD assembly "
             "tree", 6),
            ("COLLECTION_INSTANCES", "Collection instances",
             "Repeated parts become collection instances (lightest scenes)", 8),
            # ("FLAT_AND_TREE", "Flat and tree collection", "", 0),
        ],
        name="Tree hierarchy",
        default="EMPTIES",
        description="How the addon organizes the imported assembly in the scene",
    )

    user_scale: bpy.props.FloatProperty(
        name="Scale",
        description="Scale factor to use instead of the unit information in the"
                    " file. This applies only when Custom scale is on",
        default=0.01, min=0.00001)

    lin_deflection: bpy.props.FloatProperty(
        name="Linear deflection",
        description="Maximum distance between the mesh and the true "
                    "surface, in file units. Smaller values produce more "
                    "polygons",
        default=0.8,
        min=0.002,
        max=2.0,
    )

    ang_deflection: bpy.props.FloatProperty(
        name="Angular deflection",
        description="Maximum angle between adjacent facets, in radians. "
                    "Smaller values produce more polygons",
        default=0.5,
        min=0.002,
        max=2.0,
    )

    detail_level: bpy.props.IntProperty(
        name="Mesh detail",
        description="Tessellation detail level (100 = balanced default). "
                    "Higher values produce more polygons",
        default=100,
        min=1,
    )

    custom_scale: bpy.props.BoolProperty(
        name="Custom scale",
        description="Set the unit scale by hand instead of reading it from the "
                    "file",
        default=False,
    )

    apply_scale: bpy.props.BoolProperty(
        name="Apply scale",
        description="Bake scale into mesh vertices so object scale is (1, 1, 1)",
        default=True,
    )

    material_database: bpy.props.EnumProperty(
        items=_matdb_enum_items,
        name="Material Database",
        description="Replace the STEP materials with materials from a database",
    )

    quality_preset: bpy.props.EnumProperty(
        items=import_ui.QUALITY_PRESET_ITEMS,
        name="Quality",
        description="Tessellation quality preset (physical deflection, "
                    "independent of the file's unit system)",
        default="BALANCED",
    )

    lin_deflection_len: bpy.props.FloatProperty(
        name="Linear deflection",
        description="Maximum distance between the mesh and the true surface. "
                    "Smaller values increase polygon count",
        unit="LENGTH",
        default=0.0008,
        min=0.000002,
        soft_max=0.01,
        precision=4,
    )

    ang_deflection_rot: bpy.props.FloatProperty(
        name="Angular deflection",
        description="Maximum angle between adjacent mesh faces. "
                    "Smaller values increase polygon count",
        unit="ROTATION",
        default=0.5,
        min=0.002,
        max=1.5,
    )

    skip_construction: bpy.props.BoolProperty(
        name="Skip construction geometry",
        description="Skip parts whose names match the construction-geometry "
                    "filters in the addon preferences (sketches, axes, wires...)",
        default=False,
    )

    uv_mode: bpy.props.EnumProperty(
        items=[
            ("NONE", "None", "Do not create a UV map", 0),
            ("SURFACE", "CAD Surface",
             "One island per CAD face from the parametric surface "
             "coordinates (fast)", 1),
            ("UNWRAP", "Unwrap",
             "Blender's angle-based unwrap with packed islands; sharp CAD "
             "edges act as seams. Slower on large assemblies", 2),
            ("BOX", "Box Project",
             "Triplanar box projection with a world-unit tile size", 3),
        ],
        name="UV Map",
        description="How the addon fills the UVMap layer",
        default="SURFACE",
    )

    uv_normalize: bpy.props.BoolProperty(
        name="Normalize UVs",
        description="Fit the UVs to the 0-1 square. CAD Surface mode fits each "
                    "CAD face. Unwrap mode packs the whole mesh. Turn this off "
                    "to scale the UVs to real world scene units instead. The "
                    "islands stay packed and the addon rescales them together. "
                    "One material then shows a texture at the same size on "
                    "every part",
        default=False,
    )

    eng_materials: bpy.props.BoolProperty(
        name="Engineering Materials",
        description="Give each part one Blender material named after its CAD "
                    "material, such as \"AISI 304 Steel\", instead of color "
                    "materials. The STEP file must carry engineering material "
                    "data. CATIA and NX write it. SOLIDWORKS writes it only "
                    "through the free NEXT-STEP add-in. The addon stores the "
                    "density and the description as custom properties. Use the "
                    "Material Database to map each name to a full shader",
        default=True,
    )

    uv_split_closed: bpy.props.BoolProperty(
        name="Split Closed Faces",
        description="Mark a UV seam along the closure of cylinders and other "
                    "closed surfaces. CAD data has no seam there, so the unwrap"
                    " cannot flatten these faces without one. This does not "
                    "change the shading",
        default=True,
    )

    box_uv_scale: bpy.props.FloatProperty(
        name="Box UV size",
        description="World size of one UV tile for the Box Project mode",
        unit="LENGTH",
        default=1.0,
        min=0.0001,
    )

    import_curves: bpy.props.BoolProperty(
        name="Import curves",
        description="Import free edges (sketches, construction wires) as "
                    "curve objects, in a collection named \"Cad Curves\"",
        default=False,
    )

    separate_solids: bpy.props.BoolProperty(
        name="Separate solids",
        description="Give every body of a multibody part its own object. Use "
                    "this for a file that holds several solids, shells or "
                    "surfaces with no assembly structure to tell them apart",
        default=False,
    )

    group_in_collection: bpy.props.BoolProperty(
        name="Group in a collection",
        description="Put everything this file creates under one collection "
                    "named after the file. A second import then does not "
                    "interleave with the first. The whole assembly moves, hides"
                    " and deletes as one unit",
        default=False,
    )

    tessellation_relative: bpy.props.BoolProperty(
        name="Relative tessellation",
        description="Scale the deflection with the size of each feature. Small "
                    "parts keep their detail and large parts do not explode the"
                    " triangle count. This also turns on parallel meshing",
        default=False,
    )

    lin_deflection_rel: bpy.props.FloatProperty(
        name="Relative deflection",
        description="Deflection as a fraction of each edge's size "
                    "(smaller = finer mesh)",
        default=0.005,
        min=0.00001,
        max=0.5,
        precision=4,
    )

    def draw(self, context):
        import_ui.draw_import_dialog(self, self.layout, _get_addon_prefs())

    def invoke(self, context, event):
        prefs = _get_addon_prefs()
        import_ui.seed_from_prefs(self, prefs)
        # Default the material database dropdown to the addon preference
        if prefs.active_matdb and prefs.active_matdb != "NONE":
            # Verify it's still a valid option
            valid = {name for name, _ in _list_matdb_files()}
            if prefs.active_matdb in valid:
                self.material_database = prefs.active_matdb
        # Drag & drop (FileHandler) pre-populates directory+files (multi
        # drop) or filepath (single drop). Thanks to SKIP_SAVE these are
        # only ever set by an actual drop. Show the options dialog instead
        # of the file browser in that case.
        if (self.directory and len(self.files) > 0) or self.filepath:
            return context.window_manager.invoke_props_dialog(self, width=400)
        return super().invoke(context, event)

    def _background_kwargs(self):
        """Operator settings forwarded verbatim to the worker's operator."""
        kwargs = {
            "up_as": self.up_as,
            "hierarchy_types": self.hierarchy_types,
            "user_scale": self.user_scale,
            "custom_scale": self.custom_scale,
            "apply_scale": self.apply_scale,
            "material_database": self.material_database,
            "detail_level": self.detail_level,
            "skip_construction": self.skip_construction,
            "uv_mode": self.uv_mode,
            "uv_normalize": self.uv_normalize,
            "uv_split_closed": self.uv_split_closed,
            "box_uv_scale": self.box_uv_scale,
            "eng_materials": self.eng_materials,
            "import_curves": self.import_curves,
            "group_in_collection": self.group_in_collection,
            "separate_solids": self.separate_solids,
            "tessellation_relative": self.tessellation_relative,
            "lin_deflection_rel": self.lin_deflection_rel,
        }
        # Mode-selecting props keep their "explicitly set" semantics
        for prop in ("quality_preset", "lin_deflection", "ang_deflection",
                     "lin_deflection_len", "ang_deflection_rot"):
            if self.properties.is_property_set(prop):
                kwargs[prop] = getattr(self, prop)
        return kwargs

    def execute(self, context):
        folder = self.directory or os.path.dirname(self.filepath)

        deflection_spec = import_ui.make_deflection_spec(self, _get_addon_prefs())

        import_files = [i.name for i in self.files]
        # Single-file drop (or scripted call) sets only filepath
        if not import_files and self.filepath:
            import_files = [os.path.basename(self.filepath)]

        if self.override_file != "":
            import_files = [self.override_file]

        if not import_files:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}

        # Remember the dialog options for the next session. Reload/Rebuild
        # and the background worker call this operator with override_file
        # set, and those must not overwrite the user's dialog choices.
        if self.override_file == "":
            import_ui.save_last_used(self, _get_addon_prefs())

        # Route large imports through the background worker (UI stays
        # responsive). Headless sessions and reload/rebuild paths stay sync.
        if self.override_file == "":
            full_paths = [os.path.join(folder, i) for i in import_files]
            if background_mod.should_background(
                    _get_addon_prefs(), full_paths, bpy.app.background):
                job = {"files": full_paths,
                       "op_kwargs": self._background_kwargs(),
                       # Result-affecting addon preferences travel with the
                       # job, because the worker runs factory-startup and would
                       # otherwise import with default prefs
                       "prefs": background_mod.prefs_snapshot(
                           _get_addon_prefs())}
                bpy.ops.stepper.background_import(job_json=json.dumps(job))
                return {"FINISHED"}

        # iterate through the selected files
        all_failed_parts = []
        all_recovered_parts = []
        for j, i in enumerate(import_files):
            # generate full path to file
            path_to_file = os.path.join(folder, i)
            print("Opening file:", path_to_file)
            result = load_step(
                context,
                path_to_file,
                custom_scale=self.user_scale if self.custom_scale else None,
                deflection_spec=deflection_spec,
                up_as=self.up_as,
                htypes=self.hierarchy_types,
                apply_scale=self.apply_scale,
                material_database=self.material_database,
                skip_construction=self.skip_construction,
                uv_mode=self.uv_mode,
                uv_normalize=self.uv_normalize,
                uv_split_closed=self.uv_split_closed,
                box_uv_scale=self.box_uv_scale,
                import_curves=self.import_curves,
                group_in_collection=self.group_in_collection,
                separate_solids=self.separate_solids,
                eng_materials=self.eng_materials,
            )
            if result is False:
                self.report({"ERROR"}, "STEP file could not be opened. Possibly damaged file.")
                return {"CANCELLED"}
            failed, recovered = result
            all_failed_parts.extend(failed)
            all_recovered_parts.extend(recovered)

        if all_failed_parts or all_recovered_parts:
            _show_import_issues_popup(all_failed_parts, all_recovered_parts)
            if all_failed_parts:
                msg = f"{len(all_failed_parts)} part(s) imported with no geometry."
                self.report({"WARNING"}, msg)
            if all_recovered_parts:
                msg = f"{len(all_recovered_parts)} part(s) recovered from corrupted geometry."
                self.report({"INFO"}, msg)

        return {"FINISHED"}


class STEP_OT_ClearCache(bpy.types.Operator):
    bl_idname = "object.occ_clear_cache"
    bl_label = "Clear STEP cache"
    bl_description = "Clear the STEP cache so the next import reads the file again"

    def execute(self, context):
        # utils.memorytrace_print()
        # global global_file_cache
        # items = list(global_file_cache.values())
        # for entry in items:
        #     for i, shp in enumerate(entry):
        #         label, color, tag = entry[shp]
        #         # shp.Nullify()

        global_file_cache.clear()
        return {"FINISHED"}


class STEP_OT_FixASCII(bpy.types.Operator):
    bl_idname = "object.occ_fix_ascii"
    bl_label = "Attempt STEP ASCII fix"
    bl_description = (
        "Repair invalid characters in a STEP file.\n"
        "Use this for files that crash Blender on import.\n"
        "The addon writes a new file with the _fix suffix\n"
        "beside the original."
    )

    def execute(self, context):
        from pathlib import Path
        import unicodedata

        print("Attempting to format STEP file as ASCII")
        i_file = context.scene.stepper.fix_ascii_file
        p = Path(i_file)
        if i_file == "" or not p.exists():
            self.report(
                {"ERROR"},
                "File does not exist.",
            )
            return {"FINISHED"}
        print(p.stat().st_size // 1024, "kB")

        outf = Path(p.parent, Path(p.stem.replace(" ", "_") + "_fix.step"))
        # newline="\n": default newline translation would reintroduce the
        # CRLF line endings this tool just stripped (Windows)
        with outf.open("w", encoding="ASCII", newline="\n") as fo:
            with p.open("rb") as f:
                content = bytearray(f.read())
                content = content.replace(b"\r\n", b"\n")
                content = content.replace(b",\n", b",")
                content = content.replace(b"(\n", b"(")
                fo.write(content.decode("ASCII", errors="ignore"))

        self.report(
            {"INFO"},
            "Operation finished.",
        )
        return {"FINISHED"}


class STEP_OT_PrintDebug(bpy.types.Operator):
    bl_idname = "object.occ_print_debug"
    bl_label = "Print STEP debug info"
    bl_description = "Print STEP debug info"

    def execute(self, context):
        from pathlib import Path

        print("Printing STEP debug info")
        # Shares the Debug panel's file field (there is no separate
        # print_debug property)
        i_file = context.scene.stepper.fix_ascii_file
        p = Path(i_file)
        if i_file == "" or not p.exists():
            self.report(
                {"ERROR"},
                "File does not exist.",
            )
            return {"FINISHED"}

        print(p.stat().st_size // 1024, "kB")

        from . import stepanalyzer

        SA = stepanalyzer.StepAnalyzer(filename=str(p))
        print(SA.dump())

        self.report(
            {"INFO"},
            "Operation finished.",
        )
        return {"FINISHED"}


class STEP_OT_ReloadSTEP(bpy.types.Operator):
    bl_idname = "object.occ_reload_step"
    bl_label = "Reload STEP"
    bl_description = "Reload STEP file"

    @classmethod
    def poll(cls, context):
        return context.object is not None and "STEP_file" in context.object

    def execute(self, context):
        from . import formats

        filepath = context.object["STEP_file"]
        try:
            # make_reader dispatches on extension (STEP/IGES/BREP)
            step_reader = formats.make_reader(filepath)
        except Exception as e:
            self.report({"ERROR"}, f"Could not reload {filepath}: {e}")
            return {"CANCELLED"}
        _cache_put(filepath, step_reader)
        self.report({"INFO"},
                    f"Reloaded {os.path.basename(filepath)} into the cache")
        return {"FINISHED"}


class STEP_OT_ClearFileCache(bpy.types.Operator):
    bl_idname = "object.occ_clear_file_cache"
    bl_label = "Clear this file from cache"
    bl_description = (
        "Remove the STEP file of the selected object from the cache. The next "
        "import reads it from disk again"
    )

    @classmethod
    def poll(cls, context):
        return context.object is not None and "STEP_file" in context.object

    def execute(self, context):
        filepath = context.object["STEP_file"]
        if filepath in global_file_cache:
            del global_file_cache[filepath]
            self.report({"INFO"}, f"Cleared cache for: {os.path.basename(filepath)}")
        else:
            self.report({"WARNING"}, "File not in cache")
        return {"FINISHED"}


class STEP_OT_RebuildSelected(bpy.types.Operator):
    bl_idname = "object.occ_rebuild_selected"
    bl_label = "Rebuild selected objects from the STEP file"
    bl_description = ((
        "Rebuild the selected objects from the STEP file. This old tool is "
        "experimental and fails on some shapes. Use Regenerate Selected in the "
        "Tools panel instead"
    ))

    @classmethod
    def poll(cls, context):
        return context.object is not None and "STEP_file" in context.object

    def execute(self, context):
        meshes = {}
        prevname = ""
        curname = ""
        build_tags = set()
        rebuilt_meshes = set()
        # Only mesh objects that carry STEP metadata can be rebuilt; a
        # box-select of an imported assembly also contains hierarchy empties
        my_selection = [o for o in context.selected_objects
                        if o.data is not None
                        and "STEP_tag" in o and "STEP_file" in o]
        if not my_selection:
            self.report({"WARNING"}, "No rebuildable STEP objects selected")
            return {"CANCELLED"}

        ang_def = context.scene.stepper.ang_deflection
        lin_def = context.scene.stepper.lin_deflection
        # merge_distance = context.scene.stepper.merge_distance
        if _get_addon_prefs().simpler_parameters:
            ang_def, lin_def = calculate_detail_level(bpy.context.scene.stepper.detail_level)

        # select all objs with the same meshes
        for obj in my_selection:
            for other_obj in context.scene.objects:
                if obj.data == other_obj.data:
                    other_obj.select_set(True)

        # go through all selected and rebuild the meshes
        wm = bpy.context.window_manager
        wm.progress_begin(0, len(my_selection))
        for progress_count, obj in enumerate(my_selection):
            sel_tag = obj["STEP_tag"]
            if obj.data.name not in meshes:
                meshes[obj.data.name] = obj.data
                prevname = curname
                curname = obj["STEP_file"]
            else:
                assert meshes[obj.data.name] == obj.data

            if sel_tag in rebuilt_meshes:
                continue

            if prevname != curname:
                cached_reader = _cache_get(curname)
                if cached_reader is not None:
                    step_reader = cached_reader
                    tree = step_reader.tree
                else:
                    self.report(
                        {"ERROR"},
                        'STEP loader: Object "{}" not found in cache for file {}. '
                        "Please reload STEP file".format(obj.name, curname),
                    )
                    break

            for shp, node_index in tree.get_shapes():
                _, _, tag, name, _, _, _ = tree.nodes[node_index].get_values()
                if tag == sel_tag:
                    rebuilt_meshes.add(sel_tag)
                    print("Rebuilding:", sel_tag, obj.data.name)
                    # Reset pre-tessellation flag so shapes get re-tessellated
                    # with the new deflection values
                    step_reader._pre_tessellated = False
                    build_mesh(step_reader, obj, shp, lin_def, ang_def)
                    # Re-apply baked scale if it was applied during import
                    applied_scale = obj.get("STEP_applied_scale", 0.0)
                    if applied_scale and applied_scale != 1.0:
                        mesh = obj.data
                        vert_count = len(mesh.vertices)
                        if vert_count > 0:
                            verts = np.empty(vert_count * 3, dtype=np.float32)
                            mesh.vertices.foreach_get('co', verts)
                            verts *= applied_scale
                            mesh.vertices.foreach_set('co', verts)
                            mesh.update()
                    obj.display_type = "TEXTURED"
                    build_tags.add(obj["STEP_tag"])
                    break

            wm.progress_update(progress_count)

        wm.progress_end()

        for obj in context.selected_objects:
            obj.display_type = "TEXTURED"

        # Re-apply active material database if one is set
        db_path = _get_active_matdb_path()
        if db_path:
            mappings = _ensure_matdb_materials(db_path)
            _apply_matdb_to_objects(my_selection, mappings)

        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Material Database operators
# ---------------------------------------------------------------------------

class STEP_OT_MatDBCreate(bpy.types.Operator):
    """Create a new material database from the current scene"""
    bl_idname = "stepper.mat_db_create"
    bl_label = "Create Material Database"

    db_name: bpy.props.StringProperty(
        name="Database Name",
        description="Name for the new material database",
        default="material_database",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        name = _sanitize_db_name(self.db_name)
        if not name:
            self.report({'ERROR'}, "Database name cannot be empty")
            return {'CANCELLED'}

        filepath = os.path.join(_get_matdb_dir(), name + ".blend")
        if os.path.exists(filepath):
            self.report({'ERROR'}, f"Database '{name}' already exists, "
                        "choose another name or delete it first")
            return {'CANCELLED'}

        mappings = _scan_scene_materials()
        if not mappings:
            self.report({'WARNING'}, "No STEP materials found in scene")
            return {'CANCELLED'}

        _write_material_database(filepath, mappings)

        # Set as active database and load into UI
        prefs = _get_addon_prefs()
        prefs.active_matdb = name
        _populate_ui_mappings(context.scene.stepper, mappings)

        self.report({'INFO'}, f"Created '{name}' with {len(mappings)} mapping(s)")
        return {'FINISHED'}


class STEP_OT_MatDBDuplicate(bpy.types.Operator):
    """Duplicate the active material database with a new name"""
    bl_idname = "stepper.mat_db_duplicate"
    bl_label = "Duplicate Material Database"

    db_name: bpy.props.StringProperty(
        name="New Name",
        description="Name for the duplicated database",
        default="",
    )

    @classmethod
    def poll(cls, context):
        return bool(_get_active_matdb_path())

    def invoke(self, context, event):
        prefs = _get_addon_prefs()
        self.db_name = prefs.active_matdb + "_copy"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        import shutil

        name = _sanitize_db_name(self.db_name)
        if not name:
            self.report({'ERROR'}, "Database name cannot be empty")
            return {'CANCELLED'}

        src = _get_active_matdb_path()
        dst = os.path.join(_get_matdb_dir(), name + ".blend")
        if os.path.exists(dst):
            self.report({'ERROR'}, f"Database '{name}' already exists")
            return {'CANCELLED'}

        shutil.copy2(src, dst)

        prefs = _get_addon_prefs()
        prefs.active_matdb = name
        # Reload mappings from the copy
        mappings = _read_matdb_mappings(dst)
        _populate_ui_mappings(context.scene.stepper, mappings)

        self.report({'INFO'}, f"Duplicated to '{name}'")
        return {'FINISHED'}


class STEP_OT_MatDBRefresh(bpy.types.Operator):
    """Reload mappings from the active database file and append its materials"""
    bl_idname = "stepper.mat_db_refresh"
    bl_label = "Load Material Database"

    @classmethod
    def poll(cls, context):
        return bool(_get_active_matdb_path())

    def execute(self, context):
        db_path = _get_active_matdb_path()

        # Append materials first so they show up in the UI prop_search
        _append_matdb_materials(db_path)

        mappings = _read_matdb_mappings(db_path)
        if not mappings:
            self.report({'WARNING'}, "No mappings found in database")
            return {'CANCELLED'}

        _populate_ui_mappings(context.scene.stepper, mappings)
        self.report({'INFO'}, f"Loaded {len(mappings)} mapping(s)")
        return {'FINISHED'}


class STEP_OT_MatDBUpdate(bpy.types.Operator):
    """Add new original material names from the scene to the database"""
    bl_idname = "stepper.mat_db_update"
    bl_label = "Update Material Database"

    @classmethod
    def poll(cls, context):
        return len(context.scene.stepper.mat_db_mappings) > 0

    def execute(self, context):
        stepper = context.scene.stepper
        scene_mappings = _scan_scene_materials()

        # Existing originals in the UI list
        existing = {item.original_name for item in stepper.mat_db_mappings}

        added = 0
        for orig, repl in scene_mappings.items():
            if orig not in existing:
                item = stepper.mat_db_mappings.add()
                item.original_name = orig
                item.replacement_name = repl
                added += 1

        if added:
            self.report({'INFO'}, f"Added {added} new mapping(s). Press Save to write to database.")
        else:
            self.report({'INFO'}, "No new materials to add")
        return {'FINISHED'}


class STEP_OT_MatDBSave(bpy.types.Operator):
    """Save current mappings to the active database file"""
    bl_idname = "stepper.mat_db_save"
    bl_label = "Save Material Database"

    @classmethod
    def poll(cls, context):
        return bool(_get_active_matdb_path()) and len(context.scene.stepper.mat_db_mappings) > 0

    def execute(self, context):
        stepper = context.scene.stepper
        filepath = _get_active_matdb_path()

        mappings = {}
        for item in stepper.mat_db_mappings:
            mappings[item.original_name] = item.replacement_name

        _write_material_database(filepath, mappings)
        self.report({'INFO'}, f"Saved {len(mappings)} mapping(s) to database")
        return {'FINISHED'}


class STEP_OT_MatDBDelete(bpy.types.Operator):
    """Delete the active material database file"""
    bl_idname = "stepper.mat_db_delete"
    bl_label = "Delete Material Database"

    @classmethod
    def poll(cls, context):
        return bool(_get_active_matdb_path())

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        prefs = _get_addon_prefs()
        db_path = _get_active_matdb_path()
        name = prefs.active_matdb

        try:
            os.remove(db_path)
        except OSError as e:
            self.report({'ERROR'}, f"Could not delete database: {e}")
            return {'CANCELLED'}

        prefs.active_matdb = "NONE"
        context.scene.stepper.mat_db_mappings.clear()
        self.report({'INFO'}, f"Deleted database '{name}'")
        return {'FINISHED'}


class STEP_OT_MatDBApply(bpy.types.Operator):
    """Apply material mappings from the active database to objects in the scene"""
    bl_idname = "stepper.mat_db_apply"
    bl_label = "Apply Material Database"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(_get_active_matdb_path())

    def invoke(self, context, event):
        # Append materials / read mappings BEFORE the undo step
        db_path = _get_active_matdb_path()
        self._mappings = _ensure_matdb_materials(db_path)
        if not self._mappings:
            self.report({'WARNING'}, "No mappings found in database")
            return {'CANCELLED'}
        return self.execute(context)

    def execute(self, context):
        stepper = context.scene.stepper

        # invoke() prepares _mappings; a direct execute call (redo panel,
        # scripts) must load them itself
        mappings = getattr(self, "_mappings", None)
        if mappings is None:
            mappings = _ensure_matdb_materials(_get_active_matdb_path())
        if not mappings:
            self.report({'WARNING'}, "No mappings found in database")
            return {'CANCELLED'}

        if stepper.mat_db_apply_selection_only:
            objects = list(context.selected_objects)
        else:
            objects = [obj for obj in bpy.data.objects
                       if obj.get("STEP_file") is not None]

        if not objects:
            self.report({'WARNING'}, "No STEP objects found")
            return {'CANCELLED'}

        replaced = _apply_matdb_to_objects(objects, mappings)
        _cleanup_unused_step_materials(known_names=set(mappings))
        self.report({'INFO'}, f"Replaced {replaced} material(s) across {len(objects)} object(s)")
        return {'FINISHED'}


def _populate_ui_mappings(stepper, mappings):
    """Fill the UI CollectionProperty from a mappings dict."""
    stepper.mat_db_mappings.clear()
    for orig, repl in sorted(mappings.items()):
        item = stepper.mat_db_mappings.add()
        item.original_name = orig
        item.replacement_name = repl


# ---------------------------------------------------------------------------
# Material Database UI
# ---------------------------------------------------------------------------

class STEP_UL_MaterialMappings(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_property, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            split = layout.split(factor=0.45, align=True)
            split.label(text=item.original_name + "  \u2192", icon='MATERIAL')
            split.prop_search(item, "replacement_name", bpy.data, "materials", text="")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.original_name)


class STEP_PT_MaterialDB(bpy.types.Panel):
    bl_label = "STEPper NEXT: Material DB"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "STEPper NEXT"

    def draw(self, context):
        layout = self.layout
        stepper = context.scene.stepper
        prefs = _get_addon_prefs()

        # Database selector
        box = layout.box()
        box.label(text="Database")
        row = box.row(align=True)
        row.prop(prefs, "active_matdb", text="")
        sub = row.row(align=True)
        sub.enabled = prefs.active_matdb != "NONE"
        sub.operator("stepper.mat_db_delete", text="", icon='TRASH')

        row = box.row(align=True)
        row.operator("stepper.mat_db_create", text="New", icon='ADD')
        sub = row.row(align=True)
        sub.enabled = prefs.active_matdb != "NONE"
        sub.operator("stepper.mat_db_duplicate", text="Duplicate", icon='DUPLICATE')
        row.operator("stepper.mat_db_refresh", text="Load", icon='IMPORT')

        # Mappings list
        if len(stepper.mat_db_mappings) > 0:
            box = layout.box()
            box.label(text="Material Mappings")
            box.template_list(
                "STEP_UL_MaterialMappings", "",
                stepper, "mat_db_mappings",
                stepper, "mat_db_active_index",
                rows=5,
            )
            row = box.row(align=True)
            row.operator("stepper.mat_db_update", text="Update", icon='FILE_REFRESH')
            row.operator("stepper.mat_db_save", text="Save", icon='EXPORT')

            row = box.row(align=True)
            row.operator("stepper.mat_db_apply", text="Apply", icon='CHECKMARK')
            row.prop(stepper, "mat_db_apply_selection_only")


class STEP_PT_STEPper_Info(bpy.types.Panel):
    """Version line at the top of the tab, with the update notice."""
    bl_label = "STEPper NEXT: Info"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "STEPper NEXT"
    bl_options = {"HIDE_HEADER"}
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.label(text="STEPper NEXT v%s" % updater_mod.version_string(),
                  icon="TOOL_SETTINGS")
        # The tip jar: a heart, deliberately unlabelled so the version line
        # stays readable at narrow sidebar widths.
        row.operator("wm.url_open", text="",
                     icon="FUND").url = updater_mod.KOFI_URL

        update = updater_mod.available_update()
        if update:
            box = layout.box().column(align=True)
            box.label(text="Version %s is available" % update["version"],
                      icon="INFO")
            download = box.operator(
                "wm.url_open", icon="IMPORT",
                text="Download %s" % update["version"])
            download.url = update["url"]
            box.label(text="Install the zip as usual to update.")


class STEP_PT_STEPper(bpy.types.Panel):
    bl_label = "STEPper NEXT: Tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "STEPper NEXT"

    def draw(self, context):
        prg = context.scene.stepper

        layout = self.layout

        # These resolution values feed the Regenerate operator below (with
        # its "Use STEPper Panel Resolution" option, on by default)
        box = layout.box()
        col = box.column(align=True)
        col.label(text="Regenerate Resolution:")
        if _get_addon_prefs().simpler_parameters:
            col.prop(prg, "detail_level")
        else:
            col.prop(prg, "lin_deflection")
            col.prop(prg, "ang_deflection")
        box.operator("stepper.regenerate", text="Regenerate Selected",
                     icon='FILE_REFRESH')

        col = layout.column(align=True)
        row = col.row(align=True)
        row.operator("stepper.prune_hierarchy", text="Prune Hierarchy",
                     icon='X')
        row.operator("stepper.prune_restore", text="Restore",
                     icon='LOOP_BACK')
        row = col.row(align=True)
        row.operator("stepper.mesh_cleanup", text="Clean Up Meshes",
                     icon='MESH_DATA')
        row.operator("stepper.add_box_uv", text="Box Project UVs", icon='UV')


class STEP_PT_STEPper_Reload(bpy.types.Panel):
    bl_label = "STEPper NEXT: File"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "STEPper NEXT"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("stepper.batch_import_folder",
                     text="Batch Import Folder", icon='FILE_FOLDER')
        col.operator("stepper.analyze_file", text="Analyze STEP File",
                     icon='VIEWZOOM')
        col = layout.column(align=True)
        col.operator(STEP_OT_ReloadSTEP.bl_idname, text="Reload STEP File",
                     icon='FILE_REFRESH')
        col.operator(STEP_OT_ClearFileCache.bl_idname,
                     text="Clear This File From Cache", icon='TRASH')
        col.operator(STEP_OT_ClearCache.bl_idname, text="Clear All Cache",
                     icon='TRASH')
        row = layout.row()
        row.label(text=f"Cached files: {len(global_file_cache)}/{MAX_FILE_CACHE}")


class STEP_PT_STEPper_Debug(bpy.types.Panel):
    bl_label = "STEPper NEXT: Debug"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "STEPper NEXT"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        prg = context.scene.stepper

        bxp = layout.box()
        bxp.label(text="Enforce ASCII")
        col = bxp.row().column(align=True)

        row = col.row()
        row.prop(prg, "fix_ascii_file")
        row = col.row()
        row.operator("object.occ_fix_ascii", text="Attempt fix STEP charset")

        # row = layout.row()
        # row.label(text="Error messages:")

        if (
            context.object is not None
            and "STEP_file" in context.object
            and context.object["STEP_file"] in global_file_cache
        ):
            bxp = layout.box()
            bxp.label(text="Reported problems:")

            row = bxp.row()
            col = row.column(align=True)
            step_reader = global_file_cache[context.object["STEP_file"]]
            for k, v in step_reader.import_problems.items():
                row = col.row()
                row.label(text=k + ": " + repr(v))

            bxs = layout.box()
            bxs.label(text="Skipped shapes:")

            row = bxs.row()
            col = row.column(align=True)
            if len(step_reader.skipped_shapes) > 0:
                for v in step_reader.skipped_shapes:
                    row = col.row()
                    row.label(text=repr(v))
            else:
                row = col.row()
                row.label(text="No skipped shapes")

        else:
            bxp = layout.box()
            row = bxp.row()
            row.label(text="Select active STEP object")


def _get_addon_prefs():
    """Return the addon preferences instance."""
    return bpy.context.preferences.addons[__package__].preferences


class STEP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    build_materials: bpy.props.BoolProperty(
        name="Build materials",
        description="Build materials from STEP file colors",
        default=True,
    )

    hack_skip_zero_solids: bpy.props.BoolProperty(
        name="Skip faulty solids",
        description="Skip corrupted/empty parts entirely (no healing or recovery attempts)",
        default=False,
    )

    simpler_parameters: bpy.props.BoolProperty(
        name="Artist friendly parameters",
        description="Show one detail slider instead of the linear and angular "
                    "deflection values",
        default=True,
    )

    skip_empty_objects: bpy.props.BoolProperty(
        name="Skip empty objects",
        description="Do not create objects for parts that produce no geometry",
        default=True,
    )

    debug_timing: bpy.props.BoolProperty(
        name="Debug timing",
        description="Print detailed timing information during import",
        default=False,
    )

    active_matdb: bpy.props.EnumProperty(
        items=_matdb_enum_items,
        name="Material Database",
        description="Active material database for import",
    )

    matdb_dir: bpy.props.StringProperty(
        name="Material database folder",
        description="Folder that holds the .blend material databases. Leave it "
                    "empty to use the MaterialDB folder inside the addon. Every"
                    " reinstall wipes that folder, and you cannot share it "
                    "between machines or with a team",
        subtype="DIR_PATH",
        default="",
    )

    remember_import_settings: bpy.props.BoolProperty(
        name="Remember import settings",
        description="Save the import dialog options after every import and "
                    "restore them in the next Blender session. This needs Save "
                    "Preferences on Quit, or a manual preferences save. Turn "
                    "this off to start from the defaults below instead",
        default=True,
    )

    last_import_settings: bpy.props.StringProperty(
        name="Last import settings",
        description="Options from the last import (the addon manages this)",
        default="",
        options={"HIDDEN"},
    )

    preferred_up_axis: bpy.props.EnumProperty(
        items=[("XPOS", "X", "", 0), ("YPOS", "Y", "", 2), ("ZPOS", "Z", "", 4)],
        name="Default up axis",
        description="Up axis preselected in the import dialog",
        default="YPOS",
    )

    preferred_hierarchy: bpy.props.EnumProperty(
        items=[
            ("FLAT", "Flat collection",
             "All objects in a single collection, no hierarchy", 2),
            ("TREE", "Tree collection",
             "Nested collections mirroring the CAD assembly tree", 4),
            ("EMPTIES", "Parented empties",
             "Objects parented under empties mirroring the CAD assembly "
             "tree", 6),
            ("COLLECTION_INSTANCES", "Collection instances",
             "Repeated parts become collection instances (lightest "
             "scenes)", 8),
        ],
        name="Default hierarchy",
        description="Hierarchy mode preselected in the import dialog",
        default="EMPTIES",
    )

    default_quality_preset: bpy.props.EnumProperty(
        items=import_ui.QUALITY_PRESET_ITEMS,
        name="Default quality",
        description="Quality preset preselected in the import dialog",
        default="BALANCED",
    )

    background_import: bpy.props.BoolProperty(
        name="Background import",
        description="Import in a background process so Blender stays "
                    "responsive (Esc cancels). Small files still import "
                    "directly",
        default=True,
    )

    background_min_mb: bpy.props.FloatProperty(
        name="Background threshold (MB)",
        description="Import files smaller than this directly. The background "
                    "process takes too long to start to be worth it for them",
        default=2.0,
        min=0.0,
    )

    perf_calibration: bpy.props.StringProperty(
        name="Performance calibration",
        description="Import speed measured on this machine, used by the "
                    "analyzer (the addon manages this)",
        default="{}",
    )

    check_for_updates: bpy.props.BoolProperty(
        name="Check for updates",
        description="Ask GitHub once a day if a newer release exists, and show "
                    "a notice in the sidebar. The addon sends no information "
                    "about you or your files",
        default=True,
    )

    update_last_check: bpy.props.StringProperty(
        name="Last update check",
        description="Date of the last update check (the addon manages this)",
        default="",
        options={"HIDDEN"},
    )

    update_latest_tag: bpy.props.StringProperty(
        name="Latest release tag",
        description="Newest release tag seen on GitHub (the addon manages this)",
        default="",
        options={"HIDDEN"},
    )

    update_latest_url: bpy.props.StringProperty(
        name="Latest release download",
        description="Download address for the newest release (the addon manages"
                    " this)",
        default="",
        options={"HIDDEN"},
    )

    construction_filter_names: bpy.props.StringProperty(
        name="Construction name filters",
        description="Comma separated name prefixes to skip when Skip "
                    "construction geometry is on",
        default="Axes,Sketches,Lines,Hatches,Wires,Curves,Construction,"
                "Annotations,Planes,Origin",
    )

    def draw(self, context):
        layout = self.layout

        must_have_str = ".".join(str(i) for i in must_have_python)
        if sys.version_info[:2] != must_have_python:
            box = layout.box().column(align=True)
            box.alert = True
            box.label(text="STEPper NEXT: Python version check failure", icon="ERROR")

            row = box.row()
            row.label(text="Current version: " + str(".".join(str(i) for i in sys.version_info[:2])))
            row = box.row()
            row.label(text="Please install Blender with Python " + must_have_str)
            row = box.row()
            row.label(text="https://www.blender.org/")
            return

        row = layout.row()
        row.prop(self, "build_materials")

        row = layout.row()
        row.prop(self, "hack_skip_zero_solids")

        row = layout.row()
        row.prop(self, "simpler_parameters")

        row = layout.row()
        row.prop(self, "skip_empty_objects")

        row = layout.row()
        row.prop(self, "debug_timing")

        col = layout.box().column(align=True)
        col.label(text="Material database:")
        col.prop(self, "active_matdb")
        col.prop(self, "matdb_dir")
        if not self.matdb_dir.strip():
            col.label(text="Using the addon's own folder. A reinstall "
                           "wipes it", icon="INFO")

        col = layout.box().column(align=True)
        col.label(text="Import dialog defaults:")
        col.prop(self, "remember_import_settings")
        sub = col.column(align=True)
        # Last-used settings override these three, so grey them out
        sub.active = not self.remember_import_settings
        sub.prop(self, "preferred_up_axis")
        sub.prop(self, "preferred_hierarchy")
        sub.prop(self, "default_quality_preset")
        col.prop(self, "construction_filter_names")

        col = layout.box().column(align=True)
        col.prop(self, "background_import")
        if self.background_import:
            col.prop(self, "background_min_mb")

        col = layout.box().column(align=True)
        col.prop(self, "check_for_updates")
        update = updater_mod.available_update(self)
        if update:
            row = col.row()
            row.alert = True
            row.label(text="Version %s is available" % update["version"],
                      icon="INFO")
            download = col.operator(
                "wm.url_open", icon="URL",
                text="Download %s" % update["version"])
            download.url = update["url"]
        else:
            col.label(text="STEPper NEXT %s is up to date"
                           % updater_mod.version_string(), icon="CHECKMARK")
        kofi = col.operator("wm.url_open", icon="FUND",
                            text="Support development on Ko-fi")
        kofi.url = updater_mod.KOFI_URL


def menu_func_import(self, context):
    self.layout.operator(ImportStepCADOperator.bl_idname, text="STEP/IGES/BREP CAD [STEPper NEXT]")


classes = (
    STEP_AddonPreferences,
    PG_MaterialMapping,
    PG_Stepper,
    ImportStepCADOperator,
    STEP_OT_ClearCache,
    STEP_OT_ClearFileCache,
    STEP_OT_RebuildSelected,
    STEP_OT_ReloadSTEP,
    STEP_OT_FixASCII,
    STEP_OT_PrintDebug,
    STEP_OT_MatDBCreate,
    STEP_OT_MatDBDuplicate,
    STEP_OT_MatDBRefresh,
    STEP_OT_MatDBUpdate,
    STEP_OT_MatDBSave,
    STEP_OT_MatDBDelete,
    STEP_OT_MatDBApply,
    STEP_UL_MaterialMappings,
    STEP_PT_STEPper_Info,
    STEP_PT_STEPper,
    STEP_PT_STEPper_Reload,
    STEP_PT_MaterialDB,
    STEP_PT_STEPper_Debug,
) + (import_ui.classes + uv_mod.classes + tools_mod.classes
     + curves_mod.classes + formats_classes + analyzer_mod.classes
     + refresh_mod.classes
     + background_mod.classes)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.stepper = bpy.props.PointerProperty(type=PG_Stepper)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    updater_mod.start()


def unregister():
    updater_mod.stop()
    for c in classes[::-1]:
        bpy.utils.unregister_class(c)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.stepper
