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
import re
import struct
from dataclasses import dataclass, field
from typing import List

import bpy
import numpy as np
from mathutils import Matrix

from . import appearance, matdb, matching, progress, swmesh
from . import diff as diff_mod
from . import imports as imports_mod
from . import weld as weld_mod
from .. import empties as empties_mod
from .. import geometry_lock, material_lock

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
# The tags that describe the mesh of a part, and not its place in the
# assembly. A part that keeps its geometry (geometry_lock.py) keeps these
# too, so they still say what its mesh is, and the next update still sees
# that its geometry differs from the export.
_MESH_TAGS = (_TAG_GEOMETRY, _TAG_TOLERANCE, "STEP_materials")


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
    identity = _material_identity(spec)
    mat = None
    # A texture is mapped in Blender units, so a material built at another
    # Unit Scale maps it at the wrong size. One with no scale on it was
    # built in meters.
    scale = _scale_tag(unit_scale)
    for cand in bpy.data.materials:
        if (cand.name == base or cand.name.startswith(base + ".")) \
                and cand.get("SWMESH_appearance_hash") == identity \
                and cand.get("SWMESH_unit_scale", "") == scale:
            mat = cand
            break
    if mat is None:
        mat = bpy.data.materials.new(base)
        appearance.build(mat, parsed, tuple(spec.rgba), unit_scale)
        if scale:
            mat["SWMESH_unit_scale"] = scale
    mat["SWMESH_rgba"] = list(spec.rgba)
    mat["SWMESH_appearance_hash"] = identity
    mat["SWMESH_appearance_name"] = base
    if spec.appearance_json:
        mat["SWMESH_appearance"] = spec.appearance_json
    return mat


def _material_identity(spec):
    """What makes one material of the export the same as another: its
    appearance, or its color and finish when it has none. Its number in
    the export's table does not: that is the order the walk found it."""
    return appearance.digest(spec.appearance_json or "%r|%r|%r|%r" % (
        tuple(round(c, 4) for c in spec.rgba), round(spec.roughness, 4),
        round(spec.metallic, 4), spec.texture))


def _build_mesh(definition, materials, unit_scale):
    """One mesh datablock from one definition. foreach_set moves the whole
    buffer in one call: looping in Python over a hundred thousand vertices
    is the difference between instant and unusable."""
    # Every CAD face arrives with its own copy of the points on its edges.
    # They are joined here, before the mesh exists, the same as the STEP
    # route joins them, so a part from SolidWorks is one connected surface
    # and not a pile of faces. See weld.py.
    w = weld_mod.weld(definition.positions, definition.triangles,
                      definition.normals, definition.uvs,
                      getattr(definition, "body_starts", None))

    me = bpy.data.meshes.new(definition.name or "SWMesh")
    me.vertices.add(len(w.positions))
    me.vertices.foreach_set(
        "co", (w.positions.astype(np.float64) * unit_scale).ravel())

    n = len(w.triangles)
    me.loops.add(n * 3)
    me.polygons.add(n)
    me.loops.foreach_set("vertex_index", w.triangles.ravel().astype(np.int32))
    me.polygons.foreach_set("loop_start", np.arange(0, n * 3, 3, dtype=np.int32))
    me.polygons.foreach_set("loop_total", np.full(n, 3, dtype=np.int32))

    if definition.uvs is not None:
        # UVs are per corner in Blender and per point in the file. Each
        # corner takes the UV of the point it came from, so a joined point
        # keeps a UV for each face around it.
        uv = me.uv_layers.new(name="UVMap")
        uvs = np.frombuffer(definition.uvs, dtype=np.float32).reshape(-1, 2)
        uv.data.foreach_set("uv", uvs[w.corners].ravel())

    if definition.triangle_materials is not None and materials:
        # Only the materials this definition uses, so a part does not
        # carry every appearance in the assembly as an empty slot.
        top = len(materials) - 1
        per_triangle = np.clip(np.frombuffer(
            definition.triangle_materials, dtype=np.int32)[w.kept], 0, top)
        used = np.unique(per_triangle)
        for scene_index in used:
            me.materials.append(materials[int(scene_index)])
        me.polygons.foreach_set(
            "material_index",
            np.searchsorted(used, per_triangle).astype(np.int32))

    me.update(calc_edges=True)
    _mark_edges(me, w)

    if definition.normals is not None:
        # Custom normals last: they are invalidated by geometry edits, and
        # they are what makes a coarse tessellation still read as a smooth
        # surface. Each corner takes the normal of the point it came from.
        normals = np.frombuffer(definition.normals, dtype=np.float32).reshape(-1, 3)
        try:
            me.normals_split_custom_set(normals[w.corners])
        except (RuntimeError, ValueError, TypeError):
            me.shade_smooth()
    me.validate(verbose=False)
    return me


def _mark_edges(me, w):
    """Puts the CAD edges the weld found back on the mesh: sharp where two
    faces do not meet smoothly, a UV seam where their UVs do not agree."""
    if not len(w.sharp) and not len(w.seams):
        return
    ends = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", ends)
    keys = weld_mod.edge_keys(ends[0::2], ends[1::2], len(me.vertices))
    if len(w.sharp):
        me.edges.foreach_set("use_edge_sharp", np.isin(keys, w.sharp))
    if len(w.seams):
        me.edges.foreach_set("use_seam", np.isin(keys, w.seams))


def scene_unit_scale(context):
    """Blender units per meter in the scene: 1 / Unit Scale.

    The .swmesh is in meters. The rig build, pose sync, matching and the
    STEP import all convert with this value (rig_build._unit_scale). The
    direct link placed its parts at one Blender unit per meter instead, so
    in a millimeter scene pose sync pushed every part a thousand times
    further out without scaling its mesh, and the rig was built there."""
    try:
        scale = float(context.scene.unit_settings.scale_length)
    except (AttributeError, TypeError):
        return 1.0
    return 1.0 / scale if scale > 0.0 else 1.0


def cad_quality(scene):
    """The quality fields of a request to the CAD application, from the
    scene's Mesh Quality settings.

    The Custom distance is a length property, so Blender holds it in scene
    units. The CAD application takes meters: in a millimeter scene, a
    distance of 0.8 mm went to it as 0.8 m. The presets are meters
    already."""
    from .. import quality as quality_mod
    spec = quality_mod.spec_of(scene.stepper)
    if spec["mode"] == "physical" and spec.get("quality") not in quality_mod.PRESETS:
        try:
            scale = float(scene.unit_settings.scale_length)
        except (AttributeError, TypeError):
            scale = 1.0
        if scale > 0.0:
            spec["lin_m"] = spec["lin_m"] * scale
    return quality_mod.cad_request(spec)


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
_TAG_ROLE = diff_mod.TAG_ROLE  # what a collection of an import is for

