# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds Blender objects from a .swmesh: the direct link.

The STEP path asks OpenCASCADE to rebuild a solid model from a neutral
file and then has to work out, afterwards, which imported object is which
component. This path skips both problems: the add-in already knows what
every body belongs to, so the geometry arrives tagged, and there is
nothing to match.

Two consequences worth stating, because they are what the path buys:

  * A definition becomes ONE Blender mesh datablock and every occurrence
    of it becomes an object sharing that datablock. A part used two
    hundred times costs two hundred objects and one mesh, that is real
    Blender instancing, not a copy.
  * Because the component ids come with the geometry, the match report
    this returns is exact by construction. The rig, pose sync and relink
    stages downstream read it exactly as they read the STEP path's.

What it does NOT buy is a solid model. These are triangles at the
tolerance the add-in was asked for, so a part that needs to be smoother
has to be asked for again, which is what the quality round trip is for.
"""

import bpy
from mathutils import Matrix

from . import matching, swmesh

# NOT RIG_rig: that tag means "part of the rig's own scaffolding", and
# parenting.relink skips anything carrying it. Tagging imported geometry
# with it made every part invisible to the re-link stage: they arrived in
# the right place and were never attached to a bone.
_TAG_COMPONENT = "RIG_component_id"
_TAG_GROUP = "RIG_group"
_TAG_DEFINITION = "SWMESH_definition"
_TAG_TOLERANCE = "SWMESH_tolerance_m"


def _material(spec, name_prefix):
    """A Principled BSDF carrying the SolidWorks appearance. Reused by name
    so a re-import does not pile up duplicates."""
    name = "%s%s" % (name_prefix, spec.name or "material")
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
    mat.diffuse_color = spec.rgba
    bsdf = mat.node_tree.nodes.get("Principled BSDF") if mat.use_nodes else None
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = spec.rgba
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = spec.roughness
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = spec.metallic
        alpha = bsdf.inputs.get("Alpha")
        if alpha is not None:
            alpha.default_value = spec.rgba[3]
    if spec.rgba[3] < 0.999:
        mat.blend_method = "BLEND"
    return mat


def _build_mesh(definition, materials, unit_scale):
    """One mesh datablock from one definition. foreach_set moves the whole
    buffer in one call: looping in Python over a hundred thousand vertices
    is the difference between instant and unusable."""
    me = bpy.data.meshes.new(definition.name or "SWMesh")
    me.vertices.add(definition.vertex_count)
    if unit_scale == 1.0:
        me.vertices.foreach_set("co", definition.positions)
    else:
        me.vertices.foreach_set(
            "co", [c * unit_scale for c in definition.positions])

    n = definition.triangle_count
    me.loops.add(n * 3)
    me.polygons.add(n)
    me.loops.foreach_set("vertex_index", definition.triangles)
    me.polygons.foreach_set("loop_start", range(0, n * 3, 3))
    me.polygons.foreach_set("loop_total", [3] * n)

    if definition.uvs is not None:
        uv = me.uv_layers.new(name="UVMap")
        # UVs are per LOOP in Blender and per vertex in the file, so they
        # have to be scattered through the triangle list.
        flat = []
        for v in definition.triangles:
            flat.append(definition.uvs[v * 2])
            flat.append(definition.uvs[v * 2 + 1])
        uv.data.foreach_set("uv", flat)

    me.update()
    me.validate(verbose=False)

    for spec in materials:
        me.materials.append(spec)
    if definition.triangle_materials is not None and materials:
        top = len(materials) - 1
        me.polygons.foreach_set(
            "material_index",
            [min(max(int(i), 0), top) for i in definition.triangle_materials])

    if definition.normals is not None:
        # Custom split normals last: they are invalidated by geometry edits,
        # and they are what makes a coarse tessellation still read as a
        # smooth surface.
        try:
            me.normals_split_custom_set_from_vertices(
                [(definition.normals[i * 3],
                  definition.normals[i * 3 + 1],
                  definition.normals[i * 3 + 2])
                 for i in range(definition.vertex_count)])
        except (RuntimeError, ValueError):
            me.shade_smooth()
    return me


def _matrix(transform, unit_scale):
    """The instance's row-major 4x4 as a Blender matrix, translation scaled
    into scene units."""
    rows = [transform[i * 4:(i + 1) * 4] for i in range(4)]
    for i in range(3):
        rows[i] = list(rows[i])
        rows[i][3] *= unit_scale
    return Matrix(rows)


def remove_previous(collection_name):
    """Clears a previous native import so a re-send replaces rather than
    accumulates. Meshes go too: an orphaned datablock of a million
    triangles is invisible in the outliner and very much present in the
    file."""
    removed = 0
    coll = bpy.data.collections.get(collection_name)
    if coll is None:
        return 0
    for obj in list(coll.objects):
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
        if isinstance(data, bpy.types.Mesh) and data.users == 0:
            bpy.data.meshes.remove(data)
    bpy.data.collections.remove(coll)
    return removed


def _group_of(manifest):
    """component id -> rigid group id. The re-link stage attaches geometry
    by GROUP, not by component, so an object that knows only which component
    it is never gets parented."""
    out = {}
    if manifest is None:
        return out
    for group in manifest.rigid_groups:
        for cid in group.components:
            out[cid] = group.id
    return out


def build(context, path, manifest=None, collection_name="SW_Native",
          unit_scale=1.0, material_prefix="SW "):
    """Reads a .swmesh and builds the scene. Returns (objects, MatchReport).

    The report is what ties this into the existing pipeline: every entry is
    exact, so pose sync, the rig build and relink behave as though matching
    had run and got everything right, which, here, it has. The manifest is
    needed for one thing only: the component-to-group map that re-linking
    attaches by."""
    scene = swmesh.load(path)
    group_of = _group_of(manifest)

    remove_previous(collection_name)
    coll = bpy.data.collections.new(collection_name)
    context.scene.collection.children.link(coll)

    materials = [_material(spec, material_prefix) for spec in scene.materials]
    meshes = {}
    for definition in scene.definitions:
        meshes[definition.id] = _build_mesh(definition, materials, unit_scale)

    objects = []
    report = matching.MatchReport()
    report.frame_rows = matching.identity_frame()
    for inst in scene.instances:
        me = meshes.get(inst.definition_id)
        if me is None:
            report.unmatched.append(inst.component_id)
            continue
        obj = bpy.data.objects.new(inst.name or inst.component_id, me)
        obj.matrix_world = _matrix(inst.transform, unit_scale)
        obj[_TAG_COMPONENT] = inst.component_id
        gid = group_of.get(inst.component_id)
        if gid is not None:
            obj[_TAG_GROUP] = gid
        obj[_TAG_DEFINITION] = inst.definition_id
        obj[_TAG_TOLERANCE] = scene.tolerance
        coll.objects.link(obj)
        objects.append(obj)
        report.matched.append(
            matching.MatchEntry(component_id=inst.component_id,
                                object_name=obj.name,
                                step=0,          # no search happened
                                confidence="exact"))

    context.view_layer.update()
    return objects, report


def refine(context, path, unit_scale=1.0, material_prefix="SW "):
    """Swaps in finer geometry for objects that are already in the scene.

    The objects themselves are kept (only their mesh DATA is replaced), so
    transforms, bone parenting, constraints and the rig survive untouched.
    That is the whole point: refining a part must not cost the pose it is
    in, or the round trip would be useless for exactly the assemblies it is
    meant for.

    Returns the objects whose geometry changed."""
    scene = swmesh.load(path)
    materials = [_material(spec, material_prefix) for spec in scene.materials]

    by_component = {}
    for obj in bpy.data.objects:
        cid = obj.get(_TAG_COMPONENT)
        if cid:
            by_component.setdefault(cid, []).append(obj)

    meshes = {}
    replaced = []
    retired = set()
    for inst in scene.instances:
        targets = by_component.get(inst.component_id)
        if not targets:
            continue
        me = meshes.get(inst.definition_id)
        if me is None:
            definition = scene.definition(inst.definition_id)
            if definition is None:
                continue
            me = _build_mesh(definition, materials, unit_scale)
            meshes[inst.definition_id] = me
        for obj in targets:
            if obj.data is me:
                continue
            if isinstance(obj.data, bpy.types.Mesh):
                retired.add(obj.data.name)
            obj.data = me
            obj[_TAG_DEFINITION] = inst.definition_id
            obj[_TAG_TOLERANCE] = scene.tolerance
            replaced.append(obj)

    # Only now: a datablock may still have been in use while the loop ran.
    for name in retired:
        old = bpy.data.meshes.get(name)
        if old is not None and old.users == 0:
            bpy.data.meshes.remove(old)

    context.view_layer.update()
    return replaced
