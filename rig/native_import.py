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

import os

import bpy
from mathutils import Matrix

from . import appearance, matdb, matching, progress, swmesh

# NOT RIG_rig: that tag means "part of the rig's own scaffolding", and
# parenting.relink skips anything carrying it. Tagging imported geometry
# with it made every part invisible to the re-link stage: they arrived in
# the right place and were never attached to a bone.
_TAG_COMPONENT = "RIG_component_id"
_TAG_GROUP = "RIG_group"
_TAG_DEFINITION = "SWMESH_definition"
# The CAD system's own id for the occurrence, from the manifest. The
# component ids (c001, c002) are per export and shift when a part is added
# in front of another; this one does not, so a later update can find the
# same occurrence after the assembly has been edited.
_TAG_PERSISTENT = "SWMESH_persistent_id"
_TAG_TOLERANCE = "SWMESH_tolerance_m"
# The occurrence path from the root of the assembly ("lifter-1/rod-2"):
# unique, and the same on the next send, so it identifies one placement
# where the component id cannot (a rigid subassembly is one component and
# many parts).
_TAG_PATH = "SWMESH_path"
# This placement inside its component's frame, 16 floats, row-major.
_TAG_LOCAL = "SWMESH_local"


def _material(spec, name_prefix, unit_scale=1.0):
    """A material carrying the SolidWorks appearance (appearance.py builds
    the node tree). Reused only when the appearance is the same one: two
    appearances can share a name (a library appearance with different
    mappings or decals, or the generic "color" in different colours,
    live cam-follower, 2026-09-15), and the second then becomes a
    material of its own.

    The appearance travels on the material as SWMESH_appearance (the
    add-in's JSON, untouched) and SWMESH_appearance_name (the name without
    Blender's .001 suffix), for a material database to key on."""
    base = "%s%s" % (name_prefix, spec.name or "material")
    parsed = appearance.parse(spec.appearance_json)
    if parsed is None:
        parsed = {"blender": {"roughness": spec.roughness, "metallic": spec.metallic},
                  "texture": spec.texture}
    identity = appearance.digest(spec.appearance_json or "%r|%r|%r|%r" % (
        tuple(round(c, 4) for c in spec.rgba), round(spec.roughness, 4),
        round(spec.metallic, 4), spec.texture))
    mat = None
    for cand in bpy.data.materials:
        if (cand.name == base or cand.name.startswith(base + ".")) \
                and cand.get("SWMESH_appearance_hash") == identity:
            mat = cand
            break
    if mat is None:
        mat = bpy.data.materials.new(base)
        appearance.build(mat, parsed, tuple(spec.rgba), unit_scale)
    mat["SWMESH_rgba"] = list(spec.rgba)
    mat["SWMESH_appearance_hash"] = identity
    mat["SWMESH_appearance_name"] = base
    if spec.appearance_json:
        mat["SWMESH_appearance"] = spec.appearance_json
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

    if definition.triangle_materials is not None and materials:
        # Only the materials this definition uses, so a part does not
        # carry every appearance in the assembly as an empty slot.
        top = len(materials) - 1
        used = sorted({min(max(int(i), 0), top) for i in definition.triangle_materials})
        slot = {}
        for scene_index in used:
            slot[scene_index] = len(me.materials)
            me.materials.append(materials[scene_index])
        me.polygons.foreach_set(
            "material_index",
            [slot[min(max(int(i), 0), top)] for i in definition.triangle_materials])

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


def _rehome_children(coll, home):
    """Moves every child collection of `coll` under `home`, so removing
    `coll` never takes a subtree out of the scene with it."""
    for child in list(coll.children):
        try:
            coll.children.unlink(child)
            if child.name not in {c.name for c in home.children}:
                home.children.link(child)
        except RuntimeError:
            continue


_TAG_FILE = "SWMESH_file"      # on every collection and object of an import


