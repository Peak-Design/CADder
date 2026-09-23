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
import math
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
from . import empties as empties_mod
from . import geometry_lock
from . import material_lock
from . import quality as quality_mod
from . import background as background_mod
from . import updater as updater_mod
from .formats import classes as formats_classes

# The rig subpackage (CAD Link: armature from a .rig.json manifest) is
# developed in-tree but must never take the importer down with it. A broken
# rig module costs the rig panel, not STEP import.
try:
    from . import rig as rig_mod
except Exception as _rig_exc:
    rig_mod = None
    print("CADder: rig subpackage failed to load:", _rig_exc)

# Same isolation for the CAD Link bridge: a bridge fault must never cost
# STEP import.
try:
    from . import bridge as bridge_mod
except Exception as _bridge_exc:
    bridge_mod = None
    print("CADder: bridge module failed to load:", _bridge_exc)

# Active UV options for the current import (set by load_step)
# Per-import UV generation state (a single "UVMap" layer. The booleans are
# derived from the operator's uv_mode enum in load_step)
_uv_options = {"surface": True, "unwrap": False, "box": False,
               "box_scale": 1.0}


def _set_uv_options(mode, normalize, closed_seams, box_scale):
    """Set the UV options the mesh build reads, from the UV Map mode.

    The import and Regenerate both build meshes through the same code, and
    that code reads this dict instead of taking arguments. Every mode that
    starts from the CAD surface writes the parametric UVs first. An unwrap
    then replaces them, and they stay as the content to fall back on if it
    fails.
    """
    _uv_options["mode"] = mode
    _uv_options["surface"] = mode in uv_mod.SURFACE_MODES
    _uv_options["unwrap"] = mode in uv_mod.UNWRAP_MODES
    _uv_options["box"] = mode == "BOX"
    _uv_options["normalize"] = normalize
    _uv_options["closed_seams"] = closed_seams
    _uv_options["box_scale"] = box_scale

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


def _cache_put(filepath, step_reader, signature=None):
    """Add to file cache with LRU eviction.

    `signature` is the file's stamp from BEFORE the read. Stamped after it,
    a file written again during the read (an export loop that writes the
    same path) was cached under the new stamp with the old content, and
    the next import got the old pose from the cache. With the stamp from
    before, such a reader reads as stale and the file is read again."""
    if filepath in global_file_cache:
        global_file_cache.move_to_end(filepath)
    global_file_cache[filepath] = step_reader
    if signature is None:
        signature = _file_signature(filepath)
    global_file_cache_meta[filepath] = signature
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
    reading diffuse_color would always return Blender's default gray.
    Materials the addon did not build (e.g. a Material Database
    replacement using some other shader) fall back to diffuse_color.
    """
    if mat is None:
        return fallback
    if mat.use_nodes and mat.node_tree is not None:
        for node in mat.node_tree.nodes:
            if node.type != "BSDF_PRINCIPLED":
                continue
            # Named lookup can fail under some UI translations. Input 0 of
            # a Principled BSDF is Base Color either way.
            inp = node.inputs.get("Base Color")
            if inp is None and len(node.inputs):
                inp = node.inputs[0]
            if inp is not None:
                return tuple(inp.default_value[:3])
            break
    return tuple(mat.diffuse_color[:3])


# The object color the import wrote, so a refresh can tell it apart from one
# the user picked. Kept next to the value it describes.
OBJECT_COLOR_PROP = "STEP_object_color"


def set_object_colors(objects):
    """Copies the CAD color onto each object's own color.

    obj.color is what a Solid viewport shows with Color set to Object, and
    what an Object Info node reads in a shader. The color is already in the
    material, but nothing outside Material Preview reads a material, so a
    solid viewport showed a gray assembly. This makes it match the file.

    A part with more than one material takes the first, which is the
    shape-level color the file gives the product. Per-face colors have no
    single answer, and guessing one would be worse than taking the one the
    file states.

    STEP carries no transparency here, so the alpha stays at 1.
    """
    for obj in objects:
        data = getattr(obj, "data", None)
        slots = getattr(data, "materials", None) or ()
        first = next((m for m in slots if m is not None), None)
        if first is None:
            continue
        color = _material_base_color(first, None)
        if color is None:
            continue
        obj.color = (*color, 1.0)
        obj[OBJECT_COLOR_PROP] = list(obj.color)


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


def transform_to_up(up, chosen_objects, scale, to_cursor=True, apply_scale=True,
                    cursor=None):
    """
    Set all chosen_objects transforms <up>["X", "Y", "Z"] as up
    Optionally move to cursor <to_cursor>: the location <cursor> when given,
    else the 3D cursor
    Set scale to scale
    """

    # transforms and processing of objects
    # bpy.ops.object.select_all(action="DESELECT")

    if not to_cursor:
        cursor_pos = Vector((0.0, 0.0, 0.0))
    elif cursor is not None:
        cursor_pos = Vector(cursor)
    else:
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
    material is first created. Density/description are stored as custom
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


# One UDIM row is ten tiles wide, which is the convention every texture
# tool follows: tile 1001 + u + 10 * v.
UDIM_ROW = 10
UV_PACK_MARGIN = 0.005
# The Smart distortion setting, in percent (uv.SMART_DISTORTION explains it).
UV_SMART_DISTORTION = 35.0


# The UV dropdowns are offered twice: in the import dialog and in the UV
# panel, which makes the same map again for parts that are already in the
# scene. One definition, so the two can never drift apart.
# The enum numbers are what a .blend stores. Angle Based keeps number 2, the
# number of the old Unwrap mode, because that mode always used the angle
# based method. A scene saved with it opens on the same choice.
UV_MODE_ITEMS = [
    ("NONE", "None", "Do not make a UV map", 0),
    ("SURFACE", "CAD Surfaces",
     "One island for each CAD face, from the parametric coordinates of the "
     "surface. The fastest mode, with the cleanest islands", 1),
    ("SMART", "CAD Surfaces (Smart)",
     "Join each CAD face to its smooth neighbors, one face at a time. A join "
     "stays only if the island does not overlap itself and still fits the "
     "UV tile", 4),
    ("CONFORMAL", "Unwrap (Conformal)",
     "Blender unwrap that keeps the angles. The sharp CAD edges are the "
     "seams. Slower on large assemblies", 5),
    ("ANGLE_BASED", "Unwrap (Angle Based)",
     "Blender unwrap that spreads the error over the whole island. It can "
     "fold a long cylinder on to itself", 2),
    ("MINIMUM_STRETCH", "Unwrap (Minimum Stretch)",
     "Blender unwrap that works to even out the stretch. The slowest mode",
     6),
    ("BOX", "Box Project",
     "Project each face from the nearest of three directions, with a tile "
     "size in world units", 3),
]

# CAD Surfaces (Smart) builds a net and cuts where the net needs it, so it
# has no use for this setting, and the dialog shows it only for the modes
# that do.
UV_CLOSED_ITEMS = [
    ("NONE", "None",
     "Leave closed surfaces alone. An unwrap cannot flatten them and "
     "gives a badly distorted island", 0),
    ("SINGLE", "Single seam",
     "One seam along the closure. A hole unrolls into one flat "
     "island and its two halves stay joined", 1),
    ("SPLIT", "Split faces",
     "A seam on every boundary inside a closed region. A hole made "
     "of two half cylinders becomes two separate islands", 2),
]

UV_PACK_ITEMS = [
    ("NONE", "None", "Leave the islands where the UV mode put them", 0),
    ("ALL", "All parts together",
     "Pack every part into one 0-1 tile. Merge the parts afterwards "
     "and the whole import is ready to texture as one piece", 1),
    ("OBJECT", "Each part on its own",
     "Give every part its own 0-1 tile, packed on its own. The "
     "slowest choice: the packer has to run once for each part", 2),
    ("UDIM", "Into UDIM tiles",
     "Share the parts over a set number of tiles and pack each tile. "
     "A middle way between one texture for everything and one for "
     "each part", 3),
]


def _uv_array(me):
    """The active UV layer as an (n, 2) array, or None."""
    layer = me.uv_layers.active
    n = len(me.loops)
    if layer is None or not n:
        return None, None
    a = np.empty(n * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", a)
    return layer, a.reshape(-1, 2)


def _uv_move(me, du, dv):
    """Slide a mesh's UVs by whole tiles."""
    layer, uv = _uv_array(me)
    if uv is None:
        return
    uv[:, 0] += du
    uv[:, 1] += dv
    layer.uv.foreach_set("vector", uv.ravel())


def _uv_home(me):
    """Slide a mesh's UVs back into tile 0.

    The packer works out which tile to pack into from where the UVs already
    are, so everything has to start in the same tile to end up in one.
    """
    layer, uv = _uv_array(me)
    if uv is None:
        return
    low = np.floor(uv.min(axis=0))
    if low[0] or low[1]:
        uv -= low
        layer.uv.foreach_set("vector", uv.ravel())


def _hide_materials(objs):
    """Take the materials off `objs` for a while, so the UVs stay square.

    Blender's Pack Islands and Average Islands Scale read the image texture
    in a face's material, and work in the proportions of that image. On a
    2 by 1 texture, an island the packer turns comes out 4 times too narrow.
    The addon makes UVs for a square tile, whatever the material holds, and
    neither operator has an option to stop this. With no material there is
    no image to read. Returns what _restore_materials puts back.
    """
    saved = []
    for o in objs:
        for i, slot in enumerate(o.material_slots):
            if slot.material is not None:
                saved.append((o, i, slot.material))
                slot.material = None
    return saved


def _restore_materials(saved):
    """Put back the materials _hide_materials took off."""
    for o, i, mat in saved:
        try:
            o.material_slots[i].material = mat
        except (IndexError, ReferenceError, RuntimeError):
            pass


def _deselect_all():
    for o in bpy.context.view_layer.objects:
        try:
            o.select_set(False)
        except RuntimeError:
            pass


def _editable(objs):
    """Objects edit mode can take, one for each of `objs`, and the holders
    made for them.

    Edit mode takes only an object that is shown in the view layer. The
    prototype of a collection instance sits in an excluded collection,
    select_set fails on it, and the pack or unwrap did nothing while the UV
    panel reported it done. Such a mesh gets a holder object of its own in
    the scene collection, as in _unwrap_compound_objects. The caller
    removes the holders."""
    out, holders = [], []
    for o in objs:
        if o.visible_get():
            out.append(o)
            continue
        holder = bpy.data.objects.new("CADder UV", o.data)
        bpy.context.scene.collection.objects.link(holder)
        holders.append(holder)
        out.append(holder)
    return out, holders


def _crowd_margin(margin, count):
    """The margin to ask for when `count` meshes share one tile.

    Blender puts the margin around EVERY island, not around the group. A CAD
    part carries about one island per face, so a whole assembly in one tile
    is thousands of islands. Measured on 300 parts, near 3000 islands, in one
    tile: a margin of 0.005 fills 6 percent of the tile, 0.001 fills 37
    percent and 0.0001 fills 80 percent. The margin has to come down as the
    crowd grows, or the gaps eat the texture.
    """
    return margin / math.sqrt(max(1, count))


def _pack_group(objs, margin=UV_PACK_MARGIN, scale=True):
    """Pack one set of meshes into tile 0, together.

    Selects and deselects only its own meshes. Clearing the whole scene on
    every call would make packing one tile per part cost the square of the
    part count, which on a real assembly is most of the run time.

    When `scale` lets the packer resize, Average Islands Scale runs first.
    The packer applies one factor to the whole group, so it keeps whatever
    size difference the islands arrive with. Averaging gives every island in
    the group the same texel density before that.
    """
    if not objs:
        return
    view_layer = bpy.context.view_layer
    for o in objs:
        o.select_set(True)
    view_layer.objects.active = objs[0]
    hidden = _hide_materials(objs)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        if scale:
            # Each island keeps its own center, so the group stays in the
            # tile the packer is about to read it from.
            bpy.ops.uv.average_islands_scale()
        bpy.ops.uv.pack_islands(udim_source="CLOSEST_UDIM", rotate=True,
                                scale=scale,
                                margin=_crowd_margin(margin, len(objs)))
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        # Back in object mode first, so the materials go back on the mesh
        # and not on an edit copy that is thrown away.
        try:
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass
        _restore_materials(hidden)
        for o in objs:
            try:
                o.select_set(False)
            except RuntimeError:
                pass


def _balance(objs, groups):
    """Share the meshes over N groups, the biggest first into the emptiest.

    Packing scales islands to fill the tile, so what matters is that each
    tile carries a similar amount of surface, not a similar part count.
    """
    out = [[] for _ in range(groups)]
    load = [0] * groups
    for o in sorted(objs, key=lambda x: -len(x.data.polygons)):
        i = load.index(min(load))
        out[i].append(o)
        load[i] += len(o.data.polygons)
    return [g for g in out if g]


def _pack_uv_objects(objs, mode, tiles=4, margin=UV_PACK_MARGIN, scale=True):
    """Pack the UV islands. Unique meshes only, so linked copies come free.

    ALL     one tile for the whole import, ready to merge and texture as one
    OBJECT  one tile per part, each packed on its own
    UDIM    the parts shared over a fixed number of tiles

    `scale` comes from Normalize UVs. With it off the packer keeps every
    island the size it already is and only arranges them, so the real-world
    UV scale survives the pack. The result can then be larger than one tile,
    which is why UDIM always scales: its tiles are a fixed grid and an
    island that overruns one lands in the next.
    """
    if mode in (None, "NONE"):
        return
    seen = set()
    targets = []
    for o in objs:
        if (o is None or getattr(o, "type", None) != "MESH"
                or o.data is None or o.data in seen
                or not len(o.data.polygons)
                or not len(o.data.uv_layers)):
            continue
        seen.add(o.data)
        targets.append(o)
    if not targets:
        return

    t0 = time.time()
    view_layer = bpy.context.view_layer
    active = view_layer.objects.active
    targets, holders = _editable(targets)
    try:
        _deselect_all()
        if mode == "ALL":
            for o in targets:
                _uv_home(o.data)
            _pack_group(targets, margin, scale)
            used = 1
        elif mode == "OBJECT":
            # One pack for each part. This is the slow one by nature: the
            # packer has to run once per tile, and here every part is a
            # tile of its own.
            for o in targets:
                _uv_home(o.data)
                _pack_group([o], margin, scale)
            used = len(targets)
        else:
            if not scale:
                print("UV pack: UDIM tiles need scaling, so island scale is "
                      "not kept for this mode")
            groups = _balance(targets, max(1, int(tiles)))
            for i, group in enumerate(groups):
                for o in group:
                    _uv_home(o.data)
                _pack_group(group, margin, True)
                du, dv = i % UDIM_ROW, i // UDIM_ROW
                if du or dv:
                    for o in group:
                        _uv_move(o.data, du, dv)
            used = len(groups)
        print("UV pack (%s): %d mesh(es) into %d tile(s) in %.2fs"
              % (mode.lower(), len(targets), used, time.time() - t0))
    except Exception as e:
        print(f"UV pack failed: {e}")
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    finally:
        if holders:
            bpy.data.batch_remove(holders)
            view_layer.objects.active = active


