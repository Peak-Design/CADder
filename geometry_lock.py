# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometry locks: parts whose mesh CADder leaves alone.

A change made to a part in Blender is the user's work, and a rebuild puts
the geometry of the CAD data on the part again. These rebuild a part:
Rebuild from CAD, a finer mesh that the CAD application sends, Refresh
Model, a send that replaces the import, Rebuild from STEP (Regenerate),
Rebuild Selected and a refresh of a STEP file. A locked part keeps its
mesh through all of them.

A lock keeps the geometry and nothing else. A locked part still moves to
the pose that the CAD application gives it, because a pose is not
geometry. It still gets the tags of the new export, apart from the tags
that describe its mesh, and it keeps its lock.

Geometry sits on the mesh, and every part that shares a mesh shares its
geometry, so a mesh is locked when any part that uses it is locked. A
collection instance has no mesh: its geometry sits on the prototype
objects inside the collection it instances, and every placement of the
part shares them.
"""

from . import material_lock

try:
    import bpy
except ImportError:                                   # pragma: no cover
    bpy = None

PROP = "cadder_geometry_lock"


def register():
    bpy.types.Object.cadder_geometry_lock = bpy.props.BoolProperty(
        name="Lock Geometry",
        description="Keep the geometry of this part. Rebuild from CAD, "
                    "Rebuild from STEP and a refresh leave its mesh as it "
                    "is",
        default=False)


def unregister():
    del bpy.types.Object.cadder_geometry_lock


def is_locked(obj):
    try:
        return bool(getattr(obj, PROP, False))
    except ReferenceError:
        return False


def set_locked(obj, locked):
    setattr(obj, PROP, bool(locked))


def has_geometry(obj):
    """True for an object that has geometry, itself or through the
    collection it instances. Cheap enough for a poll."""
    return material_lock.has_materials(obj)


def holders(obj):
    """The objects whose data is the geometry of this part: the part
    itself, or the prototypes inside the collection it instances. The
    same objects hold its materials."""
    return material_lock.holders(obj)


def is_part(obj):
    """True for a part that CADder made from CAD data, which a rebuild can
    replace. A prototype of the live link is not a part: the placements
    that instance it are. Cheap enough for a poll."""
    try:
        return ((obj.get("STEP_file") is not None
                 or obj.get("SWMESH_file") is not None)
                and not obj.get("SWMESH_prototype")
                and has_geometry(obj))
    except ReferenceError:
        return False


def parts(objects):
    """The objects that Lock Geometry acts on."""
    return [o for o in objects if is_part(o)]


def locked_meshes(objects=None):
    """The data of every locked part, which a rebuild leaves alone."""
    out = set()
    for obj in (bpy.data.objects if objects is None else objects):
        if is_locked(obj):
            for holder in holders(obj):
                out.add(holder.data)
    return out


def keeps(obj, meshes):
    """True when a rebuild must leave the geometry of this part as it is:
    the part is locked, or it uses a mesh that `meshes` (locked_meshes)
    holds."""
    if is_locked(obj):
        return True
    return any(holder.data in meshes for holder in holders(obj))


def split(objects, meshes=None):
    """(the parts that a rebuild can give new geometry, the parts that keep
    their geometry)."""
    if meshes is None:
        meshes = locked_meshes()
    free, kept = [], []
    for obj in objects:
        (kept if keeps(obj, meshes) else free).append(obj)
    return free, kept


def read_tags(obj, names):
    """The values of these custom properties, None for one it does not
    have, to put back with write_tags."""
    out = {}
    for name in names:
        value = obj.get(name)
        if hasattr(value, "to_list"):
            value = value.to_list()
        elif hasattr(value, "to_dict"):
            value = value.to_dict()
        out[name] = value
    return out


def write_tags(obj, tags):
    """Put back what read_tags read. A property the object did not have
    then is taken off."""
    for name, value in tags.items():
        if value is not None:
            obj[name] = value
        elif name in obj.keys():
            del obj[name]


def _key(obj):
    """What finds the same part again after a send replaced every object:
    the import it came from and its place in the assembly, the same key as
    material_lock uses. None for a part that has neither."""
    stem = obj.get("SWMESH_file")
    path = obj.get("SWMESH_path")
    if not stem or not path:
        return None
    return str(stem), str(path)


class Held:
    """The meshes of the parts that keep their geometry, kept through a
    send that replaces every object."""

    def __init__(self):
        # (key, locked, tags of the part,
        #  [(holder name, mesh, tags of the holder), ...])
        self.entries = []
        # Meshes given a fake user until the send is over. The send
        # removes every mesh that it leaves with no users.
        self.pinned = []

    def __len__(self):
        return len(self.entries)


def take(objects, tags=()):
    """Write down the meshes of the parts among these objects that keep
    their geometry, and keep those meshes in the file until restore.

    tags: the custom properties that describe a mesh. They go back with
    the mesh. Only a part that has a key is written down, because restore
    finds the part by its key."""
    held = Held()
    meshes = locked_meshes()
    for obj in objects:
        key = _key(obj)
        if key is None or not keeps(obj, meshes):
            continue
        rows = []
        for holder in holders(obj):
            mesh = holder.data
            if mesh.library is None and not mesh.use_fake_user:
                mesh.use_fake_user = True
                held.pinned.append(mesh)
            rows.append((holder.name, mesh, read_tags(holder, tags)))
        held.entries.append((key, is_locked(obj), read_tags(obj, tags),
                             rows))
    return held


def release(held):
    """Take the fake users off again."""
    for mesh in held.pinned:
        try:
            mesh.use_fake_user = False
        except ReferenceError:
            pass
    held.pinned = []


def restore(held, among):
    """Put the written-down meshes on the parts that a send made in place
    of the ones it removed. Returns how many parts got their geometry
    back.

    Each part is found among the new objects by its place in the assembly.
    The new part is locked as the old one was, it gets the old mesh, and
    it gets the tags of the old part that describe that mesh. The mesh
    that the send made for it goes when no other object uses it. A kept
    mesh that no new part uses goes too, as the send removes the other
    meshes of the old parts."""
    pinned = list(held.pinned)
    try:
        if not held.entries:
            return 0
        found = {}
        for obj in among:
            key = _key(obj)
            if key is not None:
                found.setdefault(key, obj)
        # Each kept mesh, with the tags of a part that uses it. A part that
        # is new in this send can use a kept mesh through the prototype it
        # shares with a kept part, and its tags must then say so too.
        used = {}
        done = set()
        made = []
        kept = {row[1] for entry in held.entries for row in entry[3]}
        for key, locked, tags, rows in held.entries:
            target = found.get(key)
            if target is None:
                continue
            if locked:
                set_locked(target, True)
            if _put_back(target, rows, kept, made):
                write_tags(target, tags)
                done.add(target.name)
                for holder in holders(target):
                    used.setdefault(holder.data, tags)
        for obj in among:
            if obj.name in done:
                continue
            for holder in holders(obj):
                tags = used.get(holder.data)
                if tags is not None:
                    write_tags(obj, tags)
                    break
        for mesh in made:
            if _alive(mesh) is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        return len(done)
    finally:
        release(held)
        for mesh in pinned:
            if _alive(mesh) is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)


def _alive(datablock):
    try:
        datablock.name
    except ReferenceError:
        return None
    return datablock


def _put_back(target, rows, kept, made):
    """The meshes a part had, on the holders it has now. A holder takes
    the mesh of the holder with its name, or of the holder in the same
    place when the part has as many holders as before. A holder that
    matches neither keeps the new mesh. The meshes that the send made and
    that a kept mesh replaced go into `made`. A mesh in `kept` does not:
    another part can still need it. Returns True when a holder got a kept
    mesh."""
    by_name = {name: (mesh, tags) for name, mesh, tags in rows}
    now = holders(target)
    back = False
    for n, holder in enumerate(now):
        before = by_name.get(holder.name)
        if before is None and len(rows) == len(now):
            before = rows[n][1:]
        if before is None or _alive(before[0]) is None:
            continue
        mesh, tags = before
        if holder.data != mesh:
            if holder.data not in kept:
                made.append(holder.data)
            holder.data = mesh
        write_tags(holder, tags)
        back = True
    return back
