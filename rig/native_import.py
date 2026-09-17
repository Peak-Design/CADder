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

import hashlib
import os
import struct
from dataclasses import dataclass, field
from typing import List

import bpy
from mathutils import Matrix

from . import appearance, matdb, matching, progress, swmesh
from . import diff as diff_mod

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
# A short signature of the triangles this object came in with, so an update
# can tell a part that was re-tessellated from one that came back the same.
_TAG_GEOMETRY = "SWMESH_geometry"
_TAG_TRANSFORM = diff_mod.TAG_TRANSFORM


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
    file's import; None takes every native import."""
    removed = 0
    colls = _own_collections(stem)
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
        # A locked rig outlives its geometry on purpose: the send is
        # bringing the parts back, and the rig is to receive them.
        if not still_driven and not arm.get("RIG_locked"):
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


class _Placer:
    """Everything one import needs to make a part and put it where it
    belongs: the meshes, the collections, the branch empties, the tags.

    A first import and an update of one both go through this, so a part
    that arrives in an update lands exactly where the same part would have
    landed in a fresh send.
    """

    def __init__(self, context, scene, manifest, stem, root, frame,
                 unit_scale, hierarchy, material_prefix):
        self.context = context
        self.scene = scene
        self.manifest = manifest
        self.stem = stem
        self.root = root
        self.frame = frame
        self.unit_scale = unit_scale
        self.hierarchy = hierarchy
        self.material_prefix = material_prefix

        self.group_of = _group_of(manifest)
        self.persistent_of = _persistent_of(manifest)
        self.by_id, self.by_path = _paths(manifest)
        self.branches = _branches(scene, self.by_path, unit_scale)
        self.definitions = {d.id: d for d in scene.definitions}

        self._materials = None
        self._meshes = {}
        self.prototypes = {}
        self.components_collection = None
        self.tree_cols = {"": root}
        self.empties = {}
        self.flat_groups = {}

    # ── materials and meshes, built only when something needs them ──────
    #
    # An update usually changes a handful of parts out of hundreds. Building
    # a Blender mesh for every definition in the file would cost the whole
    # import again to change three of them.

    @property
    def materials(self):
        if self._materials is None:
            self._materials = [_material(spec, self.material_prefix, self.unit_scale)
                               for spec in self.scene.materials]
        return self._materials

    def mesh(self, definition_id):
        if definition_id in self._meshes:
            return self._meshes[definition_id]
        definition = self.definitions.get(definition_id)
        me = None
        if definition is not None:
            me = _build_mesh(definition, self.materials, self.unit_scale)
        self._meshes[definition_id] = me
        return me

    def build_prototypes(self):
        """The instancing mode's hidden prototypes: one object per
        definition, in a collection of its own."""
        if self.hierarchy != "COLLECTION_INSTANCES":
            return
        # Inside the assembly's own collection, so the scene shows one
        # collection per send and not a hidden second one beside it.
        components = _collection(self.stem + ".components", self.stem,
                                 "components", self.root)
        self.components_collection = components
        for definition in self.scene.definitions:
            me = self.mesh(definition.id)
            if me is None:
                continue
            part_col = _collection(
                definition.name or ("definition %d" % definition.id),
                self.stem, "part", components)
            proto = bpy.data.objects.new(definition.name or "part", me)
            proto[_TAG_FILE] = self.stem
            proto[_TAG_DEFINITION] = definition.id
            proto[_TAG_TOLERANCE] = self.scene.tolerance
            proto["SWMESH_prototype"] = True
            _material_names(proto)
            part_col.objects.link(proto)
            self.prototypes[definition.id] = part_col
        _exclude(self.context, components)

    # ── the assembly tree ───────────────────────────────────────────────

    def branch_name(self, sw_path):
        branch = self.branches.get(sw_path)
        if branch is not None and branch.name:
            return branch.name
        return sw_path.rpartition("/")[2]

    def node_col(self, sw_path):
        if sw_path in self.tree_cols:
            return self.tree_cols[sw_path]
        parent_path = sw_path.rpartition("/")[0]
        col = _collection(self.branch_name(sw_path), self.stem, "node",
                          self.node_col(parent_path))
        col[_TAG_PATH] = sw_path
        self.tree_cols[sw_path] = col
        return col

    def node_empty(self, sw_path):
        if not sw_path:
            return None
        if sw_path in self.empties:
            return self.empties[sw_path]
        parent = self.node_empty(sw_path.rpartition("/")[0])
        emp = bpy.data.objects.new(self.branch_name(sw_path), None)
        emp.empty_display_size = 2
        emp.empty_display_type = "PLAIN_AXES"
        emp[_TAG_FILE] = self.stem
        emp[_TAG_PATH] = sw_path
        comp = self.by_path.get(sw_path)
        if comp is not None:
            emp[_TAG_COMPONENT] = comp.id
        branch = self.branches.get(sw_path)
        if branch is not None and branch.transform is not None:
            emp.matrix_world = self.frame @ branch.transform
        self.root.objects.link(emp)
        if parent is not None:
            emp.parent = parent
            emp.matrix_parent_inverse = parent.matrix_world.inverted()
        self.empties[sw_path] = emp
        return emp

    def adopt_tree(self):
        """Takes over the collections and empties a previous import of this
        file left, so an update reuses them instead of building a second
        tree beside the first. Anything the user put inside one of them
        stays where it is."""
        for col in bpy.data.collections:
            if col.get(_TAG_FILE) != self.stem:
                continue
            path = col.get(_TAG_PATH)
            if col.get("SWMESH_role") == "node" and path is not None:
                self.tree_cols[path] = col
            elif col.get("SWMESH_role") == "group":
                self.flat_groups[col.name] = col
            elif col.get("SWMESH_role") == "components":
                self.components_collection = col
            elif col.get("SWMESH_role") == "part":
                for obj in col.objects:
                    if obj.get("SWMESH_prototype"):
                        self.prototypes[_int(obj.get(_TAG_DEFINITION), -1)] = col
        for obj in bpy.data.objects:
            if obj.get(_TAG_FILE) != self.stem or obj.type != "EMPTY":
                continue
            if obj.get("SWMESH_prototype") or obj.get(_TAG_GROUP) is not None:
                continue
            path = obj.get(_TAG_PATH)
            if path and obj.instance_collection is None:
                self.empties[path] = obj

    # ── the parts themselves ────────────────────────────────────────────

    def create(self, inst):
        """One new object for one placement, tagged and placed. None when
        the file holds no geometry for it."""
        me = self.mesh(inst.definition_id)
        if me is None:
            return None
        name = inst.name or inst.component_id
        if self.hierarchy == "COLLECTION_INSTANCES":
            obj = bpy.data.objects.new(name, None)
            obj.instance_type = "COLLECTION"
            obj.instance_collection = self.prototypes.get(inst.definition_id)
            obj.empty_display_size = 0.01
        else:
            obj = bpy.data.objects.new(name, me)
        self.retag(obj, inst)
        self.pose(obj, inst)
        self.place(obj, self.path_of(inst))
        _object_colour(obj)
        _material_names(obj)
        return obj

    def path_of(self, inst):
        return _instance_path(inst, self.by_id)

    def retag(self, obj, inst):
        """The tags that describe THIS export. Every one of them can change
        between two sends of one assembly, so an update rewrites them all
        rather than trusting what is there."""
        obj[_TAG_FILE] = self.stem
        obj[_TAG_COMPONENT] = inst.component_id
        gid = self.group_of.get(inst.component_id)
        if gid is not None:
            obj[_TAG_GROUP] = gid
        elif _TAG_GROUP in obj.keys():
            del obj[_TAG_GROUP]
        obj[_TAG_DEFINITION] = inst.definition_id
        obj[_TAG_TOLERANCE] = self.scene.tolerance
        obj[_TAG_PATH] = self.path_of(inst)
        # What the CAD application said the pose was, kept for the next
        # comparison. The object's own matrix cannot answer that question:
        # a part the user moved in Blender would read as moved in the CAD
        # application and be dragged back on the next update.
        obj[_TAG_TRANSFORM] = list(inst.transform or [])
        persistent = self.persistent_of.get(inst.component_id)
        if persistent:
            obj[_TAG_PERSISTENT] = persistent
        # Where this part sits INSIDE its component, as the file states it.
        # A part that is its own component carries nothing; a part inside a
        # rigid subassembly carries its place in it, because the
        # subassembly is the component. Pose sync moves a component to
        # where the CAD says it is now, and without this it would stack
        # every part of a subassembly on that one point.
        if inst.local:
            obj[_TAG_LOCAL] = list(inst.local)
        elif _TAG_LOCAL in obj.keys():
            del obj[_TAG_LOCAL]
        signature = self.geometry_hash(inst)
        if signature is not None:
            obj[_TAG_GEOMETRY] = signature

    def geometry_hash(self, inst):
        definition = self.definitions.get(inst.definition_id)
        return None if definition is None else _definition_hash(definition)

    def pose(self, obj, inst):
        obj.matrix_world = self.frame @ _matrix(inst.transform, self.unit_scale)

    def place(self, obj, sw_path):
        """Into the collection, or under the empty, the mode calls for."""
        parent_path = sw_path.rpartition("/")[0]
        if self.hierarchy == "FLAT":
            key = obj.data.name if obj.data is not None else (obj.name or "part")
            col = self.flat_groups.get(key)
            if col is None:
                col = self.flat_groups[key] = _collection(
                    key, self.stem, "group", self.root)
            col.objects.link(obj)
        elif self.hierarchy == "TREE":
            self.node_col(parent_path).objects.link(obj)
        else:
            self.root.objects.link(obj)
            parent = self.node_empty(parent_path)
            if parent is not None:
                obj.parent = parent
                obj.matrix_parent_inverse = parent.matrix_world.inverted()

    def unplace(self, obj):
        """Out of the collections THIS import made, and off its branch
        empty. A collection of the user's own keeps the object."""
        for col in list(obj.users_collection):
            if col.get(_TAG_FILE) == self.stem:
                col.objects.unlink(obj)
        parent = obj.parent
        if parent is not None and parent.get(_TAG_FILE) == self.stem \
                and parent.type == "EMPTY":
            world = obj.matrix_world.copy()
            obj.parent = None
            obj.matrix_world = world