def _tris_to_quads_objects(objs):
    """Pair the tessellation triangles back into quads, on unique meshes.

    OCCT tessellates to triangles, and a flat or lightly curved CAD face
    comes out as long thin pairs that go back together cleanly. This is the
    same work bpy.ops.mesh.tris_convert_to_quads does. It runs on the mesh
    data instead of through the operator, because the operator needs edit
    mode and one mode change per part costs more than the join.

    Every comparison is on, so a pair is never joined across a material, a
    UV island, a seam or a sharp edge. Both angle limits are open at 180
    degrees. They exist to protect a hand-made mesh from ugly joins, and a
    CAD tessellation has nothing to protect: its thin triangles are exactly
    the ones that belong together.

    Custom normals are stored as offsets from the normals Blender
    calculates, and a quad has a different calculated normal than its two
    triangles. So the normals are copied out before the join and written
    back after it (tools.stash_normals). The mesh keeps the exact CAD
    shading it was given.
    """
    seen = set()
    meshes = tris = quads = 0
    for o in objs:
        if (o is None or getattr(o, "type", None) != "MESH"
                or o.data is None or o.data in seen
                or not len(o.data.polygons)):
            continue
        seen.add(o.data)
        me = o.data
        before = len(me.polygons)
        tools_mod.stash_normals(me)
        bm = bmesh.new()
        try:
            bm.from_mesh(me)
            bmesh.ops.join_triangles(
                bm, faces=bm.faces[:],
                angle_face_threshold=math.pi, angle_shape_threshold=math.pi,
                topology_influence=2.0, deselect_joined=False,
                cmp_seam=True, cmp_sharp=True, cmp_uvs=True,
                cmp_vcols=True, cmp_materials=True)
            bm.to_mesh(me)
            meshes += 1
            tris += before
            quads += len(me.polygons)
        except Exception as e:
            print(f"Tris to quads failed on {me.name}: {e}")
        finally:
            bm.free()
            tools_mod.restore_normals(me)
            me.update()
    if meshes:
        print(f"Tris to quads: {meshes} mesh(es), {tris} faces -> {quads}")
    return meshes