def _own_collections(stem):
    """Every collection a previous import of this file made (of any file,
    with stem None), leaves first, so each is empty of children by the
    time it is removed."""
    mine = [c for c in bpy.data.collections
            if c.get(_TAG_FILE) is not None and (stem is None or c.get(_TAG_FILE) == stem)]
    depth = {}

    def d(c):
        if c.name in depth:
            return depth[c.name]
        parents = [p for p in bpy.data.collections if c.name in p.children]
        depth[c.name] = 1 + max([d(p) for p in parents if p in mine], default=0)
        return depth[c.name]

    return sorted(mine, key=lambda c: -d(c))


def remove_previous(stem=None, scene_collection=None):
    """Clears the previous native import so a send replaces rather than
    accumulates: one direct send stands in a scene at a time, whatever
    assembly it was (a different one otherwise stacks a dead rig beside
    the live one on every send, live 2026-09-14). Meshes go too: an
    orphaned datablock of a million triangles is invisible in the outliner
    and very much present in the file. `stem` narrows the removal to one
    file's import; None takes every native import. The fixed collection
    name of scenes from before is taken as well."""
    removed = 0
    colls = _own_collections(stem)
    legacy = bpy.data.collections.get("SW_Native")
    if legacy is not None and legacy not in colls:
        colls.append(legacy)
    objects = [o for o in bpy.data.objects
               if o.get(_TAG_FILE) is not None and (stem is None or o.get(_TAG_FILE) == stem)]
    for coll in colls:
        for o in coll.objects:
            if o not in objects:
                objects.append(o)
    if not colls and not objects:
        return 0
    # The rigs these parts hang from. One whose every part goes with this
    # replace is left driving nothing, so it goes too: a DIFFERENT assembly
    # sent into the session otherwise stacks a dead rig beside the live
    # one on every send (live 2026-09-14).
    rigs = {}
    for obj in objects:
        arm = obj.parent
        if arm is not None and arm.type == "ARMATURE" and arm.get("RIG_rig"):
            rigs[arm.name] = arm
    for obj in objects:
        try:
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
        except ReferenceError:
            continue
        removed += 1
        if isinstance(data, bpy.types.Mesh) and data.users == 0:
            bpy.data.meshes.remove(data)
    # The rig build parks its collection inside the one holding the parts
    # it drives, which is this one. Removing it with the rig still inside
    # cut the rig (and the bone widgets under it) out of the scene while
    # keeping it in the file, so the next build found no rig collection
    # in the scene and made a numbered copy. Children move up first.
    for coll in colls:
        try:
            _rehome_children(coll, scene_collection or bpy.context.scene.collection)
            bpy.data.collections.remove(coll)
        except ReferenceError:
            pass
    orphaned = []
    for arm in rigs.values():
        try:
            still_driven = any(
                o.parent is arm and not o.get("RIG_rig") and not o.get("RIG_helper")
                for o in bpy.data.objects)
        except ReferenceError:
            continue
        if not still_driven:
            orphaned.append(arm)
    if orphaned:
        from . import rig_build
        names = [a.name for a in orphaned]
        for arm in orphaned:
            rig_build.remove_rig(arm)
        print("[CADLink native] removed %d rig(s) whose every part was "
              "replaced: %s" % (len(names), ", ".join(names)))
    return removed


def _persistent_of(manifest):
    """Component id -> the CAD system's persistent id, where there is one."""
    out = {}
    for c in (manifest.components if manifest is not None else []):
        if c.sw_persistent_id:
            out[c.id] = c.sw_persistent_id
    return out


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


# The CAD axis that becomes Blender's Z, as the STEP importer's up_as
# option spells it. "ZPOS" is no rotation: the manifest frame is kept.
_UP_ROTATIONS = {
    "XPOS": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
    "YPOS": ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "ZPOS": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
}