def _int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _definition_hash(definition):
    """A short, exact signature of one part's geometry, so an update can
    tell a part that was re-tessellated from one that was not and leave the
    mesh (and the work done on it in Blender) alone."""
    digest = hashlib.blake2b(digest_size=8)
    digest.update(struct.pack("<II", definition.vertex_count,
                              definition.triangle_count))
    for block in (definition.positions, definition.triangles,
                  definition.triangle_materials):
        if block is not None:
            digest.update(memoryview(block).cast("B"))
    return digest.hexdigest()


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
    collection named after the file, as the importer does.

    This REPLACES what was there. To change only what changed, keeping the
    rest of the scene, see update()."""
    scene = swmesh.load(path)
    frame_rows = up_frame(up_as)
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

    placer = _Placer(context, scene, manifest, stem, root,
                     Matrix([tuple(r) for r in frame_rows]), unit_scale,
                     hierarchy, material_prefix)
    placer.build_prototypes()

    objects = []
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
        obj = placer.create(inst)
        if obj is None:
            report.unmatched.append(inst.component_id)
            continue
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
    matdb.apply(objects + prototypes_objects(placer.prototypes), "direct send")
    report.frame_agree = len(report.matched)
    context.view_layer.update()
    return objects, report


@dataclass
class UpdateReport:
    """What an update did, in the words the user reads."""

    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    moved: List[str] = field(default_factory=list)
    reshaped: List[str] = field(default_factory=list)
    kept: int = 0
    structural: bool = False

    def describe(self):
        return ("%d part(s) added, %d removed, %d moved, %d re-tessellated, "
                "%d unchanged" % (len(self.added), len(self.removed),
                                  len(self.moved), len(self.reshaped), self.kept))


def update(context, path, manifest=None, unit_scale=1.0,
           material_prefix="SW ", up_as="ZPOS", hierarchy="FLAT",
           report_to=None):
    """Brings the scene up to date with a new export, changing only what
    changed. Returns (objects, MatchReport, UpdateReport).

    A send replaces the import outright, which throws away everything done
    in Blender since. This compares the two assemblies part by part
    (rig/diff.py), and then:

      * a part that is still there and still the same shape keeps its
        object, its mesh, its materials and its modifiers, and is only
        moved and re-tagged,
      * a part that was re-tessellated or changed shape takes the new mesh
        on the SAME object,
      * a part that is new is built and placed as a fresh import would,
      * a part that has gone is removed with its mesh.

    The rig is not touched here. What to do with it is a separate question
    and a separate answer: see rig_update.py."""
    scene = swmesh.load(path)
    frame_rows = up_frame(up_as)
    stem = os.path.splitext(os.path.basename(path))[0]
    if hierarchy not in HIERARCHIES:
        hierarchy = "FLAT"
    said = report_to or progress.NONE

    root = None
    for col in bpy.data.collections:
        if col.get(_TAG_FILE) == stem and col.get("SWMESH_role") in (
                "flat", "hierarchy"):
            root = col
            break
    if root is None:
        # Nothing of this assembly is in the scene: an update of nothing is
        # an import.
        objects, report = build(
            context, path, manifest=manifest, unit_scale=unit_scale,
            material_prefix=material_prefix, up_as=up_as, hierarchy=hierarchy,
            report_to=report_to)
        out = UpdateReport(added=[o.name for o in objects], structural=True)
        return objects, report, out

    placer = _Placer(context, scene, manifest, stem, root,
                     Matrix([tuple(r) for r in frame_rows]), unit_scale,
                     hierarchy, material_prefix)
    placer.adopt_tree()
    if hierarchy == "COLLECTION_INSTANCES" and not placer.prototypes:
        placer.build_prototypes()

    old = diff_mod.from_objects(bpy.data.objects, stem)
    new = diff_mod.from_scene_file(scene, manifest)
    changes = diff_mod.compare(old, new)

    said.stage("bringing the parts up to date", 20, 85,
               len(changes.pairs) + len(changes.added) + len(changes.removed))
    done = 0
    out = UpdateReport(structural=changes.structural)
    objects = []
    report = matching.MatchReport()
    report.frame_rows = frame_rows

    for pair in changes.pairs:
        done += 1
        said.step(done)
        obj = pair.old.payload
        inst = pair.new.payload
        try:
            # New geometry only where the geometry is actually new: the
            # hash is of the triangles themselves, so a part that came back
            # identical keeps the mesh it has, with whatever was done to it
            # in Blender.
            reshaped = obj.get(_TAG_GEOMETRY) != placer.geometry_hash(inst)
        except ReferenceError:
            continue
        placer.retag(obj, inst)
        if pair.moved:
            placer.pose(obj, inst)
            out.moved.append(obj.name)
        if reshaped:
            _reshape(obj, placer, inst)
            out.reshaped.append(obj.name)
        if pair.old.path != pair.new.path:
            placer.unplace(obj)
            placer.place(obj, placer.path_of(inst))
        if not pair.moved and not reshaped:
            out.kept += 1
        objects.append(obj)
        report.matched.append(matching.MatchEntry(
            component_id=inst.component_id, object_name=obj.name,
            step=0, confidence="exact"))

    for occurrence in changes.added:
        done += 1
        said.step(done)
        obj = placer.create(occurrence.payload)
        if obj is None:
            report.unmatched.append(occurrence.component_id)
            continue
        objects.append(obj)
        out.added.append(obj.name)
        report.matched.append(matching.MatchEntry(
            component_id=occurrence.component_id, object_name=obj.name,
            step=0, confidence="exact"))

    doomed = []
    for occurrence in changes.removed:
        done += 1
        said.step(done)
        obj = occurrence.payload
        try:
            out.removed.append(obj.name)
        except ReferenceError:
            continue
        doomed.append(obj)
    _remove_objects(doomed)
    _prune_tree(placer, new)

    matdb.apply(objects + prototypes_objects(placer.prototypes), "update")
    report.frame_agree = len(report.matched)
    context.view_layer.update()
    return objects, report, out


def _reshape(obj, placer, inst):
    """New geometry on an object that is already in the scene: the mesh
    DATA is replaced and the object is not, so its place, its parent, its
    modifiers and its bone all survive."""
    me = placer.mesh(inst.definition_id)
    if me is None:
        return
    for holder in _mesh_holders(obj):
        old = holder.data
        if old is me:
            continue
        holder.data = me
        _material_names(holder)
        if isinstance(old, bpy.types.Mesh) and old.users == 0:
            bpy.data.meshes.remove(old)
    _object_colour(obj)


def _remove_objects(doomed):
    for obj in doomed:
        try:
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
        except ReferenceError:
            continue
        if isinstance(data, bpy.types.Mesh) and data.users == 0:
            bpy.data.meshes.remove(data)


def _prune_tree(placer, occurrences):
    """Branch collections and empties the new assembly no longer has. One
    that still holds something (a part of the user's own, or a part this
    update could not place) is kept: an empty branch is tidy, a branch with
    something in it is somebody's work."""
    wanted = set()
    for occurrence in occurrences:
        path = occurrence.path
        while "/" in path:
            path = path.rpartition("/")[0]
            wanted.add(path)
    for path, col in list(placer.tree_cols.items()):
        if not path or path in wanted:
            continue
        try:
            if col.objects or col.children:
                continue
            bpy.data.collections.remove(col)
        except (ReferenceError, RuntimeError):
            pass
        placer.tree_cols.pop(path, None)
    for path, emp in list(placer.empties.items()):
        if path in wanted:
            continue
        try:
            if any(o.parent is emp for o in bpy.data.objects):
                continue
            bpy.data.objects.remove(emp, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass
        placer.empties.pop(path, None)


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
    mismatched = []
    for inst in scene.instances:
        # By PATH first: a rigid subassembly is one component and several
        # parts, and the component id alone would give every part of it the
        # same mesh.
        targets = by_path.get(inst.path) if inst.path else None
        if not targets:
            targets = by_component.get(inst.component_id)
            # The component owns several objects (its bodies, or the parts
            # of a rigid subassembly) and this geometry does not say which
            # one it is. Putting it on all of them draws the whole part
            # over itself once per object, which is worse than leaving the
            # geometry as it is and saying so.
            if targets and len(targets) > 1:
                mismatched.append(inst.path or inst.component_id)
                continue
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

    if mismatched:
        print("[CADLink native] %d piece(s) of geometry did not name an object "
              "in this scene and were left out: %s"
              % (len(mismatched), ", ".join(mismatched[:5])))

    matdb.apply(replaced, "refine")

    # Only now: a datablock may still have been in use while the loop ran.
    for name in retired:
        old = bpy.data.meshes.get(name)
        if old is not None and old.users == 0:
            bpy.data.meshes.remove(old)

    context.view_layer.update()
    return replaced