def _unwrap_uv_objects(objs, world_scale=None, method="CONFORMAL"):
    """Blender's unwrap with packed islands into the 'UVMap' layer.

    `method` is the unwrap operator's own method name, which is also the
    identifier of the unwrap mode in the UV Map dropdown. Runs Blender's
    unwrap operator once over all given objects in multi-object edit mode. The imported seams (marked at sharp normal
    discontinuities between CAD faces) define the islands. The unwrap
    operator packs them into the 0-1 square per mesh.

    world_scale: None keeps the packed 0-1 layout (Normalize UVs on).
    Otherwise the packed islands are uniformly rescaled so 1 UV unit
    matches 1 scene unit as closely as possible (median of the per-triangle
    3D/UV area ratios). The value is the mesh-unit -> scene-unit factor
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
    active = view_layer.objects.active
    targets, holders = _editable(targets)
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
        # no_flip keeps a face from turning itself inside out, which is
        # what leaves an island folded on top of itself. correct_aspect is
        # off so the UVs are for a square tile. On, it reads the image in
        # the material and squashes the UVs to its proportions.
        try:
            bpy.ops.uv.unwrap(method=method, margin=0.005, no_flip=True,
                              correct_aspect=False)
        except TypeError:
            bpy.ops.uv.unwrap(method=method, margin=0.005,
                              correct_aspect=False)
        bpy.ops.object.mode_set(mode="OBJECT")
        if world_scale is not None:
            for o in targets:
                _scale_unwrap_to_world(o.data, world_scale)
        print(f"UVMap unwrap ({method.lower()}): {len(targets)} mesh(es)"
              + ("" if world_scale is None else " (real-world scale)"))
    except Exception as e:
        print(f"UVMap unwrap failed: {e}")
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    finally:
        if holders:
            bpy.data.batch_remove(holders)
            view_layer.objects.active = active


def _unwrap_compound_objects(targets, method="MINIMUM_STRETCH"):
    """Give Blender's unwrap the compound surfaces of these parts.

    CAD Surfaces gives every face the chart of its own surface. A plane, a
    cylinder and a cone unroll into that chart with no error at all. A
    sphere, a torus and a spline do not unroll, so their chart stretches
    wherever the surface does, and no scale puts it right. Those faces, and
    only those, go to the unwrap here: it is what a user does by hand
    today, and what comes back is treated like any other chart.

    The work is per MESH, not per object. An assembly holds one mesh in
    many places, and a large one holds it in thousands (Conveyor12k-A00: 11761
    objects over 140 meshes, Oscar, 2026-09-17).

    The unwrap operator needs edit mode, and a mesh here may belong to no
    object in the view layer at all, such as the prototype inside a
    collection an empty instances. So each mesh is put in a holder of its
    own for the length of the unwrap. Nothing the user had selected moves.

    Returns (faces, islands).
    """
    meshes = []
    seen = set()
    for o in targets or []:
        for holder in tools_mod.mesh_parts([o]):
            me = getattr(holder, "data", None)
            if not isinstance(me, bpy.types.Mesh) or me.name in seen:
                continue
            seen.add(me.name)
            meshes.append(me)

    picked = []
    for me in meshes:
        try:
            mask = uv_mod.strained(me)
        except Exception as e:
            print(f"UV strain test failed on {me.name}: {e}")
            continue
        if mask is None or not mask.any():
            continue
        # The unwrap reads the seams. Without one on every chart boundary
        # it welds two charts into one island, and without the cut a closed
        # face already carries it has nowhere to open that face.
        uv_mod.mark_seams(me)
        picked.append((me, mask, uv_mod.read_uvs(me)))
    if not picked:
        return 0, 0

    view_layer = bpy.context.view_layer
    tool = bpy.context.tool_settings
    select_mode = tuple(tool.mesh_select_mode)
    held = [o for o in view_layer.objects if o.select_get()]
    active = view_layer.objects.active
    holders = []
    faces = 0
    try:
        for o in held:
            o.select_set(False)
        for me, _mask, _was in picked:
            holder = bpy.data.objects.new("CADder UV", me)
            bpy.context.scene.collection.objects.link(holder)
            holder.select_set(True)
            holders.append(holder)
        view_layer.objects.active = holders[0]
        bpy.ops.object.mode_set(mode="EDIT")
        tool.mesh_select_mode = (False, False, True)
        bpy.ops.mesh.select_all(action="DESELECT")
        for me, mask, _was in picked:
            bm = bmesh.from_edit_mesh(me)
            bm.faces.ensure_lookup_table()
            for i in np.flatnonzero(mask).tolist():
                bm.faces[i].select_set(True)
            bmesh.update_edit_mesh(me, loop_triangles=False,
                                   destructive=False)
            faces += int(mask.sum())
        try:
            bpy.ops.uv.unwrap(method=method, margin=0.001, no_flip=True,
                              correct_aspect=False)
        except TypeError:
            bpy.ops.uv.unwrap(method=method, margin=0.001,
                              correct_aspect=False)
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as e:
        print(f"UV unwrap of the compound surfaces failed: {e}")
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        faces = 0
    finally:
        tool.mesh_select_mode = select_mode
        for holder in holders:
            bpy.data.objects.remove(holder)
        for o in held:
            try:
                o.select_set(True)
            except RuntimeError:
                pass
        view_layer.objects.active = active
    if not faces:
        return 0, 0

    # The unwrap packs what it made into the 0 to 1 square. Put each island
    # back at the size the CAD charts of the part are in.
    islands = 0
    for me, mask, was in picked:
        islands += uv_mod.fit_charts(me, mask, was)
    return faces, islands


def _smart_merge_objects(objs, pack="NONE", tiles=4,
                         distortion=UV_SMART_DISTORTION, sharp=False,
                         split=True, unwrap=True):
    """Grow the Smart UV islands on every unique mesh.

    `distortion` is the Smart distortion setting in percent. `sharp` lets
    Smart join across sharp edges as well. `split` lets Smart cut an island
    where its pieces pack better. `unwrap` sends the faces that no one
    scale can flatten to Blender's unwrap before any of that.

    The tile an island has to fit comes from Pack UVs. One tile for each
    part, or no packing, gives every part its own. All parts together puts
    the whole import in one tile, so an island may be far longer before it
    stops fitting, and UDIM shares the import over the tiles asked for.
    """
    targets = []
    seen = set()
    for o in objs:
        if (o is None or getattr(o, "type", None) != "MESH"
                or o.data is None or o.data in seen
                or not len(o.data.polygons)
                or not len(o.data.uv_layers)):
            continue
        seen.add(o.data)
        targets.append(o)
    if not targets:
        return
    if unwrap:
        t_un = time.time()
        faces, made = _unwrap_compound_objects(targets)
        if faces:
            print("UV smart unwrap: %d face(s) that one scale cannot "
                  "flatten into %d island(s) in %.2fs"
                  % (faces, made, time.time() - t_un))
    side = None
    if pack in ("ALL", "UDIM"):
        total = 0.0
        for o in targets:
            a = np.empty(len(o.data.polygons), dtype=np.float64)
            o.data.polygons.foreach_get("area", a)
            total += float(a.sum())
        if pack == "UDIM":
            total /= max(1, int(tiles))
        side = math.sqrt(total / uv_mod.SMART_FILL)
    t0 = time.time()
    charts = islands = 0
    for o in targets:
        try:
            c, n = uv_mod.smart_merge(o.data, side_3d=side,
                                      distortion=distortion / 100.0,
                                      sharp=bool(sharp),
                                      split=bool(split))
        except Exception as e:
            print(f"UV smart merge failed on {o.name}: {e}")
            continue
        charts += c
        islands += n
    print("UV smart merge: %d tangent chart(s) into %d island(s) on %d "
          "mesh(es) in %.2fs" % (charts, islands, len(targets),
                                 time.time() - t0))


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


def _region_labels(a, b, n):
    """Connected components over the triangle pairs (a, b).

    Label propagation with pointer jumping. It is the same method the closed
    surface test uses, and it stays in numpy, so a 100,000 triangle part
    costs a few passes over an array instead of a Python graph walk.
    """
    labels = np.arange(n, dtype=np.int64)
    if not len(a):
        return labels
    while True:
        prev = labels
        mn = np.minimum(labels[a], labels[b])
        labels = labels.copy()
        np.minimum.at(labels, a, mn)
        np.minimum.at(labels, b, mn)
        labels = labels[labels]
        if np.array_equal(labels, prev):
            break
    return labels


def _boundary_loops(labels, face_of, he0, he1, open_he, va, vb, max_v,
                    regions=None):
    """The boundary loops of each region, as loops of half edges.

    A half edge bounds its region when the face on the other side is in a
    different region, or when there is no other side. A seam with the same
    region on both sides is a slit inside the region, not an edge of it, so
    it is left out. Two edges of one region that share a vertex belong to
    the same loop.

    Returns (half edges, region of each, loop id of each). Loop ids run from
    0. `regions` limits the work to those region ids.
    """
    r0 = labels[face_of[he0]]
    r1 = labels[face_of[he1]]
    split = r0 != r1
    hes = np.concatenate([he0[split], he1[split], open_he])
    reg = labels[face_of[hes]]
    if regions is not None:
        keep = np.isin(reg, regions)
        hes, reg = hes[keep], reg[keep]
    if not len(hes):
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, empty
    node_a = reg * np.int64(max_v) + va[hes].astype(np.int64)
    node_b = reg * np.int64(max_v) + vb[hes].astype(np.int64)
    nodes, inv = np.unique(np.concatenate([node_a, node_b]),
                           return_inverse=True)
    k = len(hes)
    comp = _region_labels(inv[:k], inv[k:], len(nodes))[inv[:k]]
    _ids, loop_of = np.unique(comp, return_inverse=True)
    return hes, reg, loop_of


# A boundary loop wraps round the part when the surface normal turns
# through this many full turns as the loop goes round once. The foot of a
# cylinder turns once. Every loop of a flat pattern turns zero times, even
# the outline of a U channel, which goes out over the bends and back again.
WRAP_MIN_TURNS = 0.5


def _sheet_regions(bad_ids, chi, labels, face_of, he0, he1, open_he, va, vb,
                   corner_a, corner_b, loop_norms, he_len, max_v):
    """Which of the regions that fail the disk test are sheets with holes.

    The closed surface test asks whether a region is a disk, and a region
    with holes is not one: a disk with h holes has an Euler characteristic of
    1 - h. But a tube is not one either, and topology cannot tell the two
    apart. A plate with one hole and a length of pipe are both annuli.

    Geometry can. Walk a boundary loop and watch the surface normal. Round
    the end of a pipe it turns through a full circle. Round a hole in a
    plate, or round the outline of a bent plate, it comes back to where it
    started without going round. A region is a sheet when it has no handle
    (genus 0), has holes, and no loop of it goes round.

    Without this the test cut the bend lines of every sheet metal part that
    has holes, trying to reach a disk it could never reach while the holes
    were there.

    Returns the ids from `bad_ids` that are sheets.
    """
    if not len(bad_ids):
        return bad_ids
    hes, reg, lid = _boundary_loops(labels, face_of, he0, he1, open_he,
                                    va, vb, max_v, regions=bad_ids)
    if not len(hes):
        return bad_ids[:0]
    n_all = int(lid.max()) + 1
    na = loop_norms[corner_a[hes]].astype(np.float64)
    nb = loop_norms[corner_b[hes]].astype(np.float64)
    w = he_len[hes]

    # The axis a loop could wind round is the direction its normals vary
    # least along: the axis of a cylinder, or the length of a U channel.
    cov = np.zeros((n_all, 3, 3), dtype=np.float64)
    np.add.at(cov, lid, na[:, :, None] * na[:, None, :] * w[:, None, None])
    _vals, vecs = np.linalg.eigh(cov)
    axis = vecs[:, :, 0][lid]

    # The winding number of the normal about that axis. Each edge adds the
    # angle the normal turns along it, so the loop needs no ordering: the
    # faces of a region wind one way, and so do its boundary edges.
    pa = na - axis * np.sum(na * axis, axis=1, keepdims=True)
    pb = nb - axis * np.sum(nb * axis, axis=1, keepdims=True)
    good = ((np.linalg.norm(pa, axis=1) > 0.1)
            & (np.linalg.norm(pb, axis=1) > 0.1))
    turn = np.where(good,
                    np.arctan2(np.sum(np.cross(pa, pb) * axis, axis=1),
                               np.sum(pa * pb, axis=1)), 0.0)
    turns = np.bincount(lid, weights=turn, minlength=n_all) / (2.0 * np.pi)
    wraps = np.abs(turns) > WRAP_MIN_TURNS

    loop_reg = np.zeros(n_all, dtype=np.int64)
    loop_reg[lid] = reg
    ids, pos = np.unique(loop_reg, return_inverse=True)
    n_loops = np.bincount(pos, minlength=len(ids))
    n_wrap = np.bincount(pos, weights=wraps.astype(np.float64),
                         minlength=len(ids))
    chi_of = dict(zip(bad_ids.tolist(), chi.tolist()))
    # Genus 0 with b boundary loops has an Euler characteristic of 2 - b.
    sheets = [int(r) for r, b, nw in zip(ids.tolist(), n_loops.tolist(),
                                          n_wrap.tolist())
              if b >= 2 and chi_of.get(int(r), 1) == 2 - b and nw == 0]
    return np.array(sheets, dtype=bad_ids.dtype)


# How many times to cut one closed region before giving up. A tube needs
# one, a ring two. Past a handful the shape is unusual enough that more cuts
# are unlikely to help, and each round costs a pass over every edge.
MAX_CLOSED_CUTS = 16


def _one_seam_per_region(edge_keys, mask, region_of, face_a, face_b):
    """Pick one CAD face boundary in each region and return only its edges.

    A closed region such as a hole made of two half cylinders has two
    boundaries between its faces. Cutting both gives two half shells.
    Cutting one opens the tube into a single flat island, which wastes less
    texture and leaves no seam down the middle of the hole.

    The longest boundary wins, so the cut runs the length of the hole rather
    than around a fillet at one end.
    """
    keys = edge_keys[mask]
    if not len(keys):
        return keys
    region = region_of[mask].astype(np.int64)
    lo = np.minimum(face_a[mask], face_b[mask]).astype(np.int64)
    hi = np.maximum(face_a[mask], face_b[mask]).astype(np.int64)
    span = np.int64(max(int(hi.max()), int(lo.max())) + 1)
    group = (region * span + lo) * span + hi

    uniq, counts = np.unique(group, return_counts=True)
    group_region = uniq // (span * span)
    # Biggest boundary first, then one per region.
    order = np.lexsort((-counts, group_region))
    chosen = []
    seen = set()
    for i in order:
        r = int(group_region[i])
        if r in seen:
            continue
        seen.add(r)
        chosen.append(uniq[i])
    if not chosen:
        return keys[:0]
    return keys[np.isin(group, np.asarray(chosen, dtype=np.int64))]


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

    # Smooth joins between CAD faces get no seam here. Smart decides its own
    # later, once the mesh exists and its charts can be laid out. Cutting
    # every CAD face boundary here would also undo Closed surfaces: a hole
    # made of two half cylinders can only stay in one island while the
    # boundary between its halves carries no seam.
    # --- Parametric closure seams (closed cylinders/cones/tori/splines) ---
    # A closed face has NO sharp edge along its parametric seam, so unwrap
    # has nowhere to cut and produces a degenerate result. Detection: OCC
    # duplicates the seam vertices in parameter space (u=0 and u=period).
    # The position-based vertex fuse welds them, but the per-corner UV
    # snapshot still disagrees across the edge. So an interior edge WITHIN
    # one OCC face whose corner UVs differ at BOTH endpoints lies on the
    # closure (the both-endpoints rule keeps collapsed poles, where every
    # touching edge has one discontinuous corner, out of the seam set).
    # Marked as UV seam only, never sharp: shading stays smooth.
    closed_mode = _uv_options.get("closed_seams", "SINGLE")
    if closed_mode != "NONE":
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
        # One cut opens a tube, but a region can need more than one: a ring
        # needs two, and a shape with more handles needs one for each. Cut,
        # look again, and stop when every region is a disk. Split faces cuts
        # everything at once, so one pass does it.
        open_idx = np.where(counts == 1)[0]
        open_he = order[group_starts[open_idx]]
        he_len = np.linalg.norm(
            (verts[va] - verts[vb]).astype(np.float64), axis=1)
        for _round in range(1 if closed_mode == "SPLIT" else MAX_CLOSED_CUTS):
            smooth_pair = ~np.isin(int_edge_keys, seam_keys)
            a = face_of[he0][smooth_pair]
            b = face_of[he1][smooth_pair]
            if not len(a):
                break
            # Connected components via label propagation + pointer jumping
            labels = np.arange(T, dtype=np.int64)
            while True:
                prev = labels
                mn = np.minimum(labels[a], labels[b])
                labels = labels.copy()
                np.minimum.at(labels, a, mn)
                np.minimum.at(labels, b, mn)
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
                # A sheet with holes fails the disk test too, and cutting it
                # does not help. Leave those regions whole.
                sheets = _sheet_regions(
                    bad_ids, chi[chi != 1], labels, face_of, he0, he1,
                    open_he, va, vb, corner_a, corner_b, loop_norms, he_len,
                    max_v)
                if len(sheets):
                    bad_ids = bad_ids[~np.isin(bad_ids, sheets)]
            if not len(bad_ids):
                break
            cand = smooth_pair & cross_batch
            edge_comp = labels[face_of[he0]]
            in_bad = cand & np.isin(edge_comp, bad_ids)
            if not in_bad.any():
                # Nothing left to cut. The region has no CAD face boundary
                # inside it, so a seam would have to be invented across the
                # middle of a face, which is worse than leaving it.
                break
            if closed_mode == "SPLIT":
                # Seam every boundary inside the region. A hole made of two
                # half cylinders becomes two islands.
                extra = int_edge_keys[in_bad]
            else:
                # Seam the longest boundary in each region and leave the
                # rest joined, so a hole unrolls into a single island
                # instead of two half shells.
                extra = _one_seam_per_region(
                    int_edge_keys, in_bad, edge_comp,
                    batches[face_of[he0]], batches[face_of[he1]])
            if not len(extra):
                break
            before_n = len(seam_keys)
            seam_keys = np.unique(np.concatenate([seam_keys, extra]))
            if len(seam_keys) == before_n:
                break

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


def build_mesh(step_reader, obj, shp, lind, angd, vcol_name="Colors",
               relative=False, part_name="", fallback_color=None):
    """Tessellate one shape into obj.data. `relative` says that `lind` is a
    share of each edge and not a distance in file units. It must go
    through, or a share of 0.005 cuts the mesh at 0.005 file units."""
    hacks = set([])
    if _get_addon_prefs().hack_skip_zero_solids:
        hacks.add("skip_solids")

    mesh, colors, mat_names, norms, uvs = precompute_mesh_data(
        step_reader, shp, lind, angd, hacks, part_name=part_name,
        fallback_color=fallback_color, relative=relative)

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


# The parts the last import could not build or had to recover. The
# background worker reads this and sends it to the session that started it,
# which shows the popup.
last_import_issues = ([], [])


def _show_import_issues_popup(failed_parts, recovered_parts):
    """Show a Blender popup dialog listing parts that had import problems.

    A Blender with no window, such as the background worker, has no popup
    to show, and Blender crashes if it tries. There the lists go to the
    console only.
    """
    global last_import_issues
    last_import_issues = (list(failed_parts), list(recovered_parts))
    if bpy.app.background:
        for name in failed_parts:
            print("Import warning: %s produced no geometry" % name)
        for name in recovered_parts:
            print("Import warning: %s had corrupted geometry and was "
                  "recovered" % name)
        return

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
    bpy.context.window_manager.popup_menu(draw, title="CADder Import Warning", icon=icon)


# ---------------------------------------------------------------------------
# Material Database helpers
# ---------------------------------------------------------------------------

# Keeps the old product name on purpose. This is the name of a text
# datablock INSIDE every material database file a user already has.
# Renaming it would make those files unreadable.
_MATDB_TEXT_NAME = "STEPper_MaterialDB"
_ADDON_DIR = os.path.dirname(os.path.realpath(__file__))


def _matdb_user_dir():
    """The MaterialDB folder Blender keeps for this extension, or None.

    Blender deletes the folder of an extension on every upgrade, and the
    update notice sends the user straight into one. This folder is outside
    it, so the databases survive. A legacy install (scripts/addons) has no
    such folder and gets None."""
    try:
        return bpy.utils.extension_path_user(
            __package__, path="MaterialDB", create=True)
    except Exception:
        # ValueError for a legacy install, OSError when it cannot be made.
        return None


def _get_matdb_dir():
    """The folder holding the material databases.

    The user folder of the extension by default, and a folder the user
    names in preferences when they set one. Only a legacy install uses the
    MaterialDB folder inside the addon, which a reinstall wipes. The default
    folder cannot be shared between machines or with a team, which is
    exactly what a material library is for."""
    custom = ""
    try:
        custom = (_get_addon_prefs().matdb_dir or "").strip()
    except Exception:
        # Called from an enum callback before the preferences exist.
        custom = ""
    if custom:
        d = bpy.path.abspath(custom)
        # A path that is not there yet is created, the same as the built-in
        # one. A path that cannot be made is reported once and falls back,
        # because losing the databases is worse than ignoring the setting.
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError as exc:
            global _matdb_dir_warned
            if _matdb_dir_warned != d:
                _matdb_dir_warned = d
                print("CADder: material database folder %r is "
                      "unusable (%s). Using the default folder" % (d, exc))

    d = _matdb_user_dir()
    if d:
        return d
    d = os.path.join(_ADDON_DIR, "MaterialDB")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        # Read-only install (system-wide): callers treat a missing dir as
        # "no databases". Raising here would crash the enum callback
        pass
    return d


_matdb_dir_warned = ""


def _move_addon_matdbs(src=None, dst=None):
    """Move the databases out of the addon folder, into the user folder.

    Older versions kept them in the MaterialDB folder inside the addon, and
    the next upgrade deletes that folder. Runs on register, so the move is
    done before the user can start an upgrade. A name the user folder
    already has stays where it is: that copy is the one in use. Returns the
    number of databases moved."""
    import shutil

    if src is None:
        src = os.path.join(_ADDON_DIR, "MaterialDB")
    if dst is None:
        dst = _matdb_user_dir()
    if not dst or not os.path.isdir(src):
        return 0
    if os.path.normcase(os.path.realpath(src)) == os.path.normcase(
            os.path.realpath(dst)):
        return 0
    moved = 0
    for f in sorted(os.listdir(src)):
        if not f.lower().endswith(".blend"):
            continue
        target = os.path.join(dst, f)
        if os.path.exists(target):
            print("CADder: material database %r is in both folders. "
                  "Keeping the one in %s" % (f, dst))
            continue
        try:
            os.makedirs(dst, exist_ok=True)
            shutil.move(os.path.join(src, f), target)
            moved += 1
        except OSError as exc:
            print("CADder: could not move material database %r (%s)"
                  % (f, exc))
    if moved:
        print("CADder: moved %d material database(s) to %s" % (moved, dst))
    return moved


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


# An enum stores the number of the selected item, not its name. The number
# was the position in the folder listing, and that changes when a file is
# added to or removed from the folder (a shared team folder does this all
# the time). A stored choice then read as another database. So each
# database gets a number made from its name. The numbers start above any
# position that an older version stored, see _migrate_active_matdb.
_MATDB_VALUE_BASE = 1 << 16
# The dropdown menu keeps the number of each item as a float, and a pick
# writes that float back. A float holds each whole number up to 2**24, and
# rounds a larger one: the pick then stored a number that no database has,
# and the dropdown went blank.
_MATDB_VALUE_LIMIT = 1 << 24


def _matdb_value(name, taken):
    """The enum number of one database: the same in every session, whatever
    else is in the folder."""
    import zlib
    span = _MATDB_VALUE_LIMIT - _MATDB_VALUE_BASE
    value = zlib.crc32(name.encode("utf-8")) % span
    # Two names with one number are very rare, but must never read as
    # each other. The listing is sorted, so the step is the same each time.
    while _MATDB_VALUE_BASE + value in taken:
        value = (value + 1) % span
    return _MATDB_VALUE_BASE + value


def _matdb_enum_items(self, context):
    """Dynamic enum items for material database selection. Each item has
    the number _matdb_value gives its name, and None keeps 0."""
    global _matdb_enum_cache
    items = [("NONE", "None", "Do not use a material database", 0)]
    taken = {0}
    for name, path in _list_matdb_files():
        value = _matdb_value(name, taken)
        taken.add(value)
        items.append((name, name, f"Use material database: {name}", value))
    # Blender requires the returned strings to stay referenced from Python.
    # The module-level cache prevents garbage values in the dropdown
    _matdb_enum_cache = items
    return items


_matdb_enum_cache = []


def _migrate_active_matdb():
    """Keep the database that an older version had selected.

    Up to 1.0.1 the preference stored a position in the folder listing,
    which is below _MATDB_VALUE_BASE and matches no item now. It is read
    once against the listing, as the older version read it, and stored
    again by name. A number at or above _MATDB_VALUE_LIMIT is from a
    development build, see _matdb_from_wide_value. Called from register()."""
    try:
        prefs = _get_addon_prefs()
        stored = prefs.bl_system_properties_get()
        raw = stored.get("active_matdb") if stored is not None else None
        if not isinstance(raw, int) or _MATDB_VALUE_BASE <= raw < _MATDB_VALUE_LIMIT:
            return
        names = [name for name, _path in _list_matdb_files()]
        if 0 < raw < _MATDB_VALUE_BASE:
            prefs.active_matdb = names[raw - 1] if raw <= len(names) else "NONE"
        elif raw >= _MATDB_VALUE_LIMIT:
            prefs.active_matdb = _matdb_from_wide_value(raw, names)
    except Exception as exc:
        # A lost choice must never stop the addon from loading.
        print("CADder: could not read the older database choice:", exc)


def _matdb_from_wide_value(raw, names):
    """The database that a development build stored as raw, or "NONE".

    Those builds made the number from the whole 31-bit range. A choice set
    from Python kept that number, and a pick from the dropdown kept it
    rounded to a float. Both read as the same database here."""
    import struct
    import zlib

    def as_float(value):
        return struct.unpack("<f", struct.pack("<f", value))[0]

    span = 0x7FFFFFFF - _MATDB_VALUE_BASE
    for name in names:
        wide = _MATDB_VALUE_BASE + zlib.crc32(name.encode("utf-8")) % span
        if as_float(wide) == as_float(raw):
            return name
    return "NONE"


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
            print(f"CADder MatDB: Material '{replacement_name}' not found, skipping")
            continue
        if mat.library or mat.override_library:
            # Create a full local copy so it can be written to the database
            local_copy = mat.copy()
            local_copy.name = replacement_name  # keep the expected name
            datablocks.add(local_copy)
            temp_copies.append(local_copy)
        else:
            datablocks.add(mat)

    print(f"CADder MatDB: Writing {len(datablocks)} datablocks to {filepath}")
    # A relative path (a texture next to the blend, Blender's default for a
    # new image) is relative to THIS file. Blender reads the paths in the
    # database from the database's own folder, so written as they are they
    # pointed at nothing. RELATIVE rebases them to the database file.
    try:
        bpy.data.libraries.write(filepath, datablocks, path_remap="RELATIVE",
                                 fake_user=True)
    finally:
        # Also when the write fails (a read-only or lost folder). A copy
        # left behind has the linked material's name, and a later lookup
        # by that name can find the copy instead of the linked material.
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
            print("CADder MatDB: Invalid JSON in database file")
        bpy.data.texts.remove(text)
    else:
        print("CADder MatDB: No mapping text block found in database file")

    return mappings


def _append_matdb_materials(filepath, keep=False):
    """Append all materials from a database .blend into the current file.

    Materials that already exist locally (by name) are skipped to avoid
    duplicates.  Returns the number of materials appended.

    keep: give every material of the database a fake user, so it stays in
    the file when nothing uses it yet. Load does this: it puts the
    database's materials in the file to pick for other entries, and
    Blender does not save a material with no users.
    """
    abs_path = bpy.path.abspath(filepath)
    if not abs_path or not os.path.isfile(abs_path):
        return 0

    with bpy.data.libraries.load(abs_path, link=False) as (data_from, data_to):
        names = list(data_from.materials)
        to_append = [m for m in names if m not in bpy.data.materials]
        data_to.materials = to_append

    if keep:
        for name in names:
            mat = bpy.data.materials.get(name)
            if mat is not None and mat.library is None:
                mat.use_fake_user = True

    count = len(to_append)
    if count:
        print(f"CADder MatDB: Appended {count} material(s) from database")
    return count


def _original_names(obj):
    """The name each material slot of an object had when CADder made it,
    or None for an object that is not a CAD part.

    STEP_materials says it. An import older than that property has only
    its current materials, which are taken as the originals. The user's
    own objects (a backdrop, a light, the default cube) have neither."""
    data = obj.data
    if data is None or not hasattr(data, "materials"):
        return None
    names = material_lock.originals(obj)
    if names is not None:
        return names
    if (obj.get("STEP_file") is not None
            or obj.get("STEP_tag") is not None
            or obj.get("SWMESH_file") is not None):
        return [m.name for m in data.materials if m]
    return None


def _scan_scene_materials():
    """Scan scene for STEP objects and determine material mappings.

    For each original STEP material name, find what replacement material is
    currently assigned.  When an original name maps to multiple different
    replacements, pick the one with the highest instance count.

    Returns dict {original_name: replacement_name}.
    """
    original_to_replacements = {}  # {orig: Counter({repl: count})}

    for obj in bpy.data.objects:
        # The user's own objects are left out: their materials went into
        # the database, and every later import appended them.
        original_names = _original_names(obj)
        if original_names is None:
            continue

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
    # A file Blender cannot read (a copy to a shared folder that stopped
    # half way) raised out of the import after every part was built, and
    # left the parts unscaled, on the wrong up axis and unrecorded. Without
    # the database the import still completes.
    try:
        _append_matdb_materials(db_path)
        return _read_matdb_mappings(db_path)
    except (OSError, RuntimeError) as exc:
        print("CADder MatDB: cannot read %s (%s). CADder skips the material "
              "database" % (db_path, str(exc).strip()))
        return {}


def _apply_matdb_to_objects(objects, mappings, locked_out=None):
    """Replace STEP materials on *objects* according to *mappings* dict.

    Uses the STEP_materials custom property to determine original material
    names so replacements work even when materials were already swapped by
    a previous database apply.

    A locked part keeps its materials (material_lock.py). locked_out, a
    list, gets the objects left alone for that.

    Returns the number of material slots replaced.
    """
    if not mappings:
        return 0

    print(f"Applying material database ({len(mappings)} mappings)")

    replaced = 0
    processed_meshes = set()
    locked = material_lock.locked_meshes()
    for obj in objects:
        if obj.data is None or not hasattr(obj.data, 'materials'):
            continue
        mesh = obj.data
        if mesh in processed_meshes:
            continue
        processed_meshes.add(mesh)
        if mesh in locked:
            if locked_out is not None:
                locked_out.append(obj)
            continue

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
    (e.g. the original-name keys of the active matdb mappings). CAD color
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
        print(f"CADder MatDB: Removed {removed} unused material(s)")
    return removed


# The Material Database list asks on every redraw which entries the parts
# of the scene use, and a scene can hold ten thousand parts. So the answer
# is kept, and worked out again only after a change that can alter it.
_usage = {"scene": 0, "names": frozenset(), "locked": 0, "geometry": 0,
          "stale": True, "frame": None}


@bpy.app.handlers.persistent
def _usage_stale(*_args):
    _usage["stale"] = True


@bpy.app.handlers.persistent
def _usage_on_update(scene, depsgraph):
    """Mark the kept answer stale after a change that can alter it: an
    object added, removed or moved to another collection, a selection, or
    a material put in a slot. A part that only moves changes nothing, and
    it moves on every frame of a drag or of playback."""
    frame = scene.frame_current
    new_frame = frame != _usage["frame"]
    _usage["frame"] = frame
    if _usage["stale"] or new_frame:
        return
    for update in depsgraph.updates:
        datablock = update.id
        if isinstance(datablock, bpy.types.Object):
            if update.is_updated_shading:
                _usage["stale"] = True
                return
        elif isinstance(datablock, (bpy.types.Mesh, bpy.types.Scene,
                                    bpy.types.Collection)):
            _usage["stale"] = True
            return


def _scene_usage(scene):
    """(the original material names that the parts of this scene carry,
    the number of locked parts in it)."""
    pointer = scene.as_pointer()
    if not _usage["stale"] and _usage["scene"] == pointer:
        return _usage["names"], _usage["locked"]
    names, locked = set(), 0
    parsed = {}
    seen = set()

    def carry(obj):
        # Most parts share their STEP_materials text with other parts, so
        # each text is read once.
        raw = obj.get("STEP_materials")
        if raw and isinstance(raw, str):
            if raw not in parsed:
                parsed[raw] = material_lock.originals(obj) or ()
            names.update(n for n in parsed[raw] if n)
            return
        found = _original_names(obj)
        if found:
            names.update(n for n in found if n)

    def instanced(collection):
        if collection in seen:
            return
        seen.add(collection)
        for inner in collection.all_objects:
            carry(inner)
            if (inner.instance_type == "COLLECTION"
                    and inner.instance_collection is not None):
                instanced(inner.instance_collection)

    geometry = 0
    for obj in scene.objects:
        if material_lock.is_locked(obj):
            locked += 1
        if geometry_lock.is_locked(obj):
            geometry += 1
        carry(obj)
        if (obj.instance_type == "COLLECTION"
                and obj.instance_collection is not None):
            instanced(obj.instance_collection)
    _usage.update(scene=pointer, names=frozenset(names), locked=locked,
                  geometry=geometry, stale=False)
    return _usage["names"], locked


def _geometry_locked(scene):
    """The number of parts in this scene whose geometry is locked. Kept
    with the answer of _scene_usage, so a redraw costs no scan."""
    _scene_usage(scene)
    return _usage["geometry"]


def _parts_with(view_layer, name):
    """The parts in the view layer that carry the CAD material `name`,
    directly or through the collection they instance: (the ones that can
    be selected, the number of hidden or unselectable ones)."""
    memo = {}

    def has(obj):
        found = _original_names(obj)
        return bool(found) and name in found

    def in_collection(collection):
        if collection not in memo:
            memo[collection] = False
            memo[collection] = any(
                has(inner) or (inner.instance_type == "COLLECTION"
                               and inner.instance_collection is not None
                               and in_collection(inner.instance_collection))
                for inner in collection.all_objects)
        return memo[collection]

    shown, hidden = [], 0
    for obj in view_layer.objects:
        hit = has(obj) or (obj.instance_type == "COLLECTION"
                           and obj.instance_collection is not None
                           and in_collection(obj.instance_collection))
        if not hit:
            continue
        if obj.visible_get(view_layer=view_layer) and not obj.hide_select:
            shown.append(obj)
        else:
            hidden += 1
    return shown, hidden


def _select_only(context, objects, extend):
    """Select these objects, and only these unless extend is set. The
    first one becomes active when the active object is not among them."""
    if not extend:
        for obj in context.view_layer.objects:
            if obj.select_get():
                obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if context.view_layer.objects.active not in objects:
        context.view_layer.objects.active = objects[0]


def _mapping_flags(names, matched, has_pattern, invert, show_unused, used,
                   bit):
    """The filter flags of the Material Database list.

    matched says which names the name filter keeps. Blender inverts every
    flag when Invert is set, so a flag is inverted here as well, and the
    name filter is the only thing that Invert turns around. The eye hides
    an entry that no part of the scene uses, with Invert set or not."""
    flags = []
    for name, match in zip(names, matched):
        keep = (match != invert) if has_pattern else True
        keep = keep and (show_unused or name in used)
        flags.append(bit if keep != invert else 0)
    return flags


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
    return {
        "up_as": prefs.preferred_up_axis,
        "htypes": prefs.preferred_hierarchy,
        "apply_scale": True,
        "deflection_spec": quality_mod.spec_of(bpy.context.scene.stepper),
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

    Only a shape holding two or more bodies is touched. A single-body shape
    keeps its sub-index of None and is byte-for-byte the same import as
    before. A body is what the mesh build calls one (importer._bodies): a
    solid, a shell outside any solid (a surface body), and the faces outside
    any shell together as one more. The option's own description names
    solids, shells and surfaces. Only the solids were taken when a shape had
    any, and a surface body next to them was lost with no warning.

    Identity is ShapeKey, not an OCCT map: per-entity handle lookups are
    broken in OCP 7.9.3.1, which is why curves.py does the same.
    """
    from OCP.TopAbs import TopAbs_SOLID, TopAbs_SHELL, TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    from .ocp_utils import ShapeKey, SameKey

    def inherit_colour(parent, body):
        """A body carries its product's color: the shape-level fallback."""
        pkey, bkey = ShapeKey(parent), ShapeKey(body)
        if pkey in step_reader.face_colors:
            step_reader.face_colors.setdefault(
                bkey, step_reader.face_colors[pkey])
        prio = getattr(step_reader, "face_color_priority", None)
        if prio is not None and pkey in prio:
            prio.setdefault(bkey, prio[pkey])

    def faces_of(shape):
        found = []
        ex = TopExp_Explorer(shape, TopAbs_FACE)
        while ex.More():
            found.append(SameKey(ex.Current()))
            ex.Next()
        return found

    def carry_labels(parent, parts):
        """Give each body the color labels that lie inside it.

        The reader keeps the labels of a part (a color on a body, a face)
        against the part's own shape, and the mesh build looks them up
        under the shape it builds. A body is a shape of its own, so it
        found none and came in all in the body color. A label goes to the
        body that holds every one of its faces. A label on faces of two
        bodies stays with the part. Run again on a cached reader, this adds
        nothing twice."""
        subs = step_reader.sub_shapes.get(ShapeKey(parent)) or []
        if not subs:
            return
        owner = {}
        for k, body in enumerate(parts):
            for key in faces_of(body):
                owner.setdefault(key, k)
        carried = [[] for _ in parts]
        for sub in subs:
            homes = {owner.get(key, -1) for key in faces_of(sub)}
            if len(homes) == 1 and -1 not in homes:
                carried[homes.pop()].append(sub)
        for body, labels in zip(parts, carried):
            if not labels:
                continue
            have = step_reader.sub_shapes.setdefault(ShapeKey(body), [])
            known = {ShapeKey(s) for s in have}
            have.extend(s for s in labels if ShapeKey(s) not in known)

    def bodies(shape, kind, avoid=None):
        found, seen = [], set()
        ex = (TopExp_Explorer(shape, kind) if avoid is None
              else TopExp_Explorer(shape, kind, avoid))
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
            parts = (bodies(shp, TopAbs_SOLID)
                     + bodies(shp, TopAbs_SHELL, TopAbs_SOLID))
            loose = bodies(shp, TopAbs_FACE, TopAbs_SHELL)
            if loose and parts:
                rest = TopoDS_Compound()
                builder = BRep_Builder()
                builder.MakeCompound(rest)
                for face in loose:
                    builder.Add(rest, face)
                parts.append(rest)
        if len(parts) < 2:
            out.append((shp, idx, None))
            continue
        split_shapes += 1
        carry_labels(shp, parts)
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
    uv_closed_seams="SINGLE",
    uv_smart_distortion=UV_SMART_DISTORTION,
    uv_smart_sharp=False,
    uv_smart_split=True,
    uv_unwrap_compound=True,
    box_uv_scale=1.0,
    tris_to_quads=True,
    uv_pack="NONE",
    uv_pack_tiles=4,
    uv_pack_margin=UV_PACK_MARGIN,
    import_curves=False,
    eng_materials=False,
    group_in_collection=False,
    separate_solids=False,
    cursor=None,
    # Older records and scripts. Both now live in uv_mode, and a refresh
    # passes a record back in unchanged.
    uv_merge_tangent=None,
    uv_unwrap_method=None,
    # The scale a refresh passes to keep the size when the scene unit
    # length changed. It is used like custom_scale but is not recorded as
    # one: the user did not set it. When it was recorded, the next refresh
    # took it as the user's own and divided it by the new unit length.
    scale_override=None,
):
    from . import importer

    global _debug_timing
    _debug_timing = _get_addon_prefs().debug_timing

    uv_mode = uv_mod.migrate_settings({
        "uv_mode": uv_mode, "uv_unwrap_method": uv_unwrap_method,
        "uv_merge_tangent": uv_merge_tangent})["uv_mode"]
    _set_uv_options(uv_mode, uv_normalize, uv_closed_seams, box_uv_scale)

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
            signature = _file_signature(filepath)
            step_reader = formats.make_reader(
                filepath, skip_name_prefixes=skip_prefixes)
            _record_parse_calibration(step_reader, time.time() - parse_t0)
            _cache_put(filepath, step_reader, signature)
        except AssertionError as e:
            print(e)
            return False
    else:
        step_reader = cached
        print("Loaded file from cache")
        # A cached reader still holds the problem lists of the last import.
        step_reader.begin_import()

    tree = step_reader.tree
    scale = step_reader.scale

    # Resolve deflection spec now that the file's unit scale is known
    # (physical mode: same real-world deflection regardless of file units).
    tessellation_relative = bool(
        deflection_spec and deflection_spec.get("mode") == "relative")
    if deflection_spec is not None:
        lin_deflection, ang_deflection = import_ui.resolve_deflections(
            deflection_spec, step_reader.scale)
        unit = "(relative factor)" if tessellation_relative else "file units"
        print(f"Deflection resolved: {lin_deflection:.4f} {unit} "
              f"/ {ang_deflection:.3f} rad")

    # The curve sampler has no relative mode, and read a share of 0.005 as
    # 0.005 file units. A relative import samples its curves at the
    # distance of the default quality instead.
    curve_deflection = lin_deflection
    if tessellation_relative:
        curve_deflection = quality_mod.resolve(
            quality_mod.spec(quality_mod.DEFAULT), step_reader.scale)[0]

    if custom_scale is not None:
        scale = custom_scale
    if scale_override is not None:
        scale = scale_override

    # divide by Blender unit length
    scale /= context.scene.unit_settings.scale_length
    print("Current Blender scale set at:", context.scene.unit_settings.scale_length)

    # BoxUV tile size is specified in world units. Meshes are built in file
    # units, so the apply path needs the conversion factor.
    _uv_options["unit_scale"] = scale

    # SurfaceUV mode for this import: 0-1 normalized (default) or scaled to
    # real-world scene units for consistent texel density across parts.
    step_reader.uv_world_scale = None if uv_normalize else scale

    # Everything a refresh needs to repeat this import. It goes on the scene
    # AND on every object the import makes. A refresh reads the scene copy
    # first, but that copy does not always survive: a background import
    # records it on a worker scene which is deleted once its content has
    # been appended. The copy on the objects travels with them.
    #
    # "unit_scale" is the resolved scale in Blender units, and
    # "scene_unit_scale" the scene unit length it was resolved against.
    # Together they let a refresh reproduce the SIZE. A background import
    # needs that: it runs in a factory scene at 1.0, which is not
    # necessarily the scene the objects end up in.
    #
    # "cursor" is where the parts went. A new import puts them at the 3D
    # cursor. A refresh passes the recorded place back, because the cursor
    # has often moved since, and a refresh reads a part it put somewhere
    # else as a part moved in CAD.
    if cursor is None:
        cursor = tuple(bpy.context.scene.cursor.location)
    cursor = [float(c) for c in cursor]
    import_record = {
        "up_as": up_as if isinstance(up_as, str) else up_as[0],
        "htypes": htypes,
        "apply_scale": apply_scale,
        "custom_scale": custom_scale,
        "unit_scale": scale,
        "scene_unit_scale": context.scene.unit_settings.scale_length,
        "lin_deflection": lin_deflection,
        "ang_deflection": ang_deflection,
        "tessellation_relative": tessellation_relative,
        "material_database": material_database,
        "skip_construction": skip_construction,
        "uv_mode": _uv_options["mode"],
        "uv_normalize": _uv_options["normalize"],
        "uv_closed_seams": _uv_options["closed_seams"],
        "uv_smart_distortion": uv_smart_distortion,
        "uv_smart_sharp": uv_smart_sharp,
        "uv_smart_split": uv_smart_split,
        "uv_unwrap_compound": uv_unwrap_compound,
        "box_uv_scale": _uv_options["box_scale"],
        "tris_to_quads": tris_to_quads,
        "uv_pack": uv_pack,
        "uv_pack_tiles": uv_pack_tiles,
        "uv_pack_margin": uv_pack_margin,
        "import_curves": import_curves,
        "eng_materials": eng_materials,
        "group_in_collection": group_in_collection,
        "separate_solids": separate_solids,
        "cursor": cursor,
    }
    import_record_json = json.dumps(import_record)

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
                    # Occurrence becomes a collection-instance empty later.
                    # Just record which prototype it instances.
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
                    # Phase 1 could not tessellate this shape, so try once
                    # more, with the same settings. A shape that fails
                    # again is a part with no geometry. Before, the error
                    # stopped the import halfway, with every object made so
                    # far left unplaced at file units.
                    _shp, part_name, color_override = unique_shapes[shape_name]
                    try:
                        build_mesh(step_reader, obj, shp, lin_deflection,
                                   ang_deflection,
                                   relative=tessellation_relative,
                                   part_name=part_name,
                                   fallback_color=color_override)
                    except Exception as e:
                        print(f"\nWarning: could not build {name}: {e}")
                        obj.data.clear_geometry()

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
                        # prototype. This occurrence becomes an instance empty.
                        proto = obj
                        proto["STEP_tag"] = tag
                        proto["STEP_file"] = filepath
                        proto["STEP_name"] = name
                        proto["STEP_tree_location"] = node_index
                        proto["STEP_applied_scale"] = scale if apply_scale else 0.0
                        proto["STEP_import_settings"] = import_record_json
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
                        shp, curve_deflection, ang_deflection)
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
            obj["STEP_import_settings"] = import_record_json
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

    # Optional packed unwrap (unique meshes only. linked
    # copies share the datablock and get it for free). Meshes are still in
    # file units here, so real-world mode converts through unit_scale.
    # Quads first: the unwrap then has about half the faces to flatten,
    # and its UVs land on the mesh the user keeps.
    if tris_to_quads:
        _tris_to_quads_objects(created_names.values())

    # A pack that rescales islands would undo a real-world scale pass, so
    # that pass only runs when the scale is going to survive.
    # Normalize UVs is the one answer to "may the islands be resized". With
    # it off the pack keeps them at their real-world size instead.
    packing = uv_pack not in (None, "NONE")
    pack_scale = bool(uv_normalize)
    rescales = packing and (pack_scale or uv_pack == "UDIM")
    # Smart lays the charts out as a net and puts the seams on its edges.
    if uv_mode == "SMART":
        _smart_merge_objects(created_names.values(), uv_pack, uv_pack_tiles,
                             uv_smart_distortion, uv_smart_sharp,
                             uv_smart_split, uv_unwrap_compound)
    if _uv_options.get("unwrap"):
        _unwrap_uv_objects(
            created_names.values(),
            world_scale=(None if (_uv_options.get("normalize", True)
                                  or rescales)
                         else _uv_options["unit_scale"]),
            method=uv_mode)

    if packing:
        _pack_uv_objects(created_names.values(), uv_pack, uv_pack_tiles,
                         uv_pack_margin, pack_scale)

    # remove all temporary links
    for tobj in created_objs:
        obj_unlink_all(tobj)

    # build flat collection
    if hierarchy_flat:
        flat_collection = _own_collection(filename + ".flat", filepath, "flat")
        destination().children.link(flat_collection)

        created_collections = {}
        for obj in created_objs:
            global_t = tree.nodes[obj["STEP_tree_location"]].global_transform
            if obj in curve_objs:
                # A curve goes to Cad Curves, so a group made for it here
                # would stay empty.
                set_obj_matrix_world(obj, global_t)
                link_created(obj, flat_collection)
                continue

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
                    # The CAD name, exactly. The collection's own name is a
                    # display label Blender rewrites on collision, and a
                    # product named "42S TC100.2" cannot be told from a
                    # duplicate of "42S TC100" by the label alone.
                    collection_node["STEP_name"] = node.name
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
    # ".components" collection. Every occurrence is an instancing empty
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

    # The CAD color on the objects themselves, so a Solid viewport set to
    # Object color matches the file. After the material database pass,
    # because that pass is what decides the final material.
    set_object_colors(created_objs)
    set_object_colors(instance_prototypes.values())

    print(f"\n--- Phase 3/3: Applying transforms ---")
    transform_to_up(up_as[0], created_objs, scale, apply_scale=apply_scale,
                    cursor=cursor)

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

    # The empties were made at one size for every file. Now that the parts
    # are placed and scaled, each is sized to what hangs under it.
    if hierarchy_empties or hierarchy_instances:
        bpy.context.view_layer.update()
        empties_mod.fit(created_objs, created_objs)

    # Where the import put every object, so a later refresh can tell a part
    # that moved in CAD from one the user moved in Blender.
    refresh_mod.stamp_basis(created_objs)
    refresh_mod.stamp_basis(instance_prototypes.values())

    # What this file was imported with, so a refresh reproduces it rather
    # than falling back to whatever the dialog happens to hold later.
    try:
        refresh_mod.record_import(context.scene if context else None,
                                  filepath, import_record)
    except Exception as exc:
        print("CADder: could not record the import:", exc)

    wm.progress_end()
    elapsed = time.time() - start_time

    # Import summary report
    n_objects = len(created_objs)
    n_unique = len(created_names)
    n_linked = n_objects - n_unique
    print(f"\n{'='*50}")
    print(f"  CADder Import Summary")
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