def up_frame(up_as):
    """The 4x4 that turns CAD coordinates so the chosen axis points up,
    as row lists. The same map main.transform_to_up applies to a STEP
    import (swap the up row with Z, negate it), so a direct send and a STEP
    import of one assembly land the same way."""
    r = _UP_ROTATIONS.get(up_as or "ZPOS", _UP_ROTATIONS["ZPOS"])
    return [[r[0][0], r[0][1], r[0][2], 0.0],
            [r[1][0], r[1][1], r[1][2], 0.0],
            [r[2][0], r[2][1], r[2][2], 0.0],
            [0.0, 0.0, 0.0, 1.0]]


# The STEP importer's modes, with the same collection names and roles, so
# a direct send and a STEP import of one assembly look alike in the
# outliner. FLAT: one collection per part name. TREE: nested collections
# mirroring the assembly. EMPTIES: parts parented under empties mirroring
# the assembly. COLLECTION_INSTANCES: one prototype per part in a hidden
# ".components" collection, every occurrence an instancing empty.
HIERARCHIES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")


def _collection(name, stem, role, parent):
    """A collection that says which import made it and what for, so a
    re-send can take its own away and leave the user's alone."""
    if len(name) > 50:
        name = name[:25] + "_" + name[-25:]
    col = bpy.data.collections.new(name)
    col[_TAG_FILE] = stem
    col["SWMESH_role"] = role
    parent.children.link(col)
    return col


def _paths(manifest):
    """component id -> sw_path, and sw_path -> component, from the
    manifest. The path is the assembly tree: "sub-1/part-2" hangs under
    "sub-1"."""
    by_id, by_path = {}, {}
    if manifest is None:
        return by_id, by_path
    for c in manifest.components:
        by_id[c.id] = c.sw_path or c.id
        by_path[c.sw_path or c.id] = c
    return by_id, by_path


class _Branch(object):
    """One node of the assembly tree: what to call it and where it sits."""

    def __init__(self, name, transform=None):
        self.name = name
        self.transform = transform


def _branches(scene, by_path, unit_scale):
    """path -> _Branch for every subassembly the file names.

    The geometry file carries the tree from version 3 on, which is what
    lets a send with no rig build a hierarchy at all. An older file has
    only the manifest, and the manifest only knows the occurrences the
    walk visited, so a part inside a rigid subassembly is not in it."""
    out = {}
    for node in getattr(scene, "nodes", []):
        if not node.path:
            continue
        out[node.path] = _Branch(
            node.name or node.path.rpartition("/")[2],
            _matrix(node.transform, unit_scale) if node.transform else None)
    for path, comp in by_path.items():
        if path in out:
            continue
        rows = [v for row in comp.transform for v in row] if comp.transform else None
        out[path] = _Branch(path.rpartition("/")[2],
                            _matrix(rows, unit_scale) if rows else None)
    return out


def _instance_path(inst, by_id):
    """Where this placement hangs in the assembly tree.

    The file says so from version 3. Before that the manifest had to, and
    a component the walk never visited fell back to its own name, which is
    a leaf at the top: flat, but never wrong."""
    return inst.path or by_id.get(inst.component_id) or inst.name or inst.component_id


def _object_colour(obj):
    """The viewport colour, so a Solid view set to Object colour matches
    the CAD, as the STEP importer does."""
    data = obj.data if obj.type == "MESH" else (
        obj.instance_collection.objects[0].data
        if obj.instance_collection is not None and obj.instance_collection.objects else None)
    if data is None or not data.materials:
        return
    mat = data.materials[0]
    if mat is not None and mat.get("SWMESH_rgba") is not None:
        # The material's viewport colour, which the appearance builder has
        # already made linear; the record's own rgba is display sRGB.
        obj.color = tuple(mat.diffuse_color)