# A direct send puts its assembly collection in a top collection, and the
# rig build puts the rig collection next to it. The rig is then easy to
# find, and it does not stand among all the other collections of the
# scene (Oscar, 2026-09-23). The top collection has the tag of the import
# and this role. It is not a part of the assembly: an update does not
# take it for the assembly collection, and a send does not remove it with
# the assembly. Objects and collections the user puts in it are the
# user's.
_ROLE_TOP = "top"
# The names of one import (Oscar, 2026-09-24). The stem is
# "<assembly>_<configuration>", and it names the top collection itself.
# In it are "<stem>_Parts" and the rig, "<stem>_Rig". Two configurations
# of one assembly then stand side by side with names of their own: a
# collection name is unique in a file, so an assembly collection named
# after the assembly alone got ".001" in the second. Before 1.2 the top
# was "<stem>_Top_Level" and the assembly collection the stem.
_PARTS_SUFFIX = "_Parts"
_LEGACY_TOP_SUFFIX = "_Top_Level"
_TAG_DOCUMENT = imports_mod.TAG_DOCUMENT
_TAG_CONFIGURATION = imports_mod.TAG_CONFIGURATION
# Blender cuts a longer name at this many bytes.
_NAME_BYTES = 63

# ".body003" at the end of a path: a part that came in as one object per
# solid body. The CAD application knows the part, not the body.
_BODY_SUFFIX = re.compile(r"\.body\d+$")


def cad_paths(objects):
    """Where each of these parts sits in the CAD assembly, once each, or
    None when one of them does not say.

    This is what names ONE placement. A component id does not: every part
    of a rigid subassembly travels under the SUBASSEMBLY's id, so asking by
    id for one part asks the CAD application for every part of the branch
    it sits on (Conveyor12k-A00: one part selected, 78 tessellated and 1509 sent
    back, Oscar, 2026-09-17). A caller that cannot name them all sends none
    of them and lets the ids answer, because a partial list would quietly
    leave parts out.
    """
    out, seen = [], set()
    for obj in objects or []:
        path = obj.get(_TAG_PATH)
        if not path:
            return None
        path = _BODY_SUFFIX.sub("", str(path))
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _own_collections(stem, scope):
    """Every collection a previous import of this file made in the scene
    (of any file, with stem None), leaves first, so each is empty of
    children by the time it is removed. The top collection is not one of
    them: it can hold the user's work, and remove_previous decides about
    it apart."""
    mine = [c for c in scope.collections
            if c.get(_TAG_FILE) is not None and c.get(_TAG_ROLE) != _ROLE_TOP
            and (stem is None or c.get(_TAG_FILE) == stem)]
    depth = {}

    def d(c):
        if c.name in depth:
            return depth[c.name]
        parents = [p for p in bpy.data.collections if c.name in p.children]
        depth[c.name] = 1 + max([d(p) for p in parents if p in mine], default=0)
        return depth[c.name]

    return sorted(mine, key=lambda c: -d(c))


class _Scope:
    """The collections and objects of one scene.

    A send or an update works in the scene it was asked for. Another scene
    of the file can hold objects with the same tags: a Full Copy of the
    scene copies every part with them. Those are not this scene's to change.
    Read as this scene's parts, each part had two identities and the update
    deleted both, and a send into one scene deleted the send that stood in
    every other.

    `aside` holds the parts another add-on keeps out of the scene, in a
    collection that no scene holds and a fake user keeps in the file: the
    part a new object was made from, kept to make it again. They are that
    add-on's. A send deleted them, which left the add-on with nothing to
    make its object from. An update may still pair them, so a part kept
    aside is not built again beside what was made from it, but it does not
    move them into the import or delete them. A part kept aside and also
    shown in the scene is in `aside` and not in `objects`."""

    def __init__(self, scene):
        self.collections = []
        seen = set()
        stack = [scene.collection]
        while stack:
            for child in stack.pop().children:
                key = child.as_pointer()
                if key not in seen:
                    seen.add(key)
                    self.collections.append(child)
                    stack.append(child)
        self._collections = seen
        aside = {}
        for col in bpy.data.collections:
            if col.use_fake_user and col.as_pointer() not in seen:
                for obj in col.all_objects:
                    aside[obj.as_pointer()] = obj
        self._aside = set(aside)
        self.aside = list(aside.values())
        self.objects = [o for o in scene.objects
                        if o.as_pointer() not in self._aside]

    def has(self, collection):
        return collection.as_pointer() in self._collections

    def is_aside(self, obj):
        try:
            return obj.as_pointer() in self._aside
        except ReferenceError:
            return False


def _fit_name(stem, suffix=""):
    """The stem with `suffix` on the end, as a collection name.

    The stem is made short as _short_name does. When its characters take
    more bytes than the name has room for, it is made shorter again, so
    Blender does not cut the suffix off the end."""
    base = _short_name(stem)
    room = _NAME_BYTES - len(suffix.encode("utf-8"))
    if len(base.encode("utf-8")) > room:
        keep = len(base) // 2
        while keep > 1 and len((stem[:keep] + "_" + stem[-keep:])
                               .encode("utf-8")) > room:
            keep -= 1
        base = stem[:keep] + "_" + stem[-keep:]
    return base + suffix


def _top_name(stem):
    """The name of the top collection of the import `stem`: the stem."""
    return _fit_name(stem)


def _parts_name(stem):
    """The name of the collection that holds the parts of `stem`."""
    return _fit_name(stem, _PARTS_SUFFIX)


def _default_names(stem, top):
    """The names a send gives the top collection (`top` True) or the parts
    collection of `stem`, now and before 1.2. A name that is one of these
    is the send's, and a rename takes it along. Any other name is one the
    user gave."""
    if top:
        return (_top_name(stem), _fit_name(stem, _LEGACY_TOP_SUFFIX))
    return (_parts_name(stem), _short_name(stem))


def _is_default(name, stem, top):
    for default in _default_names(stem, top):
        if name == default or name.startswith(default + "."):
            return True
    return False


def _mark(col, document, configuration):
    """Writes on a collection of an import which document and which
    configuration it holds. An older add-in says neither, and the
    collection keeps what it has."""
    if document:
        col[_TAG_DOCUMENT] = str(document)
    if configuration:
        col[_TAG_CONFIGURATION] = str(configuration)


def _tops(scope):
    """stem -> the top collection of that import in this scene."""
    out = {}
    for col in scope.collections:
        try:
            if col.get(_TAG_ROLE) == _ROLE_TOP and col.get(_TAG_FILE):
                out.setdefault(col[_TAG_FILE], col)
        except ReferenceError:
            continue
    return out


def _new_top(stem):
    col = bpy.data.collections.new(_top_name(stem))
    col[_TAG_FILE] = stem
    col[_TAG_ROLE] = _ROLE_TOP
    return col