def _mapping_edited(self, context):
    # A change in the list is not in the database until Save writes it.
    self.id_data.stepper.mat_db_dirty = True


class PG_MaterialMapping(bpy.types.PropertyGroup):
    """A single original-name -> replacement-material mapping entry."""
    original_name: bpy.props.StringProperty(name="Original Name")
    replacement_name: bpy.props.StringProperty(name="Replacement",
                                               update=_mapping_edited)


class PG_Stepper(bpy.types.PropertyGroup):
    """Per-scene properties (import parameters, file paths).

    Persistent settings (build_materials, debug_timing, etc.) live on
    STEP_AddonPreferences and are accessed via _get_addon_prefs().
    """
    # How fine Regenerate and Rebuild from CAD cut the parts: the same five
    # settings as the import dialog, and the same meaning on either route.
    quality_preset: bpy.props.EnumProperty(**import_ui.QUALITY_KW["quality_preset"])
    lin_deflection_len: bpy.props.FloatProperty(**import_ui.QUALITY_KW["lin_deflection_len"])
    ang_deflection_rot: bpy.props.FloatProperty(**import_ui.QUALITY_KW["ang_deflection_rot"])
    tessellation_relative: bpy.props.BoolProperty(**import_ui.QUALITY_KW["tessellation_relative"])
    lin_deflection_rel: bpy.props.FloatProperty(**import_ui.QUALITY_KW["lin_deflection_rel"])

    tris_to_quads: bpy.props.BoolProperty(
        name="Triangles to Quads",
        description="Pair the tessellation triangles back into quads. A "
                    "flat or lightly curved CAD face comes out as long thin "
                    "pairs that go back together cleanly. Nothing is joined "
                    "across a material, a UV island, a seam or a sharp edge",
        default=True)

    fix_ascii_file: bpy.props.StringProperty(
        name="File",
        description="Path to problematic STEP file",
        default="",
        maxlen=1024,
        subtype="FILE_PATH",
    )

    # UV panel: make the map again for parts already in the scene. The
    # defaults match the import dialog, so the panel opens on the layout the
    # parts most likely have.
    uv_mode: bpy.props.EnumProperty(
        items=UV_MODE_ITEMS, name="UV Map",
        description="How to make the UVMap layer of the selected parts",
        default="SURFACE")
    uv_normalize: bpy.props.BoolProperty(
        name="Normalize UVs",
        description="Fit the UVs to the 0-1 square. Turn this off to scale "
                    "them to real world scene units instead",
        default=False)
    uv_closed_seams: bpy.props.EnumProperty(
        items=UV_CLOSED_ITEMS, name="Closed surfaces",
        description="What to do where a cylinder, cone, sphere or torus "
                    "closes on itself. This does not change the shading",
        default="SINGLE")
    uv_smart_distortion: bpy.props.FloatProperty(
        name="Smart Distortion", subtype="PERCENTAGE",
        description="How far Smart can bend a face to join it to an "
                    "island, as an average over the face. Squash counts in "
                    "full. Stretch along the shared edge counts half, "
                    "because an unrolled ring is still a clean strip. Set 0 "
                    "to join only the faces that fit without a bend",
        default=UV_SMART_DISTORTION, min=0.0, max=100.0, precision=0)
    uv_smart_sharp: bpy.props.BoolProperty(
        name="Join Sharp Edges",
        description="After the smooth edges, let Smart join faces across "
                    "sharp edges too. A face joins only if it does not "
                    "overlap the island and the island still fits the UV "
                    "tile. This gives fewer and larger islands",
        default=False)
    uv_smart_split: bpy.props.BoolProperty(
        name="Optimize Island Shape",
        description="Cut an island along a join where the pieces pack "
                    "better. An island shaped like a V, or with a long arm, "
                    "fills little of the rectangle around it. Clear this "
                    "option to keep every island whole",
        default=True)
    uv_unwrap_compound: bpy.props.BoolProperty(
        name="Unwrap Compound Surfaces",
        description="Give Blender's unwrap the faces that no one scale can "
                    "flatten: a sphere, a torus, a blend corner or a spline "
                    "surface. A plane, a cylinder and a cone keep the exact "
                    "chart of the CAD surface",
        default=True)
    box_uv_scale: bpy.props.FloatProperty(
        name="Box UV Size", unit="LENGTH",
        description="World size of one UV tile for the Box Project mode",
        default=1.0, min=0.0001)
    uv_pack: bpy.props.EnumProperty(
        items=UV_PACK_ITEMS, name="Pack UVs",
        description="Arrange the islands of the selected parts after the UV "
                    "map is made",
        default="NONE")
    uv_pack_tiles: bpy.props.IntProperty(
        name="Tile Count",
        description="How many UDIM tiles to share the parts over",
        default=4, min=1, max=100)
    uv_pack_margin: bpy.props.FloatProperty(
        name="Pack Margin",
        description="Space left around each island. The addon scales this "
                    "down as more parts share a tile",
        default=UV_PACK_MARGIN, min=0.0, max=0.25, precision=4)

    # Material database UI state
    mat_db_mappings: bpy.props.CollectionProperty(type=PG_MaterialMapping)
    # The database file the list came from. The list is kept in the scene
    # and the selected database is a preference, so they can differ: a
    # user selects another database, or opens a .blend whose list came
    # from another one. Save then replaced that database with this list.
    mat_db_source: bpy.props.StringProperty(options={"HIDDEN"})
    # The list has changes that Save has not written to the database. The
    # Save button says so, and Load asks before it drops them.
    mat_db_dirty: bpy.props.BoolProperty(options={"HIDDEN"})
    mat_db_active_index: bpy.props.IntProperty(default=0)
    mat_db_show_unused: bpy.props.BoolProperty(
        name="Show Unused Entries",
        description="Show the entries for materials that no part in this "
                    "scene has",
        default=True,
    )
    mat_db_apply_selection_only: bpy.props.BoolProperty(
        name="Selection Only",
        description="Apply material mappings only to selected objects",
        default=False,
    )