def build(context, path, manifest=None, collection_name=None,
          unit_scale=1.0, material_prefix="SW ", up_as="ZPOS",
          hierarchy="FLAT", report_to=None):
    """Reads a .swmesh and builds the scene. Returns (objects, MatchReport).

    The report is what ties this into the existing pipeline: every entry is
    exact, so pose sync, the rig build and relink behave as though matching
    had run and got everything right, which, here, it has. The manifest
    gives the component-to-group map that re-linking attaches by, and the
    assembly paths the tree modes are built from.

    up_as is the CAD axis that becomes Blender's up (the STEP importer's
    option, same spelling): the geometry is turned and the report's frame
    says so, so the rig lands on it. hierarchy is the STEP importer's
    hierarchy_types; group_in_collection wraps the import in one
    collection named after the file, as the importer does."""
    scene = swmesh.load(path)
    group_of = _group_of(manifest)
    persistent_of = _persistent_of(manifest)
    by_id, by_path = _paths(manifest)
    frame_rows = up_frame(up_as)
    frame = Matrix([tuple(r) for r in frame_rows])
    stem = os.path.splitext(os.path.basename(path))[0]
    if hierarchy not in HIERARCHIES:
        hierarchy = "FLAT"

    remove_previous(None, context.scene.collection)
    destination = context.scene.collection
    # One collection, named after the assembly. The shape of what is
    # inside it is the hierarchy option's business, not the name's
    # (Oscar, 2026-09-16: a send should simply put the assembly in a
    # collection of its own name).
    role = {"FLAT": "flat", "TREE": "hierarchy", "EMPTIES": "hierarchy",
            "COLLECTION_INSTANCES": "hierarchy"}[hierarchy]
    root = _collection(stem, stem, role, destination)

    materials = [_material(spec, material_prefix, unit_scale) for spec in scene.materials]
    meshes = {}
    for definition in scene.definitions:
        meshes[definition.id] = _build_mesh(definition, materials, unit_scale)

    # Prototypes for the instancing mode: one object per definition at the
    # origin, in a hidden ".components" collection, tagged like the parts
    # so a refine finds the mesh to swap.
    prototypes = {}
    if hierarchy == "COLLECTION_INSTANCES":
        # Inside the assembly's own collection, so the scene shows one
        # collection per send and not a hidden second one beside it.
        components = _collection(stem + ".components", stem, "components", root)
        for definition in scene.definitions:
            me = meshes.get(definition.id)
            if me is None:
                continue
            part_col = _collection(definition.name or ("definition %d" % definition.id),
                                   stem, "part", components)
            proto = bpy.data.objects.new(definition.name or "part", me)
            proto[_TAG_FILE] = stem
            proto[_TAG_DEFINITION] = definition.id
            proto[_TAG_TOLERANCE] = scene.tolerance
            proto["SWMESH_prototype"] = True
            _material_names(proto)
            part_col.objects.link(proto)
            prototypes[definition.id] = part_col
        _exclude(context, components)

    # Subassembly nodes for the tree modes: every path prefix above a part.
    # A node is named after the DOCUMENT it references, as the STEP route
    # names its products, and not after the occurrence ("lifter", not
    # "lifter-2"): the tree then reads the same whichever route brought it.
    branches = _branches(scene, by_path, unit_scale)
    tree_cols = {"": root}
    empties = {}

    def branch_name(sw_path):
        branch = branches.get(sw_path)
        if branch is not None and branch.name:
            return branch.name
        return sw_path.rpartition("/")[2]

    def node_col(sw_path):
        if sw_path in tree_cols:
            return tree_cols[sw_path]
        parent_path = sw_path.rpartition("/")[0]
        col = _collection(branch_name(sw_path), stem, "node", node_col(parent_path))
        col["SWMESH_path"] = sw_path
        tree_cols[sw_path] = col
        return col

    def node_empty(sw_path):
        if not sw_path:
            return None
        if sw_path in empties:
            return empties[sw_path]
        parent = node_empty(sw_path.rpartition("/")[0])
        emp = bpy.data.objects.new(branch_name(sw_path), None)
        emp.empty_display_size = 2
        emp.empty_display_type = "PLAIN_AXES"
        emp[_TAG_FILE] = stem
        emp[_TAG_PATH] = sw_path
        comp = by_path.get(sw_path)
        if comp is not None:
            emp[_TAG_COMPONENT] = comp.id
        branch = branches.get(sw_path)
        if branch is not None and branch.transform is not None:
            emp.matrix_world = frame @ branch.transform
        root.objects.link(emp)
        if parent is not None:
            emp.parent = parent
            emp.matrix_parent_inverse = parent.matrix_world.inverted()
        empties[sw_path] = emp
        return emp

    objects = []
    flat_groups = {}
    report = matching.MatchReport()
    report.frame_rows = frame_rows
    # Placing the instances is the long part of a large send, so it is the
    # part that counts itself out.
    said = report_to or progress.NONE
    said.stage("placing the parts", 20, 85, len(scene.instances))
    placed = 0
    for inst in scene.instances:
        placed += 1
        said.step(placed)
        me = meshes.get(inst.definition_id)
        if me is None:
            report.unmatched.append(inst.component_id)
            continue
        name = inst.name or inst.component_id
        if hierarchy == "COLLECTION_INSTANCES":
            obj = bpy.data.objects.new(name, None)
            obj.instance_type = "COLLECTION"
            obj.instance_collection = prototypes.get(inst.definition_id)
            obj.empty_display_size = 0.01
        else:
            obj = bpy.data.objects.new(name, me)
        placement = _matrix(inst.transform, unit_scale)
        obj.matrix_world = frame @ placement
        obj[_TAG_FILE] = stem
        obj[_TAG_COMPONENT] = inst.component_id
        gid = group_of.get(inst.component_id)
        if gid is not None:
            obj[_TAG_GROUP] = gid
        obj[_TAG_DEFINITION] = inst.definition_id
        obj[_TAG_TOLERANCE] = scene.tolerance
        persistent = persistent_of.get(inst.component_id)
        if persistent:
            obj[_TAG_PERSISTENT] = persistent

        sw_path = _instance_path(inst, by_id)
        obj[_TAG_PATH] = sw_path
        # Where this part sits INSIDE its component, as the file states it.
        # A part that is its own component carries nothing; a part inside a
        # rigid subassembly carries its place in it, because the
        # subassembly is the component. Pose sync moves a component to
        # where the CAD says it is now, and without this it would stack
        # every part of a subassembly on that one point.
        if inst.local:
            obj[_TAG_LOCAL] = list(inst.local)

        parent_path = sw_path.rpartition("/")[0]
        if hierarchy == "FLAT":
            key = me.name
            col = flat_groups.get(key)
            if col is None:
                col = flat_groups[key] = _collection(key, stem, "group", root)
            col.objects.link(obj)
        elif hierarchy == "TREE":
            node_col(parent_path).objects.link(obj)
        else:
            root.objects.link(obj)
            parent = node_empty(parent_path)
            if parent is not None:
                obj.parent = parent
                obj.matrix_parent_inverse = parent.matrix_world.inverted()
        _object_colour(obj)
        _material_names(obj)
        objects.append(obj)
        report.matched.append(
            matching.MatchEntry(component_id=inst.component_id,
                                object_name=obj.name,
                                step=0,          # no search happened
                                confidence="exact"))

    # Every instance was placed through the frame, so every one anchors
    # it. The Build Rig operator trusts a frame only when something agreed
    # with it (an unanchored match frame is identity by default, and the
    # rig would land at the origin instead of the cursor). Without this
    # count a Y-up send turned the geometry and left the rig in the
    # manifest frame, its bones across the parts.
    # The material database, as a STEP import applies it: the materials
    # are named after the SolidWorks appearance and every object carries
    # STEP_materials, so an entry for "SW polished gold" replaces it here.
    matdb.apply(objects + prototypes_objects(prototypes), "direct send")
    report.frame_agree = len(report.matched)
    context.view_layer.update()
    return objects, report