def _top_collection(stem, scope, destination, document=None,
                    configuration=None):
    """The top collection for a send of `stem`. The scene's own is used
    again, where it is and with what is in it: the user can have moved it
    into a collection of their own, or put work of their own in it.
    Otherwise a new one goes in `destination`."""
    top = _tops(scope).get(stem)
    if top is None:
        top = _new_top(stem)
        destination.children.link(top)
    _mark(top, document, configuration)
    return top


def _wrap(root, stem, scope, scene_collection):
    """Puts the assembly collection of an import from before the top
    collection into a new top collection, in the place the assembly
    collection has now. The rig build then moves the rig collection out
    of the assembly collection and next to it (rig_build).

    An import that has a top collection keeps its layout, also when the
    user moved the assembly collection out of it. Returns the new top
    collection, or None."""
    if _tops(scope).get(stem) is not None:
        return None
    holders = []
    for col in [scene_collection] + scope.collections:
        try:
            if root.name in col.children:
                holders.append(col)
        except ReferenceError:
            continue
    top = _new_top(stem)
    for holder in holders or [scene_collection]:
        if root.name in holder.children:
            holder.children.unlink(root)
        holder.children.link(top)
    top.children.link(root)
    print("[CADLink native] %s is now inside %s" % (root.name, top.name))
    return top


def _renamed(root, stem, scope):
    """Puts the new name of the import on everything the import that is
    standing tagged with the old one: the document was renamed, or an
    import from before 1.2 becomes the import of one configuration.

    The collections take the new name when they still have the name a send
    gave them. A name the user gave stays. The rig of the import takes the
    new name too, and says it drives the import by its new name, or the
    next relink would leave its parts off it."""
    was = root.get(_TAG_FILE)
    if not was or was == stem:
        return
    for coll in scope.collections:
        if coll.get(_TAG_FILE) != was:
            continue
        coll[_TAG_FILE] = stem
        role = coll.get(_TAG_ROLE)
        if role == _ROLE_TOP:
            if _is_default(coll.name, was, top=True):
                coll.name = _top_name(stem)
        elif role in ("flat", "hierarchy"):
            if _is_default(coll.name, was, top=False):
                coll.name = _parts_name(stem)
        elif coll.name.startswith(was + "."):
            # The hidden ".components" of the instancing mode.
            coll.name = stem + coll.name[len(was):]
    for obj in scope.objects + scope.aside:
        if obj.get(_TAG_FILE) == was:
            obj[_TAG_FILE] = stem
    _rename_rigs(was, stem, scope)
    print("[CADLink native] the import %s is now %s" % (was, stem))


def _rename_rigs(was, stem, scope):
    """The rigs of the import `was` drive the import `stem` from now on."""
    for obj in scope.objects:
        try:
            if obj.type != "ARMATURE" or not obj.get("RIG_rig") \
                    or was not in imports_mod.rig_stems(obj):
                continue
        except ReferenceError:
            continue
        obj[imports_mod.TAG_IMPORT] = stem
        old = was + "_Rig"
        if obj.name == old or obj.name.startswith(old + "."):
            obj.name = stem + "_Rig"
            if obj.data is not None and obj.data.name.startswith(old):
                obj.data.name = stem + "_Rig"
        for coll in obj.users_collection:
            if coll.name == old or coll.name.startswith(old + "."):
                coll.name = stem + "_Rig"


def adopt_legacy(stem, document, configuration, scope):
    """The import of `document` a CADder before 1.2 made, taken over as the
    import `stem` of `configuration`. Returns its parts collection, or None.

    Before 1.2 a send had no configuration, and its stem was the name of
    the document alone. The first send of that document from 1.2 on takes
    it over, so its top collection, the work the user put in it, and its
    locked materials carry on, and no second copy of the assembly stands
    beside the new one. An import that already has a configuration is
    never taken: it is one configuration, and this is another."""
    if not document or not configuration:
        return None
    base = os.path.splitext(os.path.basename(str(document)))[0]
    if not base or base == stem:
        return None
    if any(c.get(_TAG_FILE) == stem for c in scope.collections):
        return None
    for coll in scope.collections:
        if (coll.get(_TAG_FILE) == base
                and coll.get(_TAG_ROLE) in ("flat", "hierarchy")
                and not coll.get(_TAG_CONFIGURATION)):
            _renamed(coll, stem, scope)
            for other in scope.collections:
                if other.get(_TAG_FILE) == stem:
                    _mark(other, document, configuration)
            return coll
    return None


def standing(stem, scene, scope, configuration=None):
    """The import in the scene that an export is an update OF, or None.
    `scene` is the export, `scope` the Blender scene (a _Scope).

    By name, when a name matches. But a document gets RENAMED, most often
    when a revision is cut and a letter goes on the end, and the scene
    still holds that same assembly (Oscar, 2026-09-17). An update that did
    not find it would build the whole thing again and throw away
    everything done in Blender since, which is the one thing an update
    exists to avoid.

    So otherwise it is the import that holds most of what this export
    holds, by the occurrence paths, which are the names of the components
    INSIDE the assembly and do not change when the assembly is renamed. It
    then takes the new name.

    Never the import of another configuration: it holds the same paths,
    and it is not this import. An import from before 1.2 says no
    configuration, and it can be this one.
    """
    roots = [c for c in scope.collections
             if c.get(_TAG_FILE) is not None
             and c.get("SWMESH_role") in ("flat", "hierarchy")]
    for coll in roots:
        if coll.get(_TAG_FILE) == stem:
            return coll
    roots = [c for c in roots
             if not c.get(_TAG_CONFIGURATION)
             or c.get(_TAG_CONFIGURATION) == configuration]

    known = {i.path for i in scene.instances if i.path}
    tag = _TAG_PATH
    if not known:
        known = {i.component_id for i in scene.instances if i.component_id}
        tag = _TAG_COMPONENT
    if not known:
        return None

    best, score = None, 0
    for coll in roots:
        # Parts only. The prototypes and the subassembly empties have no
        # path any export holds, and counted in, they kept an assembly of
        # mostly unique parts from ever reaching half.
        held = [o for o in coll.all_objects
                if o.get(_TAG_FILE) is not None and diff_mod.is_part(o)]
        if not held:
            continue
        hit = sum(1 for o in held if (o.get(tag) or "") in known)
        # Half of what is standing has to be in the export. A revision
        # changes some of an assembly, never most of it, and a scene that
        # holds a DIFFERENT assembly must not be adopted by this one.
        if hit > score and hit * 2 >= len(held):
            best, score = coll, hit
    if best is None:
        return None
    _renamed(best, stem, scope)
    return best


