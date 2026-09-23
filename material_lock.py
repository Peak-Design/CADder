# SPDX-License-Identifier: GPL-3.0-or-later
"""Material locks: parts whose materials CADder leaves alone.

A material picked for a part in Blender is the user's decision. Five things
put other materials on a part: the material database (Apply, and every
import and send that applies the active database), a refresh or a refine
from the CAD application, which brings new geometry with the CAD
appearances on it, a send that replaces the import, a refresh of a STEP
file, and Regenerate. A locked part keeps its materials through all of
them.

Each of those writes down the materials of the locked parts first (take)
and puts them back after (restore).

Materials sit on the mesh, and every part that shares a mesh shares its
materials, so a mesh is locked when any part that uses it is locked. A
collection instance has no mesh: its materials sit on the prototype
objects inside the collection it instances.

A lock keeps what the part has when the change starts, not what it had
when it was locked. A material the user picks later is kept too.
"""

import json

try:
    import bpy
except ImportError:                                   # pragma: no cover
    bpy = None

PROP = "cadder_material_lock"

_UNSET = object()


def register():
    bpy.types.Object.cadder_material_lock = bpy.props.BoolProperty(
        name="Lock Materials",
        description="Keep the materials of this part. The material "
                    "database, a refresh from the CAD application and "
                    "Regenerate leave them as they are",
        default=False)


def unregister():
    del bpy.types.Object.cadder_material_lock


def is_locked(obj):
    try:
        return bool(getattr(obj, PROP, False))
    except ReferenceError:
        return False


def set_locked(obj, locked):
    setattr(obj, PROP, bool(locked))


def has_materials(obj):
    """True for an object that can carry materials, itself or through the
    collection it instances. Cheap enough for a poll."""
    data = getattr(obj, "data", None)
    if data is not None:
        return hasattr(data, "materials")
    return (getattr(obj, "instance_type", "") == "COLLECTION"
            and getattr(obj, "instance_collection", None) is not None)


def holders(obj, _seen=None):
    """The objects whose data holds the materials of this part: the part
    itself, or the prototypes inside the collection it instances."""
    data = getattr(obj, "data", None)
    if data is not None:
        return [obj] if hasattr(data, "materials") else []
    if getattr(obj, "instance_type", "") != "COLLECTION":
        return []
    collection = getattr(obj, "instance_collection", None)
    if collection is None:
        return []
    seen = set() if _seen is None else _seen
    if collection in seen:
        return []
    seen.add(collection)
    out = []
    for inner in collection.all_objects:
        out.extend(holders(inner, seen))
    return out


def parts(objects):
    """The objects that Lock Materials acts on."""
    return [o for o in objects if has_materials(o)]


def locked_meshes(objects=None):
    """The data of every locked part, which the database leaves alone."""
    out = set()
    for obj in (bpy.data.objects if objects is None else objects):
        if is_locked(obj):
            for holder in holders(obj):
                out.add(holder.data)
    return out


def originals(obj):
    """The name each slot had when CADder made it (STEP_materials), or
    None when the object does not say."""
    try:
        names = json.loads(obj.get("STEP_materials") or "null")
    except (TypeError, ValueError):
        return None
    return names if isinstance(names, list) else None


def _key(obj):
    """What finds the same part again after a send replaced every object:
    the import it came from and its place in the assembly. None for a part
    that has neither."""
    stem = obj.get("SWMESH_file")
    path = obj.get("SWMESH_path")
    if not stem or not path:
        return None
    return str(stem), str(path)


class Held:
    """The materials of the locked parts before a change, to put back
    after it."""

    def __init__(self):
        # (object, key, [(holder name, [(original, material), ...]), ...])
        self.entries = []
        # Materials given a fake user until the change is over. A change
        # can leave a locked material with no users for a moment, and the
        # database removes an import material with no users.
        self.pinned = []

    def __len__(self):
        return len(self.entries)


def take(objects=None):
    """Write down the materials of the locked parts among these objects,
    or of every locked part."""
    held = Held()
    for obj in (bpy.data.objects if objects is None else objects):
        if not is_locked(obj):
            continue
        rows = []
        for holder in holders(obj):
            names = originals(holder)
            slots = []
            for i, mat in enumerate(holder.data.materials):
                name = names[i] if names is not None and i < len(names) else None
                slots.append((name or None, mat))
                if (mat is not None and mat.library is None
                        and not mat.use_fake_user):
                    mat.use_fake_user = True
                    held.pinned.append(mat)
            rows.append((holder.name, slots))
        held.entries.append((obj, _key(obj), rows))
    return held


def release(held):
    """Take the fake users off again."""
    for mat in held.pinned:
        try:
            mat.use_fake_user = False
        except ReferenceError:
            pass
    held.pinned = []


def restore(held, among=None):
    """Put the written-down materials back. Returns how many slots it
    changed.

    among: the objects a send made in place of the ones it removed. Each
    part is found among them by its place in the assembly, and the new
    object is locked as the old one was."""
    try:
        if not held.entries:
            return 0
        found = {}
        for obj in among or ():
            key = _key(obj)
            if key is not None:
                found.setdefault(key, obj)
        changed = 0
        for obj, key, rows in held.entries:
            if among is not None:
                target = found.get(key) if key is not None else None
            else:
                target = _alive(obj)
            if target is None:
                continue
            if among is not None:
                set_locked(target, True)
            changed += _put_back(target, rows)
        return changed
    finally:
        release(held)


def _alive(datablock):
    try:
        datablock.name
    except ReferenceError:
        return None
    return datablock


def _put_back(target, rows):
    """The materials a part had, on the slots it has now.

    A slot takes the material of the slot that had the same original name,
    because a new tessellation can put the materials in another order. A
    slot whose original is new (the appearance was changed in the CAD
    application) takes the material of the slot at the same place, so a
    part with one material keeps it whatever the CAD application calls it
    now."""
    by_name, twice = {}, set()
    for _holder, slots in rows:
        for name, mat in slots:
            if name is None:
                continue
            if name in by_name and by_name[name] != mat:
                twice.add(name)
            by_name.setdefault(name, mat)
    by_holder = dict(rows)
    now = holders(target)
    changed = 0
    for n, holder in enumerate(now):
        before = by_holder.get(holder.name)
        if before is None and len(rows) == len(now):
            before = rows[n][1]
        names = originals(holder)
        mats = holder.data.materials
        for i in range(len(mats)):
            name = names[i] if names is not None and i < len(names) else None
            want = _UNSET
            if name and name in by_name and name not in twice:
                want = by_name[name]
            elif before is not None and i < len(before):
                want = before[i][1]
            if want is _UNSET:
                continue
            if want is not None and _alive(want) is None:
                continue
            if mats[i] != want:
                mats[i] = want
                changed += 1
    return changed