def prototypes_objects(prototypes):
    """The prototype objects of the instancing mode, which hold the meshes
    and so the material slots."""
    out = []
    for collection in (prototypes or {}).values():
        out.extend(collection.objects)
    return out


def _material_names(obj):
    """STEP_materials, the property the CADder material database
    matches on: the original name of the material in each slot, so a
    database entry for "polished gold" replaces it on a direct send
    exactly as on a STEP import."""
    data = obj.data if obj.type == "MESH" else None
    if data is None and obj.instance_collection is not None:
        return
    if data is None:
        return
    import json
    names = [(m.get("SWMESH_appearance_name") or m.name) if m is not None else ""
             for m in data.materials]
    obj["STEP_materials"] = json.dumps(names)


def _exclude(context, collection):
    """Hides a collection from the view layer, as the STEP importer hides
    its prototypes."""
    def find(layer_col):
        if layer_col.collection == collection:
            return layer_col
        for child in layer_col.children:
            found = find(child)
            if found is not None:
                return found
        return None
    lc = find(context.view_layer.layer_collection)
    if lc is not None:
        lc.exclude = True


def _mesh_holders(obj):
    """The objects whose mesh data is this part's geometry.

    A part that came in as its own mesh holds it. A part that came in as
    a collection instance is an empty, and the geometry is on the
    prototype objects inside the collection it instances. Those
    prototypes are shared by every instance of the same part, so
    replacing one replaces them all, which is what a re-tessellation
    means."""
    if obj.type == "MESH":
        return [obj]
    collection = getattr(obj, "instance_collection", None)
    if collection is None:
        return []
    return [o for o in collection.all_objects if o.type == "MESH"]