def remove_previous(stem=None, scene_collection=None, scene=None,
                    keep_top=None):
    """Clears the previous native import of `stem` so a send replaces it
    rather than stacking a second copy beside it. Meshes go too, when no
    other import uses them: an orphaned datablock of a million triangles
    is invisible in the outliner and very much present in the file. None
    takes every native import. Only in `scene` (the current one by
    default), and never a part another add-on keeps aside (see _Scope).

    A send removes only its own import (Oscar, 2026-09-24). The other
    configurations of the assembly, and other assemblies, stay, each with
    its own rig. Up to 1.1 a send removed every import in the scene: a
    different assembly otherwise stacked a dead rig beside the live one on
    every send (live 2026-09-14). Each import now has a rig of its own,
    named after its stem, and a rig goes with the last of its parts
    (below). ci/native_configurations_smoke.py sends two configurations
    and a second assembly in turn, and counts the rigs.

    A top collection is not removed with its import. It goes only when
    nothing is left in it, and never the one of `keep_top`, the stem the
    send is about to build again: the send uses it again, in the place the
    user gave it."""
    removed = 0
    scene = scene or bpy.context.scene
    scene_collection = scene_collection or scene.collection
    scope = _Scope(scene)
    colls = _own_collections(stem, scope)
    tops = _tops(scope)
    doomed_tops = [top for name, top in tops.items()
                   if stem is None or name == stem]
    objects = [o for o in scope.objects
               if o.get(_TAG_FILE) is not None and (stem is None or o.get(_TAG_FILE) == stem)]
    for coll in colls:
        for o in coll.objects:
            if o not in objects and not scope.is_aside(o):
                objects.append(o)
    # A part kept aside and shown in the scene is in an import collection
    # too. It leaves the collection with the import, and stays kept.
    for obj in scope.aside:
        for coll in colls:
            try:
                if obj.name in coll.objects:
                    coll.objects.unlink(obj)
            except (ReferenceError, RuntimeError):
                continue
    if not colls and not objects and not doomed_tops:
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
    # A build before the top collection parked the rig collection inside
    # the one holding the parts it drives, which is this one. Removing it
    # with the rig still inside cut the rig (and the bone widgets under
    # it) out of the scene while keeping it in the file, so the next build
    # found no rig collection in the scene and made a numbered copy. So
    # children move up first. A collection of the user's own inside the
    # assembly (the rig of one part, say) goes into the top collection of
    # the same import, so a re-send keeps it inside the model. An import
    # from before the top collection has none, and the scene root takes
    # the children.
    for coll in colls:
        try:
            home = tops.get(coll.get(_TAG_FILE)) or scene_collection
            _rehome_children(coll, home)
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
    # Last, when the rigs have gone from them too. A top collection that
    # still holds something (a locked rig, a collection or an object of
    # the user's) stays, also the one of a different assembly.
    for top in doomed_tops:
        try:
            if top.get(_TAG_FILE) == keep_top or top.objects or top.children:
                continue
            bpy.data.collections.remove(top)
        except ReferenceError:
            continue
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


def _short_name(name):
    """The name a collection of the import gets for `name`."""
    if len(name) > 50:
        name = name[:25] + "_" + name[-25:]
    return name


def _collection(name, stem, role, parent):
    """A collection that says which import made it and what for, so a
    re-send can take its own away and leave the user's alone."""
    col = bpy.data.collections.new(_short_name(name))
    col[_TAG_FILE] = stem
    col["SWMESH_role"] = role
    parent.children.link(col)
    return col