class ImportStepCADOperator(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.occ_import_step"
    bl_label = "Import STEP"
    bl_description = "Import a STEP, IGES or BREP CAD file"
    # UNDO: Blender pushes an undo step after an operator only when it has
    # this option. Without it the import joined the next step, and a Ctrl+Z
    # of a later edit took the whole import away with it. A call from a
    # script pushes no step, so refresh and the worker are not changed.
    bl_options = {"PRESET", "UNDO"}

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

    # Legacy deflection, for scripts only. When a caller sets one of these,
    # the import ignores Quality (see make_deflection_spec). A preset writes
    # and sets every property that has no SKIP_PRESET, so without it each
    # applied preset went back to legacy 0.8 and 0.5 in file units.
    lin_deflection: bpy.props.FloatProperty(
        name="Linear Deflection",
        description="Maximum distance between the mesh and the true "
                    "surface, in file units. Smaller values produce more "
                    "polygons",
        default=0.8,
        min=0.002,
        max=2.0,
        options={"HIDDEN", "SKIP_SAVE", "SKIP_PRESET"},
    )

    ang_deflection: bpy.props.FloatProperty(
        name="Angular Deflection",
        description="Maximum angle between adjacent facets, in radians. "
                    "Smaller values produce more polygons",
        default=0.5,
        min=0.002,
        max=2.0,
        options={"HIDDEN", "SKIP_SAVE", "SKIP_PRESET"},
    )

    custom_scale: bpy.props.BoolProperty(
        name="Custom Scale",
        description="Set the unit scale by hand instead of reading it from the "
                    "file",
        default=False,
    )

    apply_scale: bpy.props.BoolProperty(
        name="Apply Scale",
        description="Bake scale into mesh vertices so object scale is (1, 1, 1)",
        default=True,
    )

    material_database: bpy.props.EnumProperty(
        items=_matdb_enum_items,
        name="Material Database",
        description="Replace the STEP materials with materials from a database",
    )

    quality_preset: bpy.props.EnumProperty(**import_ui.QUALITY_KW["quality_preset"])
    lin_deflection_len: bpy.props.FloatProperty(**import_ui.QUALITY_KW["lin_deflection_len"])
    ang_deflection_rot: bpy.props.FloatProperty(**import_ui.QUALITY_KW["ang_deflection_rot"])

    skip_construction: bpy.props.BoolProperty(
        name="Skip Construction Geometry",
        description="Skip parts whose names match the construction-geometry "
                    "filters in the addon preferences (sketches, axes, wires...)",
        default=False,
    )

    uv_mode: bpy.props.EnumProperty(
        items=UV_MODE_ITEMS,
        name="UV Map",
        description="How the addon fills the UVMap layer",
        default="SURFACE",
    )

    uv_normalize: bpy.props.BoolProperty(
        name="Normalize UVs",
        description="Fit the UVs to the 0-1 square. Every island of a part is "
                    "divided by the same number, so the islands keep their "
                    "size against each other. Turn this off "
                    "to scale the UVs to real world scene units instead. The "
                    "islands stay packed and the addon rescales them together. "
                    "One material then shows a texture at the same size on "
                    "every part. This also tells Pack UVs whether it may "
                    "resize the islands. UDIM tiles always resize them, "
                    "because their grid is fixed",
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

    uv_smart_distortion: bpy.props.FloatProperty(
        name="Smart Distortion",
        subtype="PERCENTAGE",
        description="How far Smart can bend a face to join it to an "
                    "island, as an average over the face. Squash counts in "
                    "full. Stretch along the shared edge counts half, "
                    "because an unrolled ring is still a clean strip. Set 0 "
                    "to join only the faces that fit without a bend",
        default=UV_SMART_DISTORTION,
        min=0.0,
        max=100.0,
        precision=0,
    )

    uv_smart_sharp: bpy.props.BoolProperty(
        name="Join Sharp Edges",
        description="After the smooth edges, let Smart join faces across "
                    "sharp edges too. A face joins only if it does not "
                    "overlap the island and the island still fits the UV "
                    "tile. This gives fewer and larger islands",
        default=False,
    )

    uv_smart_split: bpy.props.BoolProperty(
        name="Optimize Island Shape",
        description="Cut an island along a join where the pieces pack "
                    "better. An island shaped like a V, or with a long arm, "
                    "fills little of the rectangle around it. Clear this "
                    "option to keep every island whole",
        default=True,
    )

    uv_unwrap_compound: bpy.props.BoolProperty(
        name="Unwrap Compound Surfaces",
        description="Give Blender's unwrap the faces that no one scale can "
                    "flatten: a sphere, a torus, a blend corner or a spline "
                    "surface. A plane, a cylinder and a cone keep the exact "
                    "chart of the CAD surface",
        default=True,
    )

    uv_closed_seams: bpy.props.EnumProperty(
        items=UV_CLOSED_ITEMS,
        name="Closed surfaces",
        description="What to do where a cylinder, cone, sphere or torus "
                    "closes on itself. CAD data has no seam there, so an "
                    "unwrap has nowhere to cut. This does not change the "
                    "shading",
        default="SINGLE",
    )

    uv_pack: bpy.props.EnumProperty(
        items=UV_PACK_ITEMS,
        name="Pack UVs",
        description="Pack the UV islands after the UV map is made. Packing "
                    "scales the islands to fill the tile, so it replaces the "
                    "real world UV scale",
        default="NONE",
    )

    uv_pack_tiles: bpy.props.IntProperty(
        name="UDIM Tiles",
        description="How many UDIM tiles to spread the parts over. The parts "
                    "are shared out by surface area, so each tile carries a "
                    "similar amount",
        default=4,
        min=1,
        max=100,
    )

    uv_pack_margin: bpy.props.FloatProperty(
        name="Pack Margin",
        description="Space left around each UV island, as a fraction of the "
                    "tile. Blender puts this around every island, and a CAD "
                    "part has about one island per face, so the addon divides "
                    "it down when many parts share a tile. Raise it if a bake "
                    "bleeds between islands",
        default=0.005,
        min=0.0,
        max=0.25,
        precision=4,
    )

    tris_to_quads: bpy.props.BoolProperty(
        name="Tris to Quads",
        description="Pair the tessellation triangles back into quads after "
                    "the import. A CAD tessellation cuts every flat face into "
                    "thin triangle pairs, and this puts them back. It never "
                    "joins across a material, a UV island, a seam or a sharp "
                    "edge. It does not retopologize: the vertices do not move "
                    "and the shape does not change",
        default=True,
    )

    box_uv_scale: bpy.props.FloatProperty(
        name="Box UV Size",
        description="World size of one UV tile for the Box Project mode",
        unit="LENGTH",
        default=1.0,
        min=0.0001,
    )

    import_curves: bpy.props.BoolProperty(
        name="Import Curves",
        description="Import free edges (sketches, construction wires) as "
                    "curve objects, in a collection named \"Cad Curves\"",
        default=False,
    )

    separate_solids: bpy.props.BoolProperty(
        name="Separate Solids",
        description="Give every body of a multibody part its own object. Use "
                    "this for a file that holds several solids, shells or "
                    "surfaces with no assembly structure to tell them apart",
        default=False,
    )

    group_in_collection: bpy.props.BoolProperty(
        name="Group in a Collection",
        description="Put everything this file creates under one collection "
                    "named after the file. A second import then does not "
                    "interleave with the first. The whole assembly moves, hides"
                    " and deletes as one unit",
        default=False,
    )

    tessellation_relative: bpy.props.BoolProperty(**import_ui.QUALITY_KW["tessellation_relative"])
    lin_deflection_rel: bpy.props.FloatProperty(**import_ui.QUALITY_KW["lin_deflection_rel"])

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
            "skip_construction": self.skip_construction,
            "uv_mode": self.uv_mode,
            "uv_normalize": self.uv_normalize,
            "uv_closed_seams": self.uv_closed_seams,
            "uv_smart_distortion": self.uv_smart_distortion,
            "uv_smart_sharp": self.uv_smart_sharp,
            "uv_smart_split": self.uv_smart_split,
            "uv_unwrap_compound": self.uv_unwrap_compound,
            "box_uv_scale": self.box_uv_scale,
            "tris_to_quads": self.tris_to_quads,
            "uv_pack": self.uv_pack,
            "uv_pack_tiles": self.uv_pack_tiles,
            "uv_pack_margin": self.uv_pack_margin,
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
                uv_closed_seams=self.uv_closed_seams,
                uv_smart_distortion=self.uv_smart_distortion,
                uv_smart_sharp=self.uv_smart_sharp,
                uv_smart_split=self.uv_smart_split,
                uv_unwrap_compound=self.uv_unwrap_compound,
                box_uv_scale=self.box_uv_scale,
                tris_to_quads=self.tris_to_quads,
                uv_pack=self.uv_pack,
                uv_pack_tiles=self.uv_pack_tiles,
                uv_pack_margin=self.uv_pack_margin,
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
    bl_label = "Clear STEP Cache"
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
    bl_label = "Fix STEP Text Encoding"
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
    bl_label = "Print STEP Debug Information"
    bl_description = ("Print what the reader found in this STEP file to "
                      "the system console")

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
    bl_label = "Clear This File from Cache"
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


def _rebuild_body(step_reader, shp, node_index, obj):
    """The part of `shp` that `obj` was made from.

    Separate Solids gives each body of a shape its own object, and every
    body keeps the tag and the node of the whole shape. A rebuild from the
    whole shape merged every body into each one. The import names body k
    "<part>.body<k + 1>", so the name says which body to take. None when
    that body is not in the shape.
    """
    try:
        record = json.loads(obj.get("STEP_import_settings") or "{}")
    except (TypeError, ValueError):
        record = {}
    if not record.get("separate_solids"):
        return shp
    bodies = _split_solids([(shp, node_index, None)], step_reader)
    if len(bodies) < 2:
        return shp
    _part, sep, number = str(obj.get("STEP_name", "")).rpartition(".body")
    if not sep or not number.isdigit():
        return None
    k = int(number) - 1
    return bodies[k][0] if 0 <= k < len(bodies) else None


class STEP_OT_RebuildSelected(bpy.types.Operator):
    bl_idname = "object.occ_rebuild_selected"
    bl_label = "Rebuild Selected Objects"
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
        # Only mesh objects that carry STEP metadata can be rebuilt. a
        # box-select of an imported assembly also contains hierarchy empties
        my_selection = [o for o in context.selected_objects
                        if o.data is not None
                        and "STEP_tag" in o and "STEP_file" in o]
        if not my_selection:
            self.report({"WARNING"}, "No rebuildable STEP objects selected")
            return {"CANCELLED"}
        # A part whose geometry is locked keeps its mesh. The mesh is
        # rebuilt in place, so a mesh that a locked part uses stays as it
        # is for every part that uses it.
        my_selection, kept = geometry_lock.split(my_selection)
        if not my_selection:
            self.report({"WARNING"},
                        "The geometry of every selected part is locked")
            return {"CANCELLED"}

        wanted = quality_mod.spec_of(context.scene.stepper)

        # select all objs with the same meshes
        for obj in my_selection:
            for other_obj in context.scene.objects:
                if obj.data == other_obj.data:
                    other_obj.select_set(True)

        # go through all selected and rebuild the meshes. A new mesh gets
        # the materials of the file, and a locked part gets its own back.
        locks = material_lock.take()
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

            # Once per mesh: linked copies share one. A separated body and
            # a color variant carry the tag of their shape, but each has a
            # mesh of its own.
            if obj.data in rebuilt_meshes:
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

            # Every occurrence of a shape has its tag. Take the node the
            # object was made from, because a color variant differs from the
            # other occurrences only in its node.
            source = None
            for shp, node_index in tree.get_shapes():
                _, _, tag, name, _, _, _ = tree.nodes[node_index].get_values()
                if tag == sel_tag and (
                        source is None
                        or node_index == obj.get("STEP_tree_location")):
                    source = (shp, node_index)
            shp = None
            if source is not None:
                shp, node_index = source
                shp = _rebuild_body(step_reader, shp, node_index, obj)
            if shp is not None:
                rebuilt_meshes.add(obj.data)
                print("Rebuilding:", sel_tag, obj.data.name)
                # Reset pre-tessellation flag so shapes get re-tessellated
                # with the new deflection values
                step_reader._pre_tessellated = False
                lin_def, ang_def, relative = quality_mod.resolve(
                    wanted, step_reader.scale)
                build_mesh(step_reader, obj, shp, lin_def, ang_def,
                           relative=relative,
                           part_name=obj.get("STEP_name", ""),
                           fallback_color=(
                               tree.nodes[node_index].color_override))
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

            wm.progress_update(progress_count)

        wm.progress_end()

        for obj in context.selected_objects:
            obj.display_type = "TEXTURED"

        material_lock.restore(locks)
        # Re-apply active material database if one is set
        db_path = _get_active_matdb_path()
        if db_path:
            mappings = _ensure_matdb_materials(db_path)
            _apply_matdb_to_objects(my_selection, mappings)

        if kept:
            self.report({"INFO"},
                        f"Rebuilt {len(rebuilt_meshes)} mesh(es), "
                        f"{len(kept)} part(s) kept their locked geometry")
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
        _populate_ui_mappings(context.scene.stepper, mappings, filepath)

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
        _populate_ui_mappings(context.scene.stepper, mappings, dst)

        self.report({'INFO'}, f"Duplicated to '{name}'")
        return {'FINISHED'}


class STEP_OT_MatDBRefresh(bpy.types.Operator):
    """Show the entries of the selected database, and add its materials to this file to use for other entries"""
    bl_idname = "stepper.mat_db_refresh"
    bl_label = "Load Material Database"

    @classmethod
    def poll(cls, context):
        return bool(_get_active_matdb_path())

    def invoke(self, context, event):
        stepper = context.scene.stepper
        if stepper.mat_db_dirty and len(stepper.mat_db_mappings) > 0:
            return context.window_manager.invoke_confirm(
                self, event, title="Load Material Database",
                message="The list has changes that are not saved. Load "
                        "the database and discard them?",
                confirm_text="Load", icon='WARNING')
        return self.execute(context)

    def execute(self, context):
        db_path = _get_active_matdb_path()

        # The materials first, so the list can show them. They stay in the
        # file, to pick for an entry of another material.
        added = _append_matdb_materials(db_path, keep=True)

        mappings = _read_matdb_mappings(db_path)
        if not mappings:
            self.report({'WARNING'}, "No mappings found in database")
            return {'CANCELLED'}

        _populate_ui_mappings(context.scene.stepper, mappings, db_path)
        message = f"Loaded {len(mappings)} mapping(s)"
        if added:
            message += f" and added {added} material(s) to this file"
        self.report({'INFO'}, message)
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
            stepper.mat_db_dirty = True
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
        stepper = context.scene.stepper
        filepath = _get_active_matdb_path()
        if not filepath or len(stepper.mat_db_mappings) == 0:
            return False
        # Save replaces the whole file, so a list from another database
        # would destroy this one.
        if not _same_file(stepper.mat_db_source, filepath):
            cls.poll_message_set("The list is not from the selected "
                                 "database. Load the selected database first")
            return False
        return True

    def execute(self, context):
        stepper = context.scene.stepper
        filepath = _get_active_matdb_path()

        mappings = {}
        for item in stepper.mat_db_mappings:
            mappings[item.original_name] = item.replacement_name

        _write_material_database(filepath, mappings)
        stepper.mat_db_dirty = False
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
        # One misclick on the trash would delete the whole library, and
        # undo cannot bring a file back. So the click only asks, and names
        # the file.
        name = _get_addon_prefs().active_matdb
        return context.window_manager.invoke_confirm(
            self, event, title="Delete Material Database",
            message="Undo cannot restore a deleted database. Delete "
                    "%s.blend from the database folder?" % name,
            confirm_text="Delete", icon='WARNING')

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
        context.scene.stepper.mat_db_source = ""
        context.scene.stepper.mat_db_dirty = False
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

        # invoke() prepares _mappings. A direct execute call (redo panel,
        # scripts) must load them itself
        mappings = getattr(self, "_mappings", None)
        if mappings is None:
            mappings = _ensure_matdb_materials(_get_active_matdb_path())
        if not mappings:
            self.report({'WARNING'}, "No mappings found in database")
            return {'CANCELLED'}

        if stepper.mat_db_apply_selection_only:
            # A selected collection instance has its materials on the
            # prototypes inside the collection it instances.
            objects = [holder for obj in context.selected_objects
                       for holder in material_lock.holders(obj)]
        else:
            objects = [obj for obj in bpy.data.objects
                       if obj.get("STEP_file") is not None
                       or obj.get("SWMESH_file") is not None]

        if not objects:
            self.report({'WARNING'}, "No CAD objects found")
            return {'CANCELLED'}

        locked = []
        replaced = _apply_matdb_to_objects(objects, mappings,
                                           locked_out=locked)
        _cleanup_unused_step_materials(known_names=set(mappings))
        message = (f"Replaced {replaced} material(s) across "
                   f"{len(objects)} object(s)")
        if locked:
            message += (f". {len(locked)} locked part(s) kept their "
                        "materials")
        self.report({'INFO'}, message)
        return {'FINISHED'}


class STEP_OT_MatDBSelectEntry(bpy.types.Operator):
    """Select the parts that have this material"""
    bl_idname = "stepper.mat_db_select_entry"
    bl_label = "Select Parts"
    bl_options = {'REGISTER', 'UNDO'}

    original_name: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    extend: bpy.props.BoolProperty(
        name="Extend",
        description="Add the parts to the selection",
        default=False,
        options={'SKIP_SAVE'},
    )

    @classmethod
    def description(cls, context, properties):
        shown, hidden = _parts_with(context.view_layer,
                                    properties.original_name)
        text = "Select the %d part(s) that have %s" % (
            len(shown), properties.original_name)
        if hidden:
            text += ". %d hidden part(s) stay as they are" % hidden
        return text + ". Shift-click to add them to the selection"

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            cls.poll_message_set("Leave edit mode first")
            return False
        return True

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        shown, hidden = _parts_with(context.view_layer, self.original_name)
        if not shown:
            self.report({'WARNING'}, "The parts with this material are hidden"
                        if hidden else "No part in this scene has this material")
            return {'CANCELLED'}
        _select_only(context, shown, self.extend)
        message = f"Selected {len(shown)} part(s)"
        if hidden:
            message += f". {hidden} hidden part(s) stay as they are"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class STEP_OT_MatDBRemoveEntry(bpy.types.Operator):
    """Remove this entry from the list. Save removes it from the database"""
    bl_idname = "stepper.mat_db_remove_entry"
    bl_label = "Remove Entry"
    bl_options = {'REGISTER', 'UNDO'}

    original_name: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        stepper = context.scene.stepper
        items = stepper.mat_db_mappings
        for index, item in enumerate(items):
            if item.original_name != self.original_name:
                continue
            items.remove(index)
            stepper.mat_db_active_index = max(
                0, min(stepper.mat_db_active_index, len(items) - 1))
            stepper.mat_db_dirty = True
            self.report({'INFO'}, f"Removed the entry for "
                        f"{self.original_name}. Save to remove it from the "
                        "database")
            return {'FINISHED'}
        return {'CANCELLED'}


class STEP_OT_MaterialLock(bpy.types.Operator):
    """Keep the materials of the selected parts. The material database, a refresh from the CAD application and Regenerate leave them as they are"""
    bl_idname = "stepper.material_lock"
    bl_label = "Lock Materials"
    bl_options = {'REGISTER', 'UNDO'}

    lock: bpy.props.BoolProperty(
        name="Lock",
        description="Lock the materials. Clear to unlock them",
        default=True,
    )

    @classmethod
    def description(cls, context, properties):
        if properties.lock:
            return cls.__doc__
        return ("Let the material database, a refresh from the CAD "
                "application and Regenerate change the materials of the "
                "selected parts again")

    @classmethod
    def poll(cls, context):
        if not any(material_lock.has_materials(o)
                   for o in context.selected_objects):
            cls.poll_message_set("Select one or more parts first")
            return False
        return True

    def execute(self, context):
        targets = material_lock.parts(context.selected_objects)
        for obj in targets:
            material_lock.set_locked(obj, self.lock)
        _usage_stale()
        verb = "Locked" if self.lock else "Unlocked"
        self.report({'INFO'},
                    f"{verb} the materials of {len(targets)} part(s)")
        return {'FINISHED'}


class STEP_OT_MaterialLockSelect(bpy.types.Operator):
    """Select the parts whose materials are locked. Shift-click to add them to the selection"""
    bl_idname = "stepper.material_lock_select"
    bl_label = "Select Locked Parts"
    bl_options = {'REGISTER', 'UNDO'}

    extend: bpy.props.BoolProperty(
        name="Extend",
        description="Add the parts to the selection",
        default=False,
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            cls.poll_message_set("Leave edit mode first")
            return False
        if _scene_usage(context.scene)[1] == 0:
            cls.poll_message_set("No part in this scene is locked")
            return False
        return True

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        view_layer = context.view_layer
        shown, hidden = [], 0
        for obj in view_layer.objects:
            if not material_lock.is_locked(obj):
                continue
            if obj.visible_get(view_layer=view_layer) and not obj.hide_select:
                shown.append(obj)
            else:
                hidden += 1
        if not shown:
            self.report({'WARNING'}, "The locked parts are hidden"
                        if hidden else "No part in this scene is locked")
            return {'CANCELLED'}
        _select_only(context, shown, self.extend)
        message = f"Selected {len(shown)} locked part(s)"
        if hidden:
            message += f". {hidden} hidden part(s) stay as they are"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class STEP_OT_GeometryLock(bpy.types.Operator):
    """Keep the geometry of the parts in scope. Rebuild from CAD, Rebuild from STEP and a refresh leave their meshes as they are. The parts still move to new poses"""
    bl_idname = "stepper.geometry_lock"
    bl_label = "Lock Geometry"
    bl_options = {'REGISTER', 'UNDO'}

    lock: bpy.props.BoolProperty(
        name="Lock",
        description="Lock the geometry. Clear to unlock it",
        default=True,
    )

    @classmethod
    def description(cls, context, properties):
        if properties.is_property_set("lock"):
            lock = properties.lock
        else:
            lock = not _every_geometry_locked(_lock_scope(context))
        if lock:
            return cls.__doc__
        return ("Let Rebuild from CAD, Rebuild from STEP and a refresh "
                "replace the geometry of the parts in scope again")

    @classmethod
    def poll(cls, context):
        # This runs on every redraw of the panel, so it looks at a limited
        # number of objects.
        if not _lock_state(context)[0]:
            cls.poll_message_set("Select parts that came from a STEP file "
                                 "or over the live link")
            return False
        return True

    def execute(self, context):
        targets = _lock_scope(context)
        if not targets:
            self.report({'WARNING'}, "Select parts that came from a STEP "
                        "file or over the live link")
            return {'CANCELLED'}
        # The button sets no value: it locks the parts, or unlocks them
        # when every one is locked already. That is decided here, from the
        # whole scope. The panel looks at a part of a large scope only.
        # The value goes into the operator, so Adjust Last Operation shows
        # it and a redo does the same.
        if not self.properties.is_property_set("lock"):
            self.lock = not _every_geometry_locked(targets)
        for obj in targets:
            geometry_lock.set_locked(obj, self.lock)
        _usage_stale()
        verb = "Locked" if self.lock else "Unlocked"
        self.report({'INFO'},
                    f"{verb} the geometry of {len(targets)} part(s)")
        return {'FINISHED'}


class STEP_OT_GeometryLockSelect(bpy.types.Operator):
    """Select the parts whose geometry is locked. Shift-click to add them to the selection"""
    bl_idname = "stepper.geometry_lock_select"
    bl_label = "Select Parts with Locked Geometry"
    bl_options = {'REGISTER', 'UNDO'}

    extend: bpy.props.BoolProperty(
        name="Extend",
        description="Add the parts to the selection",
        default=False,
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            cls.poll_message_set("Leave edit mode first")
            return False
        if _geometry_locked(context.scene) == 0:
            cls.poll_message_set("No part in this scene has locked geometry")
            return False
        return True

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        view_layer = context.view_layer
        shown, hidden = [], 0
        for obj in view_layer.objects:
            if not geometry_lock.is_locked(obj):
                continue
            if obj.visible_get(view_layer=view_layer) and not obj.hide_select:
                shown.append(obj)
            else:
                hidden += 1
        if not shown:
            self.report({'WARNING'}, "The parts with locked geometry are hidden"
                        if hidden else "No part in this scene has locked geometry")
            return {'CANCELLED'}
        _select_only(context, shown, self.extend)
        message = f"Selected {len(shown)} part(s) with locked geometry"
        if hidden:
            message += f". {hidden} hidden part(s) stay as they are"
        self.report({'INFO'}, message)
        return {'FINISHED'}


def _populate_ui_mappings(stepper, mappings, source):
    """Fill the UI CollectionProperty from a mappings dict, and record the
    database file it came from. Save writes only to that file."""
    stepper.mat_db_mappings.clear()
    for orig, repl in sorted(mappings.items()):
        item = stepper.mat_db_mappings.add()
        item.original_name = orig
        item.replacement_name = repl
    stepper.mat_db_source = source
    # Filling the list set the flag. The list is the database now.
    stepper.mat_db_dirty = False


def _same_file(a, b):
    """True when two paths name the same file."""
    if not a or not b:
        return False
    return (os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


# ---------------------------------------------------------------------------
# Material Database UI
# ---------------------------------------------------------------------------

class STEP_UL_MaterialMappings(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_property, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            used = _scene_usage(context.scene)[0]
            row = layout.row(align=True)
            split = row.split(factor=0.45, align=True)
            # The preview of the material the entry puts on the parts.
            mat = bpy.data.materials.get(item.replacement_name)
            text = item.original_name + "  \u2192"
            if mat is not None:
                split.label(text=text, icon_value=layout.icon(mat))
            else:
                split.label(text=text, icon='MATERIAL')
            split.prop_search(item, "replacement_name", bpy.data, "materials", text="")
            sub = row.row(align=True)
            sub.enabled = item.original_name in used
            op = sub.operator("stepper.mat_db_select_entry", text="",
                              icon='RESTRICT_SELECT_OFF', emboss=False)
            op.original_name = item.original_name
            op = row.operator("stepper.mat_db_remove_entry", text="",
                              icon='TRASH', emboss=False)
            op.original_name = item.original_name
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.original_name)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        names = [item.original_name for item in items]
        helper = bpy.types.UI_UL_list
        pattern = self.filter_name
        matched = [True] * len(names)
        if pattern:
            bit = self.bitflag_filter_item
            matched = [bool(f & bit) for f in helper.filter_items_by_name(
                pattern, bit, items, "original_name")]
        flags = _mapping_flags(names, matched, bool(pattern),
                               self.use_filter_invert, data.mat_db_show_unused,
                               _scene_usage(context.scene)[0],
                               self.bitflag_filter_item)
        order = []
        if self.use_filter_sort_alpha:
            order = helper.sort_items_by_name(items, "original_name")
        return flags, order


class STEP_PT_MaterialDB(bpy.types.Panel):
    bl_label = "Material Database"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CADder"
    bl_order = 1005

    def draw(self, context):
        layout = self.layout
        stepper = context.scene.stepper
        prefs = _get_addon_prefs()

        # The database picker, with delete beside it: a well known icon
        # on a destructive action, so the icon carries it.
        row = layout.row(align=True)
        row.prop(prefs, "active_matdb", text="")
        sub = row.row(align=True)
        sub.enabled = prefs.active_matdb != "NONE"
        sub.operator("stepper.mat_db_delete", text="", icon='TRASH')

        row = layout.row(align=True)
        row.operator("stepper.mat_db_create", text="New", icon='ADD')
        sub = row.row(align=True)
        sub.enabled = prefs.active_matdb != "NONE"
        sub.operator("stepper.mat_db_duplicate", text="Duplicate")
        row.operator("stepper.mat_db_refresh", text="Load")

        mappings = stepper.mat_db_mappings
        if len(mappings) > 0:
            # The list stays in the scene when another database is
            # selected. Save refuses then, and this says why.
            source = stepper.mat_db_source
            if source and not _same_file(source, _get_active_matdb_path()):
                layout.label(text="The list is from %s" % os.path.splitext(
                    os.path.basename(source))[0], icon='INFO')
            layout.separator()
            # How many entries the parts of this scene use, and the eye
            # that hides the others.
            used = _scene_usage(context.scene)[0]
            row = layout.row()
            row.label(text="In Scene: %d of %d" % (
                sum(1 for item in mappings if item.original_name in used),
                len(mappings)))
            row.prop(stepper, "mat_db_show_unused", text="", emboss=False,
                     icon='HIDE_OFF' if stepper.mat_db_show_unused
                     else 'HIDE_ON')
            layout.template_list(
                "STEP_UL_MaterialMappings", "",
                stepper, "mat_db_mappings",
                stepper, "mat_db_active_index",
                rows=5,
            )
            row = layout.row(align=True)
            row.operator("stepper.mat_db_update", text="Update",
                         icon='FILE_REFRESH')
            # An asterisk marks changes Save has not written, as Blender
            # marks unsaved preferences.
            row.operator("stepper.mat_db_save",
                         text="Save *" if stepper.mat_db_dirty else "Save")

            row = layout.row(align=True)
            row.operator("stepper.mat_db_apply", text="Apply",
                         icon='CHECKMARK')
            row.prop(stepper, "mat_db_apply_selection_only")

        # The lock is for the live link as much as for the database, so it
        # is here with or without a list. The button names what it does
        # to the selection: it unlocks when every selected part is locked.
        layout.separator()
        parts = material_lock.parts(context.selected_objects)
        every = bool(parts) and all(material_lock.is_locked(o) for o in parts)
        row = layout.row(align=True)
        op = row.operator("stepper.material_lock",
                          text="Unlock Materials" if every else "Lock Materials",
                          icon='LOCKED' if every else 'UNLOCKED',
                          depress=every)
        op.lock = not every
        row.operator("stepper.material_lock_select", text="",
                     icon='RESTRICT_SELECT_OFF')


class STEP_PT_STEPper_Info(bpy.types.Panel):
    """Version line at the top of the tab, with the update notice.

    The version and the tip jar are drawn in the panel header, so the line
    is one row high and the body stays empty until there is an update.

    Do not put HIDE_HEADER on this panel. Blender registers a headerless
    panel in front of every panel that has a header, whatever bl_order says,
    and the tab of a category sits where its first panel sits. One headerless
    panel here therefore pulls the whole CADder tab above Item, Tool
    and View.
    """
    bl_label = ""
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CADder"
    bl_order = 1000

    def draw_header(self, context):
        row = self.layout.row(align=True)
        row.label(text="CADder v%s" % updater_mod.version_string(),
                  icon="TOOL_SETTINGS")
        # The tip jar: a heart, deliberately unlabelled so the version line
        # stays readable at narrow sidebar widths.
        row.operator("wm.url_open", text="",
                     icon="FUND").url = updater_mod.KOFI_URL

    def draw(self, context):
        # What the link to the CAD application is doing. It is here, under
        # the name of the addon, because it is true of the whole tab and
        # not of one panel in it, and because a second CAD application
        # would put a line of its own beside this one.
        prefs = _get_addon_prefs()
        if bridge_mod is not None and bridge_mod.wanted(prefs):
            for text, icon in bridge_mod.status():
                self.layout.label(text=text, icon=icon)

        update = updater_mod.available_update()
        if not update:
            return
        box = self.layout.box().column(align=True)
        box.label(text="Version %s is available" % update["version"],
                  icon="INFO")
        download = box.operator(
            "wm.url_open", icon="IMPORT",
            text="Download %s" % update["version"])
        download.url = update["url"]
        box.label(text="Install the zip as usual to update.")


# What a part was read from, by the name of the file it came from. The
# button that reads it again says which, because a person reaching for it
# is thinking of the file and not of the addon.
_SOURCE_NAMES = {".iges": "IGES", ".igs": "IGES",
                 ".brep": "BREP", ".brp": "BREP"}

# How many objects the panel looks at to find out which routes the scope
# holds. It stops as soon as it has seen both, and the answer only decides
# which controls to draw, so a huge scene must not cost a walk of every
# object on every redraw.
_ROUTE_SCAN = 500


def _routes(context):
    """Which routes the parts in scope came in by: (live, step name).

    live is True when a part came over the live link. The step name is the
    format of the file the STEP parts were read from, or None when there
    are none. The scope is the selection, and the active collection when
    nothing is selected, which is the rule every button here follows.
    """
    live, step = False, None
    found = context.selected_objects
    if not found:
        holder = context.collection
        found = holder.all_objects if holder is not None else ()
    for i, obj in enumerate(found):
        if i >= _ROUTE_SCAN:
            break
        if step is None and tools_mod.from_step(obj):
            ext = ntpath.splitext(str(obj.get("STEP_file") or ""))[1].lower()
            step = _SOURCE_NAMES.get(ext, "STEP")
        elif not live and tools_mod.from_cad_link(obj):
            live = True
        if live and step:
            break
    return live, step


def _lock_scope(context):
    """The parts that Lock Geometry covers. The rule is the one of the
    rebuild buttons beside it: the parts that are selected, or the
    collection that is active in the outliner when no part is selected."""
    return tools_mod.scope_objects(context, geometry_lock.is_part)


def _every_geometry_locked(parts):
    """True when there are parts and the geometry of each one is locked."""
    return bool(parts) and all(geometry_lock.is_locked(o) for o in parts)


def _lock_sources(context):
    """Where the scope of Lock Geometry comes from, in order: the
    selection, then the active collection."""
    yield context.selected_objects
    holder = context.collection
    if holder is not None:
        yield holder.all_objects


def _lock_state(context):
    """(a part is in the scope of Lock Geometry, the geometry of every
    part in it is locked), for the panel and the poll.

    Both run on every redraw, so this looks at _ROUTE_SCAN objects at
    most, and it stops when it knows both answers. In a larger scope the
    answer is that of the parts it looked at. The operator decides from
    the whole scope when it runs."""
    for source in _lock_sources(context):
        found = False
        for i, obj in enumerate(source):
            if i >= _ROUTE_SCAN:
                break
            if not geometry_lock.is_part(obj):
                continue
            if not geometry_lock.is_locked(obj):
                return True, False
            found = True
        if found:
            return True, True
    return False, False


class CADLINK_PT_quality(bpy.types.Panel):
    """How fine the mesh of a part is, whichever way the part came in.

    A part from a STEP file and a part from the live link are tessellated
    by different programs, but the question is the same one and the answer
    is asked for in the same place. The panel shows the controls of the
    route the parts in scope came in by, and the button says which file it
    is going to read again.
    """

    bl_label = "Mesh Quality"
    bl_idname = "CADLINK_PT_quality"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CADder"
    bl_order = 1001

    def draw(self, context):
        layout = self.layout
        prg = context.scene.stepper
        live, step = _routes(context)
        # There is no link on macOS and Linux (bridge.SUPPORTED), so the
        # panel offers only the STEP route there.
        link =(getattr(context.scene, "cad_link", None) is not None
                and getattr(bridge_mod, "SUPPORTED", False))
        if not link:
            live = False
        # With nothing in scope there is still a panel to draw. It shows the
        # live link when there is one, because that is the route that is set
        # up, and the STEP controls otherwise.
        if not live and step is None:
            live = link
            step = None if live else "STEP"

        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        # The same settings whichever route the parts came from: a name
        # cuts a part from SolidWorks and a part from a file the same way.
        import_ui.draw_quality(prg, col)
        col.prop(prg, "tris_to_quads")

        tools_mod.scope_hint(layout, context)
        buttons = []
        if live:
            buttons.append(("cadlink.update_from_cad", "Rebuild from CAD"))
        if step:
            buttons.append(("stepper.regenerate", "Rebuild from %s" % step))
        for n, (idname, text) in enumerate(buttons):
            row = layout.row(align=True)
            row.operator(idname, text=text, icon="FILE_REFRESH")
            if n == 0:
                # Lock Geometry once, beside the first rebuild button: it
                # says which parts a rebuild leaves as they are. It shows
                # whether the parts in scope are locked, and a click locks
                # or unlocks them.
                every = _lock_state(context)[1]
                row.operator("stepper.geometry_lock", text="",
                             icon="LOCKED" if every else "UNLOCKED",
                             depress=every)
                row.operator("stepper.geometry_lock_select", text="",
                             icon="RESTRICT_SELECT_OFF")
        layout.operator("stepper.mesh_cleanup", text="Clean Up Meshes")


class STEP_PT_STEPper(bpy.types.Panel):
    """What the tree of a STEP import holds, and what it does not need.

    A part from the live link is not in it: Prune Hierarchy reads the
    STEP_uuid the STEP importer writes, and the empties it takes out are
    the ones a STEP tree carries. So the panel says STEP, like the two
    below it.
    """

    bl_label = "STEP - Hierarchy"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CADder"
    bl_order = 1006

    def draw(self, context):
        row = self.layout.row(align=True)
        row.operator("stepper.prune_hierarchy", text="Prune Hierarchy",
                     icon='X')
        row.operator("stepper.prune_restore", text="Restore")


class STEP_PT_STEPper_Reload(bpy.types.Panel):
    bl_label = "STEP - File"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CADder"
    bl_order = 1007
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("stepper.batch_import_folder",
                     text="Batch Import Folder", icon='FILE_FOLDER')
        col.operator("stepper.analyze_file", text="Analyze STEP File")
        col = layout.column(align=True)
        col.operator(STEP_OT_ReloadSTEP.bl_idname, text="Reload STEP File",
                     icon='FILE_REFRESH')
        col.operator(STEP_OT_ClearFileCache.bl_idname,
                     text="Clear This File from Cache", icon='TRASH')
        col.operator(STEP_OT_ClearCache.bl_idname, text="Clear All Cache",
                     icon='TRASH')
        row = layout.row()
        row.label(text=f"Cached files: {len(global_file_cache)}/{MAX_FILE_CACHE}")

        layout.separator()
        refresh_mod.draw_imported_files(layout, context)


class STEP_PT_STEPper_UV(bpy.types.Panel):
    """UV maps for every part, whichever way it came in.

    The modes that need the CAD data get it from wherever the part came
    from: the STEP file on disk, or the CAD application holding the live
    model. So this is not a STEP panel any more, and is not named one.
    """

    bl_label = "UV"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CADder"
    bl_order = 1004
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        prg = context.scene.stepper
        layout = self.layout
        col = layout.column()
        import_ui.draw_uv_mode(prg, col)
        col.prop(prg, "uv_pack")
        sub = col.row()
        sub.active = prg.uv_pack == "UDIM"
        sub.prop(prg, "uv_pack_tiles")
        sub = col.row()
        sub.active = prg.uv_pack != "NONE"
        sub.prop(prg, "uv_pack_margin")
        tools_mod.scope_hint(layout, context)
        layout.operator("stepper.reapply_uv", icon="UV")


class STEP_PT_STEPper_Debug(bpy.types.Panel):
    bl_label = "STEP - Debug"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CADder"
    bl_order = 1008
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return _get_addon_prefs().debug_options

    def draw(self, context):
        layout = self.layout
        prg = context.scene.stepper

        bxp = layout.box()
        bxp.label(text="Text Encoding")
        col = bxp.row().column(align=True)

        row = col.row()
        row.prop(prg, "fix_ascii_file")
        row = col.row()
        row.operator("object.occ_fix_ascii", text="Fix Text Encoding")

        # row = layout.row()
        # row.label(text="Error Messages")

        if (
            context.object is not None
            and "STEP_file" in context.object
            and context.object["STEP_file"] in global_file_cache
        ):
            bxp = layout.box()
            bxp.label(text="Reported Problems")

            row = bxp.row()
            col = row.column(align=True)
            step_reader = global_file_cache[context.object["STEP_file"]]
            for k, v in step_reader.import_problems.items():
                row = col.row()
                row.label(text=k + ": " + repr(v))

            bxs = layout.box()
            bxs.label(text="Skipped Shapes")

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
            row.label(text="Select an imported STEP object", icon="INFO")


def _get_addon_prefs():
    """Return the addon preferences instance."""
    return bpy.context.preferences.addons[__package__].preferences


class STEP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    build_materials: bpy.props.BoolProperty(
        name="Build Materials",
        description="Build materials from STEP file colors",
        default=True,
    )

    hack_skip_zero_solids: bpy.props.BoolProperty(
        name="Skip Faulty Solids",
        description="Skip corrupted/empty parts entirely (no healing or recovery attempts)",
        default=False,
    )

    skip_empty_objects: bpy.props.BoolProperty(
        name="Skip Empty Objects",
        description="Do not create objects for parts that produce no geometry",
        default=True,
    )

    debug_options: bpy.props.BoolProperty(
        name="Debug Options",
        description="Show the STEP - Debug panel, which reports what the "
                    "reader made of a file and what it had to skip",
        default=False,
    )

    debug_timing: bpy.props.BoolProperty(
        name="Debug Timing",
        description="Print detailed timing information during import",
        default=False,
    )

    active_matdb: bpy.props.EnumProperty(
        items=_matdb_enum_items,
        name="Material Database",
        description="Active material database for import",
    )

    matdb_dir: bpy.props.StringProperty(
        name="Material Database Folder",
        description="Folder that holds the .blend material databases. Leave it "
                    "empty to use the default folder, which an upgrade keeps. "
                    "You cannot share the default folder between machines or "
                    "with a team",
        subtype="DIR_PATH",
        default="",
    )

    remember_import_settings: bpy.props.BoolProperty(
        name="Remember Import Settings",
        description="Save the import dialog options after every import and "
                    "restore them in the next Blender session. This needs Save "
                    "Preferences on Quit, or a manual preferences save. Turn "
                    "this off to start from the defaults below instead",
        default=True,
    )

    last_import_settings: bpy.props.StringProperty(
        name="Last Import Settings",
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
        name="Background Import",
        description="Import in a background process so Blender stays "
                    "responsive (Esc cancels). Small files still import "
                    "directly",
        default=True,
    )

    background_min_mb: bpy.props.FloatProperty(
        name="Background Threshold (MB)",
        description="Import files smaller than this directly. The background "
                    "process takes too long to start to be worth it for them",
        default=2.0,
        min=0.0,
    )

    perf_calibration: bpy.props.StringProperty(
        name="Performance Calibration",
        description="Import speed measured on this machine, used by the "
                    "analyzer (the addon manages this)",
        default="{}",
    )

    check_for_updates: bpy.props.BoolProperty(
        name="Check for Updates",
        description="Ask GitHub once a day if a newer release exists, and show "
                    "a notice in the sidebar. The addon sends no information "
                    "about you or your files",
        default=True,
    )

    update_last_check: bpy.props.StringProperty(
        name="Last Update Check",
        description="Date of the last update check (the addon manages this)",
        default="",
        options={"HIDDEN"},
    )

    update_latest_tag: bpy.props.StringProperty(
        name="Latest Release Tag",
        description="Newest release tag seen on GitHub (the addon manages this)",
        default="",
        options={"HIDDEN"},
    )

    update_latest_url: bpy.props.StringProperty(
        name="Latest Release Download",
        description="Download address for the newest release (the addon manages"
                    " this)",
        default="",
        options={"HIDDEN"},
    )

    construction_filter_names: bpy.props.StringProperty(
        name="Construction Name Filters",
        description="Comma separated name prefixes to skip when Skip "
                    "construction geometry is on",
        default="Axes,Sketches,Lines,Hatches,Wires,Curves,Construction,"
                "Annotations,Planes,Origin",
    )

    def _enable_bridge_changed(self, context):
        if bridge_mod is None:
            return
        try:
            if bridge_mod.wanted(self):
                bridge_mod.start()
            else:
                bridge_mod.stop()
        except Exception as exc:
            print("CADder: bridge toggle failed:", exc)

    enable_bridge: bpy.props.BoolProperty(
        name="SolidWorks Bridge",
        description="Listen for the CADder Bridge add-in, so SolidWorks can "
                    "send an assembly into this Blender session, and show the "
                    "SolidWorks Bridge panel. The listener accepts "
                    "connections only from this computer",
        default=False,
        update=_enable_bridge_changed,
    )

    cad_link_advanced: bpy.props.BoolProperty(
        name="STEP Rig Panel",
        description="Show the STEP Rig panel, which holds the manifest file "
                    "and the stages of the rig run by hand. A direct send "
                    "from the CAD add-in does all of that itself, so most "
                    "users never need it",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        must_have_str = ".".join(str(i) for i in must_have_python)
        if sys.version_info[:2] != must_have_python:
            box = layout.box().column(align=True)
            box.alert = True
            box.label(text="CADder needs Python %s" % must_have_str,
                      icon="ERROR")
            box.label(text="This Blender runs Python %s"
                           % ".".join(str(i) for i in sys.version_info[:2]))
            box.operator("wm.url_open", icon="URL",
                         text="Blender Downloads").url =                 "https://www.blender.org/download/"
            return

        # A heading takes the repeated word out of the rows below it.
        col = layout.column(heading="Import")
        col.prop(self, "build_materials")
        col.prop(self, "skip_empty_objects")
        col.prop(self, "hack_skip_zero_solids")

        layout.separator()

        col = layout.column()
        col.prop(self, "debug_options")
        sub = col.column()
        # The timing numbers are part of the same picture, so they follow
        # the panel that shows the rest of it.
        sub.active = self.debug_options
        sub.prop(self, "debug_timing")

        layout.separator()

        col = layout.column()
        col.prop(self, "active_matdb")
        col.prop(self, "matdb_dir")
        # Only a legacy install keeps the databases in the addon folder. An
        # extension keeps them in its user folder, which an upgrade keeps.
        if not self.matdb_dir.strip() and not _matdb_user_dir():
            col.label(text="The addon folder holds the databases. "
                           "A reinstall clears it", icon="INFO")

        layout.separator()

        col = layout.column()
        col.prop(self, "remember_import_settings")
        sub = col.column()
        # The last used settings win over these three, so they are dead
        # while that switch is on.
        sub.active = not self.remember_import_settings
        sub.prop(self, "preferred_up_axis")
        sub.prop(self, "preferred_hierarchy")
        sub.prop(self, "default_quality_preset")
        col.prop(self, "construction_filter_names")

        layout.separator()

        col = layout.column()
        col.prop(self, "background_import")
        sub = col.column()
        sub.active = self.background_import
        sub.prop(self, "background_min_mb")

        # The bridge is for SolidWorks, which runs only on Windows. The
        # switch and what depends on it are not drawn elsewhere.
        if bridge_mod is not None and bridge_mod.SUPPORTED:
            layout.separator()

            col = layout.column()
            col.prop(self, "enable_bridge")
            sub = col.column()
            sub.active = self.enable_bridge
            sub.prop(self, "cad_link_advanced")
            if bridge_mod.is_running():
                col.label(text="Listening on 127.0.0.1:%d"
                               % bridge_mod.port(), icon="CHECKMARK")

        layout.separator()

        col = layout.column()
        col.prop(self, "check_for_updates")
        update = updater_mod.available_update(self)
        if update:
            row = col.row()
            row.label(text="Version %s is available" % update["version"],
                      icon="INFO")
            download = col.operator(
                "wm.url_open", icon="URL",
                text="Download %s" % update["version"])
            download.url = update["url"]
        else:
            col.label(text="CADder %s is up to date"
                           % updater_mod.version_string(), icon="CHECKMARK")
        kofi = col.operator("wm.url_open", icon="FUND",
                            text="Support Development on Ko-fi")
        kofi.url = updater_mod.KOFI_URL


def menu_func_import(self, context):
    self.layout.operator(ImportStepCADOperator.bl_idname, text="STEP/IGES/BREP CAD [CADder]")


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
    STEP_OT_MatDBSelectEntry,
    STEP_OT_MatDBRemoveEntry,
    STEP_OT_MaterialLock,
    STEP_OT_MaterialLockSelect,
    STEP_OT_GeometryLock,
    STEP_OT_GeometryLockSelect,
    STEP_UL_MaterialMappings,
    STEP_PT_STEPper_Info,
    CADLINK_PT_quality,
    STEP_PT_STEPper,
    STEP_PT_STEPper_Reload,
    STEP_PT_MaterialDB,
    STEP_PT_STEPper_UV,
    STEP_PT_STEPper_Debug,
) + (import_ui.classes + uv_mod.classes + tools_mod.classes
     + curves_mod.classes + formats_classes + analyzer_mod.classes
     + refresh_mod.classes
     + background_mod.classes)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.stepper = bpy.props.PointerProperty(type=PG_Stepper)
    material_lock.register()
    geometry_lock.register()
    bpy.app.handlers.depsgraph_update_post.append(_usage_on_update)
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post,
                     bpy.app.handlers.redo_post):
        handlers.append(_usage_stale)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    # The move comes first: the older choice is read against the listing of
    # the folder the databases end up in.
    try:
        _move_addon_matdbs()
    except Exception as exc:
        print("CADder: material databases not moved:", exc)
    _migrate_active_matdb()
    updater_mod.start()

    if rig_mod is not None:
        try:
            rig_mod.register()
        except Exception as exc:
            print("CADder: rig registration failed:", exc)
    if bridge_mod is not None:
        try:
            bridge_mod.register()
        except Exception as exc:
            print("CADder: bridge registration failed:", exc)


def unregister():
    updater_mod.stop()
    if bridge_mod is not None:
        try:
            bridge_mod.unregister()
        except Exception as exc:
            print("CADder: bridge unregistration failed:", exc)
    if rig_mod is not None:
        try:
            rig_mod.unregister()
        except Exception as exc:
            print("CADder: rig unregistration failed:", exc)
    for c in classes[::-1]:
        bpy.utils.unregister_class(c)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for handlers, handler in (
            (bpy.app.handlers.depsgraph_update_post, _usage_on_update),
            (bpy.app.handlers.load_post, _usage_stale),
            (bpy.app.handlers.undo_post, _usage_stale),
            (bpy.app.handlers.redo_post, _usage_stale)):
        if handler in handlers:
            handlers.remove(handler)
    geometry_lock.unregister()
    material_lock.unregister()
    del bpy.types.Scene.stepper