def refine(context, path, unit_scale=1.0, material_prefix="SW "):
    """Swaps in finer geometry for objects that are already in the scene.

    The objects themselves are kept (only their mesh DATA is replaced), so
    transforms, bone parenting, constraints and the rig survive untouched.
    That is the whole point: refining a part must not cost the pose it is
    in, or the round trip would be useless for exactly the assemblies it is
    meant for.

    Returns the objects whose geometry changed."""
    scene = swmesh.load(path)
    materials = [_material(spec, material_prefix, unit_scale) for spec in scene.materials]

    by_component = {}
    by_path = {}
    for obj in bpy.data.objects:
        cid = obj.get(_TAG_COMPONENT)
        if cid:
            by_component.setdefault(cid, []).append(obj)
        path = obj.get(_TAG_PATH)
        if path:
            by_path.setdefault(path, []).append(obj)

    meshes = {}
    replaced = []
    retired = set()
    for inst in scene.instances:
        # By PATH first: a rigid subassembly is one component and several
        # parts, and the component id alone would give every part of it the
        # same mesh.
        targets = by_path.get(inst.path) if inst.path else None
        if not targets:
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
            # A part sent as a collection instance is an empty: the mesh
            # sits on the prototype object inside the collection it
            # instances. Assigning a mesh to the empty itself raised
            # "Object.data expected a Image type" (Oscar, 2026-09-16),
            # because an empty's data slot takes an image, not geometry.
            for holder in _mesh_holders(obj):
                if holder.data is me:
                    continue
                if isinstance(holder.data, bpy.types.Mesh):
                    retired.add(holder.data.name)
                holder.data = me
                _material_names(holder)
                if holder not in replaced:
                    replaced.append(holder)
            obj[_TAG_DEFINITION] = inst.definition_id
            obj[_TAG_TOLERANCE] = scene.tolerance
            if obj not in replaced:
                replaced.append(obj)

    matdb.apply(replaced, "refine")

    # Only now: a datablock may still have been in use while the loop ran.
    for name in retired:
        old = bpy.data.meshes.get(name)
        if old is not None and old.users == 0:
            bpy.data.meshes.remove(old)

    context.view_layer.update()
    return replaced