def _root_collection(stem, role, parent, document=None, configuration=None):
    """The collection that holds the parts of the import `stem`,
    "<stem>_Parts"."""
    col = bpy.data.collections.new(_parts_name(stem))
    col[_TAG_FILE] = stem
    col["SWMESH_role"] = role
    _mark(col, document, configuration)
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
                 unit_scale, hierarchy, material_prefix, document=None,
                 configuration=None):
        self.context = context
        self.scene = scene
        self.manifest = manifest
        self.stem = stem
        self.root = root
        self.frame = frame
        self.unit_scale = unit_scale
        self.hierarchy = hierarchy
        self.material_prefix = material_prefix
        self.document = document
        self.configuration = configuration
        # geometry hash -> a mesh another configuration of this document
        # already has, read when the first mesh is needed.
        self._shared = None

        self.group_of = _group_of(manifest)
        self.persistent_of = _persistent_of(manifest)
        self.by_id, self.by_path = _paths(manifest)
        self.branches = _branches(scene, self.by_path, unit_scale)
        self.definitions = {d.id: d for d in scene.definitions}
        self.material_ids = [_material_identity(m) for m in scene.materials]

        self._materials = None
        self._meshes = {}
        # definition id of THIS export -> the collection of its prototype.
        self.prototypes = {}
        self.components_collection = None
        self.tree_cols = {"": root}
        self.empties = {}
        # The empties this run made, which are sized once the parts are in.
        # An empty an earlier send made keeps the size it has, which may be
        # one the user chose.
        self.new_empties = []
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
            signature = self.definition_hash(definition)
            me = self.shared_meshes().get(signature)
            if me is None:
                me = _build_mesh(definition, self.materials, self.unit_scale)
                me[_TAG_GEOMETRY] = signature
        self._meshes[definition_id] = me
        return me

    def shared_meshes(self):
        """geometry hash -> the mesh of a part that another configuration
        of this document has in the scene.

        A part that two configurations hold the same way is one mesh in
        Blender, as two placements of it in one configuration are (Oscar,
        2026-09-24). The hash covers the triangles, the materials of each
        triangle and the Unit Scale, so a part with another appearance in
        the other configuration, or another shape, gets a mesh of its own.
        The mesh says which geometry it was built from. Rebuild from CAD
        makes new meshes that say nothing, and so did a send before 1.2,
        so neither is taken."""
        if self._shared is not None:
            return self._shared
        self._shared = {}
        if not self.document:
            return self._shared
        for obj in _Scope(self.context.scene).objects:
            try:
                if obj.get(_TAG_FILE) in (None, self.stem) \
                        or obj.get(_TAG_DOCUMENT) != self.document:
                    continue
                for holder in _mesh_holders(obj):
                    me = holder.data
                    signature = me.get(_TAG_GEOMETRY) if me is not None else None
                    if signature:
                        self._shared.setdefault(signature, me)
            except (ReferenceError, AttributeError):
                continue
        return self._shared

    def build_prototypes(self):
        """The instancing mode's hidden prototypes: one object per
        definition, in a collection of its own."""
        if self.hierarchy != "COLLECTION_INSTANCES":
            return
        for definition in self.scene.definitions:
            self.prototype(definition.id)

    def prototype(self, definition_id):
        """The collection that holds the prototype of one definition of
        this export. One is built when the scene has none with the same
        geometry. None when the file holds no geometry for it."""
        if definition_id in self.prototypes:
            return self.prototypes[definition_id]
        me = self.mesh(definition_id)
        if me is None:
            return None
        definition = self.definitions[definition_id]
        part_col = _collection(
            definition.name or ("definition %d" % definition.id),
            self.stem, "part", self.components())
        proto = bpy.data.objects.new(definition.name or "part", me)
        proto[_TAG_FILE] = self.stem
        proto[_TAG_DEFINITION] = definition.id
        proto[_TAG_TOLERANCE] = self.scene.tolerance
        proto[_TAG_GEOMETRY] = self.definition_hash(definition)
        proto["SWMESH_prototype"] = True
        _material_names(proto)
        part_col.objects.link(proto)
        self.prototypes[definition_id] = part_col
        return part_col

    def components(self):
        """The hidden collection of the prototypes, made when needed."""
        if self.components_collection is None:
            # Inside the assembly's own collection, so the scene shows one
            # collection per send and not a hidden second one beside it.
            self.components_collection = _collection(
                self.stem + ".components", self.stem, "components", self.root)
            _exclude(self.context, self.components_collection)
        return self.components_collection

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
        # A branch, not a part. It gets no component id, even for a rigid
        # subassembly: its parts carry that. See diff.is_part.
        emp[diff_mod.TAG_ROLE] = "node"
        branch = self.branches.get(sw_path)
        if branch is not None and branch.transform is not None:
            emp.matrix_world = self.frame @ branch.transform
        self.root.objects.link(emp)
        if parent is not None:
            emp.parent = parent
            emp.matrix_parent_inverse = parent.matrix_world.inverted()
        self.empties[sw_path] = emp
        self.new_empties.append(emp)
        return emp

    def adopt_tree(self, scope):
        """Takes over the collections and empties a previous import of this
        file left in this scene, so an update reuses them instead of
        building a second tree beside the first. Anything the user put
        inside one of them stays where it is."""
        # The prototypes, by their geometry. The export numbers its
        # definitions in walk order, so the numbers shift when a part is
        # added in front of another, and a prototype found by the old
        # number drew a different part. A prototype of an older import has
        # no geometry tag, and the instances that draw it say what it is.
        by_geometry = {}
        for col in scope.collections:
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
                    if obj.get("SWMESH_prototype") and obj.get(_TAG_GEOMETRY):
                        by_geometry.setdefault(obj[_TAG_GEOMETRY], col)
        for obj in scope.objects:
            if obj.get(_TAG_FILE) != self.stem or obj.type != "EMPTY":
                continue
            if diff_mod.is_part(obj) or obj.get("SWMESH_prototype") \
                    or obj.get(_TAG_GROUP) is not None:
                continue
            path = obj.get(_TAG_PATH)
            if not path:
                continue
            self.empties[path] = obj
            # An empty of an older build: say what it is, and take off the
            # component id that made it look like a part.
            obj[diff_mod.TAG_ROLE] = "node"
            if _TAG_COMPONENT in obj.keys():
                del obj[_TAG_COMPONENT]
        for obj in scope.objects:
            col = getattr(obj, "instance_collection", None)
            if obj.get(_TAG_FILE) == self.stem and obj.get(_TAG_GEOMETRY) \
                    and col is not None and col.get("SWMESH_role") == "part":
                by_geometry.setdefault(obj[_TAG_GEOMETRY], col)
        for definition in self.scene.definitions:
            signature = self.definition_hash(definition)
            col = by_geometry.get(signature)
            if col is None and _in_meters(self.unit_scale):
                col = by_geometry.get(_legacy_definition_hash(definition))
            if col is None:
                continue
            self.prototypes[definition.id] = col
            for proto in col.objects:
                if proto.get("SWMESH_prototype"):
                    proto[_TAG_DEFINITION] = definition.id
                    proto[_TAG_GEOMETRY] = signature

    # ── the parts themselves ────────────────────────────────────────────

    def create(self, inst):
        """One new object for one placement, tagged and placed. None when
        the file holds no geometry for it."""
        name = inst.name or inst.component_id
        if self.hierarchy == "COLLECTION_INSTANCES":
            # Only the prototype holds a mesh. One built here for the
            # instance too was left with no users.
            col = self.prototype(inst.definition_id)
            if col is None:
                return None
            obj = bpy.data.objects.new(name, None)
            obj.instance_type = "COLLECTION"
            obj.instance_collection = col
            obj.empty_display_size = 0.01
        else:
            me = self.mesh(inst.definition_id)
            if me is None:
                return None
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
        # Which document and configuration the part is of. A request back
        # to the CAD application names both, so it answers for this
        # configuration and not for the one that is active there.
        if self.document:
            obj[_TAG_DOCUMENT] = str(self.document)
        if self.configuration:
            obj[_TAG_CONFIGURATION] = str(self.configuration)
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

    def definition_hash(self, definition):
        # The mesh is built at the scene's Unit Scale, so the scale is part
        # of what the mesh is. Blender scales nothing when the Unit Scale
        # changes, and a part kept across that change stood in the right
        # place a thousand times too small.
        return _definition_hash(definition, self.material_ids) \
            + _scale_tag(self.unit_scale)

    def geometry_hash(self, inst):
        definition = self.definitions.get(inst.definition_id)
        return None if definition is None else self.definition_hash(definition)

    def same_geometry(self, signature, inst):
        """True when the geometry tag of an object says this placement's
        geometry is what it has. A tag of an older send is read the way
        that send wrote it, so its first update does not read every part
        as re-tessellated and replace the meshes."""
        definition = self.definitions.get(inst.definition_id)
        if definition is None or not signature:
            return False
        if str(signature).startswith(_HASH_VERSION):
            return signature == self.definition_hash(definition)
        # An older send built every mesh in meters, whatever the scene.
        return _in_meters(self.unit_scale) \
            and signature == _legacy_definition_hash(definition)

    def pose(self, obj, inst):
        obj.matrix_world = self.frame @ _matrix(inst.transform, self.unit_scale)

    def on_pose(self, obj, transform):
        """True when the object stands where the CAD pose it was tagged
        with puts it."""
        if not transform or len(transform) != 16:
            return False
        try:
            want = self.frame @ _matrix(transform, self.unit_scale)
            return (obj.matrix_world.translation - want.translation).length                 <= 1e-5 * max(1.0, self.unit_scale)
        except (ReferenceError, ValueError):
            return False

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
        empty. A collection of the user's own keeps the object, and so
        does the top collection: what is in it the user put there."""
        for col in list(obj.users_collection):
            if col.get(_TAG_FILE) == self.stem \
                    and col.get(_TAG_ROLE) != _ROLE_TOP:
                col.objects.unlink(obj)
        parent = obj.parent
        if parent is not None and parent.get(_TAG_FILE) == self.stem \
                and parent.type == "EMPTY":
            world = obj.matrix_world.copy()
            obj.parent = None
            obj.matrix_world = world


# The hash of a part's geometry says which version of it it is. The first
# took the material numbers of the export, which shift when a color is
# added or removed anywhere in the assembly.
_HASH_VERSION = "v2:"


def _in_meters(unit_scale):
    """True at Unit Scale 1: one Blender unit is one meter."""
    return abs(float(unit_scale) - 1.0) <= 1e-9


def _scale_tag(unit_scale):
    """The Unit Scale a mesh or a material was built at, for its tag.
    Nothing at 1, so the tags of a meter scene stay what they were."""
    return "" if _in_meters(unit_scale) else "@%.9g" % float(unit_scale)


def _rescale_tag(obj, unit_scale):
    """Moves an object's geometry tag to the Unit Scale its mesh was just
    built at. The triangles of the export stay what the tag says, so a
    refresh keeps the mesh a rebuild put there."""
    signature = obj.get(_TAG_GEOMETRY)
    if signature and str(signature).startswith(_HASH_VERSION):
        obj[_TAG_GEOMETRY] = str(signature).split("@", 1)[0] \
            + _scale_tag(unit_scale)


def _definition_hash(definition, material_ids):
    """A short, exact signature of one part's geometry, so an update can
    tell a part that was re-tessellated from one that was not and leave the
    mesh (and the work done on it in Blender) alone.

    `material_ids` gives the identity of each material of the export by
    its number (_material_identity). The export numbers its materials in
    the order the walk finds them, so a part that did not change came back
    with other numbers when a color was added in front of its own. The
    hash takes the material of each triangle by what it is, in the order
    the triangles first use them."""
    digest = hashlib.blake2b(digest_size=8)
    digest.update(struct.pack("<II", definition.vertex_count,
                              definition.triangle_count))
    for block in (definition.positions, definition.triangles):
        if block is not None:
            digest.update(memoryview(block).cast("B"))
    if definition.triangle_materials is not None \
            and len(definition.triangle_materials):
        numbers = np.frombuffer(definition.triangle_materials, dtype=np.int32)
        if material_ids:
            numbers = np.clip(numbers, 0, len(material_ids) - 1)
        used, first, per_triangle = np.unique(
            numbers, return_index=True, return_inverse=True)
        order = np.argsort(first)
        label = np.empty(len(order), dtype=np.int32)
        label[order] = np.arange(len(order), dtype=np.int32)
        digest.update(label[per_triangle.ravel()].astype("<i4").tobytes())
        for number in used[order]:
            number = int(number)
            identity = (material_ids[number]
                        if 0 <= number < len(material_ids) else "#%d" % number)
            digest.update(identity.encode("utf-8") + b"\0")
    return _HASH_VERSION + digest.hexdigest()


def _legacy_definition_hash(definition):
    """The hash of an older send, which took the material numbers as they
    were. Only to recognize the parts of such a send on their first
    update."""
    digest = hashlib.blake2b(digest_size=8)
    digest.update(struct.pack("<II", definition.vertex_count,
                              definition.triangle_count))
    for block in (definition.positions, definition.triangles,
                  definition.triangle_materials):
        if block is not None:
            digest.update(memoryview(block).cast("B"))
    return digest.hexdigest()


def build(context, path, manifest=None, collection_name=None,
          unit_scale=None, material_prefix="SW ", up_as="ZPOS",
          hierarchy="FLAT", report_to=None, document=None,
          configuration=None):
    """Reads a .swmesh and builds the scene. Returns (objects, MatchReport).

    The report is what ties this into the existing pipeline: every entry is
    exact, so pose sync, the rig build and relink behave as though matching
    had run and got everything right, which, here, it has. The manifest
    gives the component-to-group map that re-linking attaches by, and the
    assembly paths the tree modes are built from.

    up_as is the CAD axis that becomes Blender's up (the STEP importer's
    option, same spelling): the geometry is turned and the report's frame
    says so, so the rig lands on it. hierarchy is the STEP importer's
    hierarchy_types. The parts go in one collection, "<file>_Parts", in
    the top collection, "<file>", and the rig build puts the rig
    collection, "<file>_Rig", next to it. The file is named after the
    document and the configuration.

    `document` is the path of the CAD document and `configuration` the
    configuration the file holds, as the send says them. Both go on the
    parts, so a request back to the CAD application names them.

    unit_scale is Blender units per meter. None takes the scene's
    (scene_unit_scale), which is what every caller wants.

    This REPLACES what was there. To change only what changed, keeping the
    rest of the scene, see update()."""
    if unit_scale is None:
        unit_scale = scene_unit_scale(context)
    scene = swmesh.load(path)
    frame_rows = up_frame(up_as)
    stem = os.path.splitext(os.path.basename(path))[0]
    if hierarchy not in HIERARCHIES:
        hierarchy = "FLAT"

    # An import from before 1.2 of this document becomes this one, so it is
    # replaced below and not left standing beside it.
    adopt_legacy(stem, document, configuration, _Scope(context.scene))
    # A send replaces every object of its import. A locked part is found
    # again by its place in the assembly: the new object is locked and
    # gets the materials back. A part that keeps its geometry gets its old
    # mesh back in the same way, so its mesh stays in the file until then.
    previous = [o for o in context.scene.objects
                if o.get(_TAG_FILE) == stem]
    locks = material_lock.take(previous)
    shapes = geometry_lock.take(previous, tags=_MESH_TAGS)
    remove_previous(stem, context.scene.collection, context.scene,
                    keep_top=stem)
    # The top collection holds the assembly, and the rig next to it. A
    # re-send uses the one it made before, in the place it has now.
    destination = _top_collection(stem, _Scope(context.scene),
                                  context.scene.collection,
                                  document, configuration)
    # One collection for the parts. The shape of what is inside it is the
    # hierarchy option's business, not the name's (Oscar, 2026-09-16: a
    # send should simply put the assembly in a collection of its own).
    role = {"FLAT": "flat", "TREE": "hierarchy", "EMPTIES": "hierarchy",
            "COLLECTION_INSTANCES": "hierarchy"}[hierarchy]
    root = _root_collection(stem, role, destination, document, configuration)

    placer = _Placer(context, scene, manifest, stem, root,
                     Matrix([tuple(r) for r in frame_rows]), unit_scale,
                     hierarchy, material_prefix, document, configuration)
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
                                confidence="exact",
                                object_path=obj.get(_TAG_PATH)))

    # The geometry first: the old mesh carries the old materials, and the
    # materials of a locked part then go back on the mesh it has now.
    if geometry_lock.restore(shapes, among=objects) and hierarchy == "FLAT":
        _rename_flat_groups(placer)
    material_lock.restore(locks, among=objects)
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
    _fit_empties(placer, objects)
    return objects, report


def _rename_flat_groups(placer):
    """Names each collection of the flat tree after the mesh its parts
    use now.

    The flat tree has one collection per mesh, named after it. A part that
    keeps its geometry gets its old mesh back after the send placed it, in
    the collection of the new mesh. The old mesh was still in the file
    when the new one was named, so the new one, and its collection, had a
    ".001" name."""
    for col in list(placer.flat_groups.values()):
        try:
            names = {o.data.name for o in col.objects if o.data is not None}
        except ReferenceError:
            continue
        if len(names) != 1:
            continue
        want = _short_name(names.pop())
        if col.name != want and bpy.data.collections.get(want) is None:
            col.name = want


@dataclass
class UpdateReport:
    """What an update did, in the words the user reads."""

    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    moved: List[str] = field(default_factory=list)
    reshaped: List[str] = field(default_factory=list)
    kept: int = 0
    structural: bool = False
    # Copies of a part made in Blender, which the update left alone.
    copies: List[str] = field(default_factory=list)
    # Parts with new geometry in the export that kept their own, because
    # their geometry is locked (geometry_lock.py).
    locked: List[str] = field(default_factory=list)

    def describe(self):
        text = ("%d part(s) added, %d removed, %d moved, %d re-tessellated, "
                "%d unchanged" % (len(self.added), len(self.removed),
                                  len(self.moved), len(self.reshaped), self.kept))
        if self.locked:
            text += ", %d kept their locked geometry" % len(self.locked)
        return text


def update(context, path, manifest=None, unit_scale=None,
           material_prefix="SW ", up_as="ZPOS", hierarchy="FLAT",
           report_to=None, before_changes=None, document=None,
           configuration=None):
    """Brings the scene up to date with a new export, changing only what
    changed. Returns (objects, MatchReport, UpdateReport).

    `before_changes(stem)` runs once the import this export updates is
    found, and before any part moves. It does not run when there is none
    and the update falls back to a fresh import.

    A send replaces the import outright, which throws away everything done
    in Blender since. This compares the two assemblies part by part
    (rig/diff.py), and then:

      * a part that is still there and still the same shape keeps its
        object, its mesh, its materials and its modifiers, and is only
        moved and re-tagged,
      * a part that was re-tessellated or changed shape takes the new mesh
        on the SAME object, unless its geometry is locked
        (geometry_lock.py): it then keeps its mesh and only moves,
      * a part that is new is built and placed as a fresh import would,
      * a part that has gone is removed with its mesh.

    The rig is not touched here. What to do with it is a separate question
    and a separate answer: see rig_update.py.

    unit_scale, `document` and `configuration` are as for build()."""
    if unit_scale is None:
        unit_scale = scene_unit_scale(context)
    scene = swmesh.load(path)
    frame_rows = up_frame(up_as)
    stem = os.path.splitext(os.path.basename(path))[0]
    if hierarchy not in HIERARCHIES:
        hierarchy = "FLAT"
    said = report_to or progress.NONE

    scope = _Scope(context.scene)
    adopt_legacy(stem, document, configuration, scope)
    root = standing(stem, scene, scope, configuration)
    if root is None:
        # Nothing of this assembly is in the scene: an update of nothing is
        # an import.
        objects, report = build(
            context, path, manifest=manifest, unit_scale=unit_scale,
            material_prefix=material_prefix, up_as=up_as, hierarchy=hierarchy,
            report_to=report_to, document=document,
            configuration=configuration)
        out = UpdateReport(added=[o.name for o in objects], structural=True)
        return objects, report, out
    # A scene from before the top collection gets one around its
    # assembly. Nothing else about the layout changes.
    _wrap(root, stem, scope, context.scene.collection)
    _mark(root, document, configuration)
    top = _tops(_Scope(context.scene)).get(stem)
    if top is not None:
        _mark(top, document, configuration)

    placer = _Placer(context, scene, manifest, stem, root,
                     Matrix([tuple(r) for r in frame_rows]), unit_scale,
                     hierarchy, material_prefix, document, configuration)
    # A copy made in Blender is the user's object from here on. It keeps
    # its place, its mesh and its parent, and loses the tags that made it
    # a second identity for the part. This is done before the rig lets go
    # of its parts, so a copy on a bone stays on it.
    old, copies = diff_mod.split_copies(
        diff_mod.from_objects(scope.objects + scope.aside, stem),
        rank=lambda o: (not placer.on_pose(o.payload, o.transform),
                        len(o.name), o.name))
    disowned = []
    for occurrence in copies:
        try:
            _disown(occurrence.payload)
            disowned.append(occurrence.payload.name)
        except ReferenceError:
            continue
    if disowned:
        print("[CADLink native] %d object(s) are Blender copies of a part. "
              "They no longer have the tags of the assembly: %s"
              % (len(disowned), ", ".join(disowned[:5])))

    if before_changes is not None:
        before_changes(stem)
    # A prototype the scene lacks is built when a placement needs it.
    placer.adopt_tree(scope)

    new = diff_mod.from_scene_file(scene, manifest)
    changes = diff_mod.compare(old, new)
    # New geometry brings the CAD appearances with it. A locked part gets
    # its own materials back before the database runs.
    locks = material_lock.take()
    # The meshes that no new geometry replaces.
    held = geometry_lock.locked_meshes()

    said.stage("bringing the parts up to date", 20, 85,
               len(changes.pairs) + len(changes.added) + len(changes.removed))
    done = 0
    out = UpdateReport(structural=changes.structural, copies=disowned)
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
            reshaped = not placer.same_geometry(obj.get(_TAG_GEOMETRY), inst)
        except ReferenceError:
            continue
        # A part whose geometry is locked keeps its mesh, and the tags
        # that describe the mesh keep saying what it is. A tag of the new
        # geometry would make the next update read the part as up to date,
        # and it would not be rebuilt after the lock comes off.
        kept = reshaped and geometry_lock.keeps(obj, held)
        mesh_tags = geometry_lock.read_tags(obj, _MESH_TAGS) if kept else None
        placer.retag(obj, inst)
        if kept:
            geometry_lock.write_tags(obj, mesh_tags)
        if pair.moved:
            placer.pose(obj, inst)
            out.moved.append(obj.name)
        if kept:
            out.locked.append(obj.name)
        elif reshaped:
            _reshape(obj, placer, inst)
            out.reshaped.append(obj.name)
        if pair.old.path != pair.new.path and not scope.is_aside(obj):
            placer.unplace(obj)
            placer.place(obj, placer.path_of(inst))
        if not pair.moved and not reshaped:
            out.kept += 1
        objects.append(obj)
        report.matched.append(matching.MatchEntry(
            component_id=inst.component_id, object_name=obj.name,
            step=0, confidence="exact", object_path=obj.get(_TAG_PATH)))

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
            step=0, confidence="exact", object_path=obj.get(_TAG_PATH)))

    doomed = []
    for occurrence in changes.removed:
        done += 1
        said.step(done)
        obj = occurrence.payload
        try:
            out.removed.append(obj.name)
        except ReferenceError:
            continue
        if scope.is_aside(obj):
            # Gone from the assembly, and still the other add-on's.
            _disown(obj)
            continue
        doomed.append(obj)
    _remove_objects(doomed)
    _prune_tree(placer, new)
    _prune_prototypes(placer)

    material_lock.restore(locks)
    matdb.apply(objects + prototypes_objects(placer.prototypes), "update")
    report.frame_agree = len(report.matched)
    context.view_layer.update()
    _fit_empties(placer, objects)
    return objects, report, out


def _fit_empties(placer, objects):
    """Sizes the empties this run made to the parts under them. An empty
    the prune took out again is skipped."""
    alive = []
    for emp in placer.new_empties:
        try:
            emp.name
        except ReferenceError:
            continue
        alive.append(emp)
    empties_mod.fit(alive, objects)


def _reshape(obj, placer, inst):
    """New geometry on an object that is already in the scene: the mesh
    DATA is replaced and the object is not, so its place, its parent, its
    modifiers and its bone all survive.

    A collection instance takes the prototype of its new definition. The
    prototype it had is shared with the other placements of the old
    definition, and new geometry put into it changed them all."""
    if obj.type == "EMPTY" and obj.instance_type == "COLLECTION":
        col = placer.prototype(inst.definition_id)
        if col is not None and obj.instance_collection is not col:
            obj.instance_collection = col
        _object_colour(obj)
        return
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


# The tags that make an object an occurrence of the import and a part of
# the rig. A copy made in Blender loses them. RIG_parent_mode goes too, so
# a copy on a bone stays on it, as an object the user put there does.
_OCCURRENCE_TAGS = (_TAG_FILE, _TAG_PATH, _TAG_PERSISTENT, _TAG_COMPONENT,
                    _TAG_GROUP, _TAG_DEFINITION, _TAG_TOLERANCE, _TAG_LOCAL,
                    _TAG_GEOMETRY, _TAG_TRANSFORM, _TAG_DOCUMENT,
                    _TAG_CONFIGURATION, "RIG_parent_mode")


def _disown(obj):
    for tag in _OCCURRENCE_TAGS:
        if tag in obj.keys():
            del obj[tag]


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


def _prune_prototypes(placer):
    """Prototypes that no placement draws any more: the part left the
    assembly, or its placements took new geometry. A collection that holds
    anything besides the prototype is kept."""
    components = placer.components_collection
    if components is None:
        return
    wanted = {c.as_pointer() for c in placer.prototypes.values() if c is not None}
    for col in list(components.children):
        try:
            if col.get("SWMESH_role") != "part" or col.as_pointer() in wanted:
                continue
            if col.users_dupli_group or col.children \
                    or any(not o.get("SWMESH_prototype") for o in col.objects):
                continue
            for proto in list(col.objects):
                data = proto.data
                bpy.data.objects.remove(proto, do_unlink=True)
                if isinstance(data, bpy.types.Mesh) and data.users == 0:
                    bpy.data.meshes.remove(data)
            bpy.data.collections.remove(col)
        except (ReferenceError, RuntimeError):
            continue


def prototypes_objects(prototypes):
    """The prototype objects of the instancing mode, which hold the meshes
    and so the material slots."""
    out = []
    for collection in (prototypes or {}).values():
        if collection is not None:
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


def quads(objects):
    """Pair the tessellation triangles back into quads, when the scene asks.

    A part from the live link arrives as triangles, the same as a part from
    a STEP file, and the same setting decides what happens to them. It is
    read from the scene rather than passed in, because a send, a rebuild
    and a regenerate all have to give the same answer. Returns how many
    meshes were changed.
    """
    prg = getattr(bpy.context.scene, "stepper", None)
    if prg is None or not prg.tris_to_quads or not objects:
        return 0
    from .. import main as main_mod
    return main_mod._tris_to_quads_objects(objects) or 0


def refine(context, path, unit_scale=None, material_prefix="SW ",
           stems=None):
    """Swaps in finer geometry for objects that are already in the scene.

    The objects themselves are kept (only their mesh DATA is replaced), so
    transforms, bone parenting, constraints and the rig survive untouched.
    That is the whole point: refining a part must not cost the pose it is
    in, or the round trip would be useless for exactly the assemblies it is
    meant for.

    `stems` names the imports the geometry is for. Two configurations of
    one assembly hold parts with the same paths and ids, and the geometry
    of one configuration is not the other's: a part can be a different
    configuration of the part in each (imports.py). None takes every
    import, as before 1.2.

    unit_scale is as for build(). Returns the objects whose geometry
    changed."""
    if unit_scale is None:
        unit_scale = scene_unit_scale(context)
    scene = swmesh.load(path)
    materials = [_material(spec, material_prefix, unit_scale) for spec in scene.materials]
    locks = material_lock.take()
    # Rebuild from CAD leaves a part whose geometry is locked out of its
    # request. The CAD application also asks for geometry on its own (the
    # defeatured parts of a send), and a component id can name more parts
    # than were asked for, so the lock is also kept here.
    held = geometry_lock.locked_meshes()
    locked = []

    by_component = {}
    by_path = {}
    for obj in imports_mod.only(bpy.data.objects, stems):
        cid = obj.get(_TAG_COMPONENT)
        if cid:
            by_component.setdefault(cid, []).append(obj)
        path = obj.get(_TAG_PATH)
        if path:
            by_path.setdefault(path, []).append(obj)

    meshes = {}
    # A list answers "is this one already in?" by walking itself, which on
    # an assembly of 11761 parts is 69 million comparisons and most of the
    # time a rebuild takes (Conveyor12k-A00, Oscar, 2026-09-17). The names go
    # in a set beside it, and the list keeps the order.
    replaced, already = [], set()
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
        targets, kept = geometry_lock.split(targets, held)
        locked.extend(o.name for o in kept)
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
                _rescale_tag(holder, unit_scale)
                if holder.name not in already:
                    already.add(holder.name)
                    replaced.append(holder)
            _rescale_tag(obj, unit_scale)
            obj[_TAG_DEFINITION] = inst.definition_id
            obj[_TAG_TOLERANCE] = scene.tolerance
            if obj.name not in already:
                already.add(obj.name)
                replaced.append(obj)

    if mismatched:
        print("[CADLink native] %d piece(s) of geometry did not name an object "
              "in this scene and were left out: %s"
              % (len(mismatched), ", ".join(mismatched[:5])))
    if locked:
        print("[CADLink native] %d part(s) kept their locked geometry: %s"
              % (len(locked), ", ".join(locked[:5])))

    material_lock.restore(locks)
    matdb.apply(replaced, "refine")

    # Only now: a datablock may still have been in use while the loop ran.
    for name in retired:
        old = bpy.data.meshes.get(name)
        if old is not None and old.users == 0:
            bpy.data.meshes.remove(old)

    quads(replaced)
    context.view_layer.update()
    return replaced
