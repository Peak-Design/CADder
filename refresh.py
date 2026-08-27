# SPDX-License-Identifier: GPL-3.0-or-later
"""Re-import a STEP file over the top of the one already in the scene, and
keep the work the user built around it.

A refresh is not "delete and import again". Between the first import and the
refresh a user has been WORKING: they put a bevel and a weighted normal on a
bracket, dragged the assembly into a collection of their own, parented a
light to a part, moved the whole thing off the origin, hid the fasteners.
None of that is in the STEP file, and a naive re-import throws all of it
away.

So the objects are not replaced. They are KEPT. Only what the file describes
moves onto them: the mesh, the material slots and the CAD placement. The
object datablock never goes, so everything hanging off it survives without
being written down and put back: modifiers, constraints, drivers, animation,
custom properties, which collections it sits in, and whatever the user
parented to it.

Two things the file describes are still the user's to override, and both are
kept:

  * Placement. The file decides where a part sits, but a user who moved the
    assembly off the origin meant it. The importer writes down where it put
    each object (STEP_import_basis), so the refresh can tell a part that
    moved in CAD from one the user moved in Blender, and apply the user's
    move on top of the new CAD position.

  * Parenting. The assembly structure comes from the file, unless the user
    re-parented an object onto something of their own. That is a rig, and
    the refresh leaves it alone.

Identity across a re-import is a cascade, because no single key survives
every edit. The CAD uuid with the CAD name first. Then the CAD name with the
node tag, which survives a uuid changing. Then the CAD name alone, which
survives a node moving in the tree. Last the object's own name. What matches
by none of them is reported rather than guessed at: an object that has gone
from the file, or one that is new in it, is exactly the "the assembly
changed" signal worth showing a user.
"""

import inspect
import json
import os

try:
    import bpy
    from mathutils import Matrix
except ImportError:
    bpy = None
    Matrix = None

FILE_PROP = "STEP_file"
ROLE_PROP = "STEP_role"

# Where the import put an object, as 16 floats in row order. The difference
# between this and the object's transform now is the user's own move, which
# a refresh has to carry over onto the new CAD placement.
BASIS_PROP = "STEP_import_basis"

# The object color the import wrote. Set in main.set_object_colors, and read
# back here to tell it apart from a color the user picked.
OBJECT_COLOR_PROP = "STEP_object_color"

# Scene-level record of what was imported and how, so a refresh can reproduce
# the import rather than guess at its settings.
REGISTRY_PROP = "step_import_registry"

# Every custom property the importer owns starts with this. A refresh takes
# the whole set off the object and puts the new one on, so a stamp that is
# no longer written does not linger. Anything else on the object is the
# user's and is never touched.
STAMP_PREFIX = "STEP_"


# -- the registry ------------------------------------------------------------

def _registry_read(scene):
    raw = scene.get(REGISTRY_PROP) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _registry_write(scene, data):
    scene[REGISTRY_PROP] = json.dumps(data)


def file_stat(path):
    """(mtime, size) of the file on disk, or None when it is not there."""
    try:
        st = os.stat(bpy.path.abspath(path))
        return [st.st_mtime, st.st_size]
    except OSError:
        return None


def record_import(scene, path, settings):
    """Remembers how a file was imported. Called at the end of every import,
    so a refresh reproduces the settings instead of falling back to whatever
    the dialog happens to hold later."""
    if scene is None:
        return
    data = _registry_read(scene)
    data[path] = {"settings": settings, "stat": file_stat(path)}
    _registry_write(scene, data)


def merge_registry(target, source):
    """Copies one scene's import records onto another.

    A background import builds the objects in a worker scene and appends
    them, then deletes that scene. The record of how the file was imported
    is on it, so it has to travel with them. Without this a refresh has
    nothing to reproduce, and a file imported at a custom scale comes back
    a different size.
    """
    if target is None or source is None:
        return 0
    incoming = _registry_read(source)
    if not incoming:
        return 0
    data = _registry_read(target)
    data.update(incoming)
    _registry_write(target, data)
    return len(incoming)


def forget(scene, path):
    data = _registry_read(scene)
    if data.pop(path, None) is not None:
        _registry_write(scene, data)


def settings_for(scene, path):
    """The settings a file was imported with, or None when this blend predates
    the registry (or the entry was cleared)."""
    entry = _registry_read(scene).get(path)
    if not isinstance(entry, dict):
        return None
    settings = entry.get("settings")
    return settings if isinstance(settings, dict) else None


def stamped_settings(path):
    """The same record, read back off the objects themselves.

    The importer stamps it on everything it makes. That copy travels with
    the objects through a background import, where the scene copy does not,
    so it is what a refresh falls back to before it falls back to guessing.
    """
    for obj in file_objects(path):
        raw = obj.get("STEP_import_settings")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("unit_scale"):
            return data
    return None


def changed_on_disk(scene, path):
    """Whether the file has been written since it was imported. None when
    that cannot be told: no record, or the file is gone."""
    entry = _registry_read(scene).get(path)
    was = (entry or {}).get("stat")
    now = file_stat(path)
    if not was or not now:
        return None
    return [round(was[0], 3), was[1]] != [round(now[0], 3), now[1]]


# -- reproducing the import --------------------------------------------------

def _accepted_kwargs():
    from . import main as _main
    return set(inspect.signature(_main.load_step).parameters)


def _scale_override(record, scene):
    """The scale that keeps a refresh the same size as the import it repeats.

    load_step divides the file's unit scale by the scene unit length, so the
    same file gives a different size in two scenes set up differently. A
    background import runs in a factory scene at 1.0, which is not
    necessarily the scene the objects ended up in. Feeding the resolved
    scale back through that division reproduces the size exactly.

    Only a mismatch is corrected. The file's own units decide the size of a
    first import, and they still decide it here when nothing disagrees, so
    a part whose units really did change in CAD is not pinned to the old
    size.
    """
    was = record.get("scene_unit_scale")
    resolved = record.get("unit_scale")
    if record.get("custom_scale") is not None or not was or not resolved:
        return None
    now = scene.unit_settings.scale_length
    if abs(was - now) <= 1e-12 * max(1.0, abs(now)):
        return None
    return resolved * now


def import_settings(scene, path):
    """The load_step arguments that repeat this import, and where they came
    from. Returns (kwargs, record, source). `source` is None when nothing
    was recorded and the current defaults are all there is to go on."""
    from . import main as _main

    record = settings_for(scene, path)
    source = "recorded settings"
    if not record:
        record = stamped_settings(path)
        source = "settings stamped on the objects"
    if not record:
        record = dict(_main.import_defaults())
        source = None

    accepted = _accepted_kwargs()
    kwargs = {k: v for k, v in record.items() if k in accepted}
    override = _scale_override(record, scene)
    if override is not None:
        kwargs["custom_scale"] = override
    return kwargs, record, source


# -- what is in the scene ----------------------------------------------------

def _own(datablock, path):
    """Whether this object or collection was made by the import of `path`."""
    try:
        return datablock.get(FILE_PROP) == path
    except (AttributeError, TypeError):
        return False


def file_objects(path):
    return [o for o in bpy.data.objects if _own(o, path)]


def file_collections(path):
    return [c for c in bpy.data.collections if _own(c, path)]


def imported_files():
    """Every STEP file this blend holds, sorted by path so the panel does not
    reorder itself."""
    counts = {}
    for obj in bpy.data.objects:
        f = obj.get(FILE_PROP)
        if f:
            counts.setdefault(f, {"objects": 0, "collections": 0})["objects"] += 1
    for col in bpy.data.collections:
        f = col.get(FILE_PROP)
        if f:
            counts.setdefault(f, {"objects": 0, "collections": 0})["collections"] += 1
    return [{"path": p, **v} for p, v in sorted(counts.items())]


def _parents_of(col):
    """The collections a collection is linked into. bpy exposes only children,
    so this inverts, including the scene master collections, which are not in
    bpy.data.collections."""
    out = []
    for scene in bpy.data.scenes:
        if col.name in [c.name for c in scene.collection.children]:
            out.append(("SCENE", scene.name))
    for other in bpy.data.collections:
        if other is col:
            continue
        if col.name in [c.name for c in other.children]:
            out.append(("COLLECTION", other.name))
    return out


def owned_roots(path):
    """The import's own collections that are NOT inside another of its own:
    the tops of its subtree, and the only ones whose placement a user can
    have changed without touching anything inside."""
    owned = {c.name for c in file_collections(path)}
    roots = []
    for col in file_collections(path):
        parents = _parents_of(col)
        if not parents or any(kind == "SCENE" or name not in owned
                              for kind, name in parents):
            roots.append(col)
    return roots


# -- matching the new import onto what is already here -----------------------

def _is_occurrence(obj):
    """An instancing empty, as COLLECTION_INSTANCES mode makes. It carries
    the same CAD name as the prototype it instances, so the two have to be
    told apart or they match each other."""
    return obj.get("STEP_instance_of") is not None


def _keys(obj):
    """The identity cascade for one object, strongest key first."""
    name = obj.get("STEP_name")
    kind = _is_occurrence(obj)
    return (
        ("uuid", obj.get("STEP_uuid"), name, kind),
        ("tag", name, obj.get("STEP_tag"), kind),
        ("name", name, kind),
        ("object", obj.name),
    )


def _match(old_objs, fresh_objs):
    """Pairs each freshly imported object with the one it replaces.

    Returns (pairs, added, gone): the matches, the objects that are new in
    the file, and the ones that have gone from it.
    """
    buckets = {}
    for obj in old_objs:
        for level, key in enumerate(_keys(obj)):
            if key[1] is None:
                continue
            buckets.setdefault((level, key), []).append(obj)

    taken, pairs, added = set(), [], []
    for fresh in fresh_objs:
        found = None
        for level, key in enumerate(_keys(fresh)):
            if key[1] is None:
                continue
            same = [o for o in buckets.get((level, key), ()) if o not in taken]
            # A key that picks out more than one object has not identified
            # anything. Fall through to the weaker keys instead of guessing.
            if len(same) == 1:
                found = same[0]
                break
            # Except at the end of the cascade. Several objects left sharing
            # the last key are repeated occurrences of ONE part, which is
            # what an assembly is mostly made of. Their uuids all change
            # together when a component is added earlier in the tree, and
            # they are interchangeable apart from the work the user did on
            # them. Pairing them in order keeps that work on the part it
            # belongs to. Calling them all new would throw it away.
            if same and key[0] == "name":
                found = same[0]
                break
        if found is None:
            added.append(fresh)
        else:
            taken.add(found)
            pairs.append((found, fresh))
    gone = [o for o in old_objs if o not in taken]
    return pairs, added, gone


def _map_collections(old_cols, fresh_cols, fresh_to_old):
    """Which collection already in the scene each freshly made one repeats.

    Roles the import makes one of (the wrapper, the curves collection, the
    components collection) match on the role alone. A per-part collection is
    identified by the prototype inside it, which the object matching has
    already paired up.
    """
    by_role = {}
    for col in old_cols:
        by_role.setdefault(col.get(ROLE_PROP) or col.name, []).append(col)

    out = {}
    for col in fresh_cols:
        role = col.get(ROLE_PROP) or col.name
        if role == "part":
            for obj in col.objects:
                old = fresh_to_old.get(obj)
                if old is None:
                    continue
                for home in old.users_collection:
                    if (home.get(ROLE_PROP) or "") == "part":
                        out[col] = home
                        break
                break
        else:
            same = by_role.get(role) or []
            if len(same) == 1:
                out[col] = same[0]
    return out


# -- moving the new import onto the objects already here ---------------------

def _user_move(obj):
    """How far the user has moved this object since the import put it down,
    in its parent's space. None when they have not touched it."""
    stored = obj.get(BASIS_PROP)
    try:
        rows = [list(stored[i * 4:i * 4 + 4]) for i in range(4)]
    except (TypeError, ValueError, IndexError):
        return None
    if len(rows) != 4 or any(len(r) != 4 for r in rows):
        return None
    try:
        was = Matrix(rows)
        delta = obj.matrix_basis @ was.inverted()
    except ValueError:
        return None
    # Inverting and multiplying never gives back an exact identity, so an
    # object nobody touched has to be recognized as untouched or it drifts
    # a little further on every refresh.
    if all(abs(delta[i][j] - (1.0 if i == j else 0.0)) < 1e-6
           for i in range(4) for j in range(4)):
        return None
    return delta


def stamp_basis(objects):
    """Writes down where the import put each object. Called at the end of an
    import and again after a refresh."""
    for obj in objects:
        try:
            obj[BASIS_PROP] = [v for row in obj.matrix_basis for v in row]
        except (AttributeError, TypeError, ReferenceError):
            pass


def _restamp(old, fresh):
    """Replaces the importer's own custom properties, and only those."""
    for key in [k for k in old.keys() if k.startswith(STAMP_PREFIX)]:
        del old[key]
    for key in fresh.keys():
        if key.startswith(STAMP_PREFIX):
            old[key] = fresh[key]


def _user_materials(obj):
    """The material slots the user put there, or None when the slots are
    still the import's own.

    Materials sit on the mesh, and the mesh is replaced, so a material the
    user assigned to an imported part would go. The importer writes down
    what it assigned (STEP_materials), so anything else in the slots now is
    the user's and has to be put back.
    """
    data = getattr(obj, "data", None)
    if data is None or not hasattr(data, "materials"):
        return None
    try:
        was = json.loads(obj.get("STEP_materials") or "[]")
    except (TypeError, ValueError):
        return None
    now = [m.name if m else "" for m in data.materials]
    if not isinstance(was, list) or now == was:
        return None
    return now


def _vertex_groups(obj):
    """The user's vertex groups, taken off the mesh before it is replaced.

    Vertex groups live on the mesh data, so swapping the mesh takes them
    with it. The names always come back, because a modifier that points at
    one by name loses the link otherwise. The weights only come back when
    the part is unchanged: they index vertices, and a part re-tessellated or
    edited in CAD has different ones.
    """
    data = getattr(obj, "data", None)
    if data is None or not hasattr(data, "vertices"):
        return None
    names = [g.name for g in obj.vertex_groups]
    if not names:
        return None
    weights = [(v.index, g.group, g.weight)
               for v in data.vertices for g in v.groups]
    return names, weights, len(data.vertices)


def _restore_vertex_groups(obj, saved):
    names, weights, count = saved
    groups = []
    for name in names:
        existing = obj.vertex_groups.get(name)
        groups.append(existing if existing is not None
                      else obj.vertex_groups.new(name=name))
    if len(obj.data.vertices) != count:
        return
    for vert, index, weight in weights:
        if index < len(groups):
            groups[index].add([vert], weight, "REPLACE")


def _object_color_is_users(obj):
    """Whether the object color on screen is the user's pick rather than the
    one the import wrote. The importer stamps what it wrote, so anything
    else is theirs and a refresh leaves it alone."""
    stamped = obj.get(OBJECT_COLOR_PROP)
    if stamped is None:
        return True          # imported before the stamp: never overwrite
    try:
        return any(abs(a - b) > 1e-6 for a, b in zip(obj.color, stamped))
    except TypeError:
        return True


def _adopt(old, fresh, col_map):
    """Moves the freshly imported result onto the object already in the
    scene. Returns the data the object used to hold, for purging.

    Only what the STEP file describes is touched. The object keeps its
    modifiers, constraints, drivers, animation, collections, visibility and
    anything parented to it, because it is the same object.
    """
    was = old.data
    mine = _user_materials(old)
    groups = _vertex_groups(old)
    keep_color = _object_color_is_users(old)
    if old.type == fresh.type:
        old.data = fresh.data
    else:
        was = None
        mine = None
        groups = None

    if groups is not None:
        _restore_vertex_groups(old, groups)

    if mine is not None:
        slots = old.data.materials
        for i, name in enumerate(mine):
            mat = bpy.data.materials.get(name) if name else None
            if i < len(slots):
                slots[i] = mat
            elif mat is not None:
                slots.append(mat)

    # An instancing empty points at the collection holding its prototype.
    # That prototype is one of the objects already in the scene, so the
    # empty has to point at ITS collection, not the freshly made duplicate.
    if fresh.instance_type == "COLLECTION":
        old.instance_type = "COLLECTION"
        target = fresh.instance_collection
        old.instance_collection = col_map.get(target, target)

    _restamp(old, fresh)

    # The object color comes from the file, so a part recolored in CAD
    # shows the new color. One the user picked themselves stays theirs.
    if not keep_color:
        old.color = fresh.color
    return was


def _purge(datablocks):
    """Meshes and curves the refresh replaced. Blender keeps a datablock with
    no users until the file is reopened, so without this a refresh a day
    grows the blend by a whole assembly each time."""
    for data in datablocks:
        if data is None:
            continue
        try:
            if data.users:
                continue
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)
        except (ReferenceError, RuntimeError):
            pass


def _once(names):
    """The same names, in order, without the repeats."""
    seen, out = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def apply_import(path, old_objs, old_cols):
    """Folds a completed re-import into the objects that were already here.

    `old_objs` and `old_cols` are what the file owned BEFORE load_step ran.
    Everything it owns now that is not in those lists is freshly made.

    Returns (kept, added, gone): how many objects were updated in place, the
    names that are new in the file, and the names that have gone from it.
    """
    known_objs, known_cols = set(old_objs), set(old_cols)
    fresh_objs = [o for o in file_objects(path) if o not in known_objs]
    fresh_cols = [c for c in file_collections(path) if c not in known_cols]

    pairs, added, gone = _match(old_objs, fresh_objs)
    fresh_to_old = {fresh: old for old, fresh in pairs}
    col_map = _map_collections(old_cols, fresh_cols, fresh_to_old)

    # 1. The geometry, the material slots and the import's own stamps.
    stale = [_adopt(old, fresh, col_map) for old, fresh in pairs]

    # 2. Parenting. The assembly structure is the file's, unless the user
    #    re-parented the object onto something of their own. That is a rig,
    #    and tearing it apart on a refresh is exactly what this module is
    #    written to avoid.
    for old, fresh in pairs:
        if old.parent is not None and not _own(old.parent, path):
            continue
        want = fresh.parent
        if want is not None:
            want = fresh_to_old.get(want, want)
        if old.parent is not want:
            old.parent = want
        old.matrix_parent_inverse = fresh.matrix_parent_inverse.copy()
    for obj in added:
        if obj.parent in fresh_to_old:
            obj.parent = fresh_to_old[obj.parent]

    # 3. Placement, with the user's own move carried over onto it.
    for old, fresh in pairs:
        move = _user_move(old)
        old.matrix_basis = (move @ fresh.matrix_basis if move is not None
                            else fresh.matrix_basis.copy())

    # 4. Components that are new in the file stay where the import put them,
    #    except that the import put them in a duplicate of a collection the
    #    scene already has. Move them into the real one.
    for col in fresh_cols:
        target = col_map.get(col)
        if target is None:
            continue                       # a collection new in this import
        for obj in list(col.objects):
            col.objects.unlink(obj)
            if obj in fresh_to_old or obj.name in target.objects:
                continue
            target.objects.link(obj)
        for child in list(col.children):
            col.children.unlink(child)
            if child in col_map:
                continue                   # a duplicate, removed below
            if child.name not in [c.name for c in target.children]:
                target.children.link(child)

    # 5. The duplicates have handed everything over and can go. Read the
    #    names for the report first: an object cannot be asked for its name
    #    once it has been removed.
    #    COLLECTION_INSTANCES mode makes two objects per component, the
    #    prototype and the empty that instances it, so report the CAD names
    #    once each and not once per object.
    added_names = _once([o.get("STEP_name") or o.name for o in added])
    gone_names = _once([o.get("STEP_name") or o.name for o in gone])
    for old, fresh in pairs:
        bpy.data.objects.remove(fresh, do_unlink=True)
    for col in col_map:
        bpy.data.collections.remove(col)
    for obj in gone:
        bpy.data.objects.remove(obj, do_unlink=True)
    _purge(stale)

    stamp_basis(file_objects(path))
    return len(pairs), added_names, gone_names


# -- operators and panel -----------------------------------------------------

if bpy is not None:

    class STEP_OT_RefreshFile(bpy.types.Operator):
        """Re-import this STEP file from disk. Your modifiers, collections,
        parenting and placement are kept"""
        bl_idname = "stepper.refresh_file"
        bl_label = "Refresh from disk"
        bl_options = {"REGISTER", "UNDO"}

        filepath: bpy.props.StringProperty()

        def execute(self, context):
            from . import main as _main

            path = self.filepath
            if not os.path.isfile(bpy.path.abspath(path)):
                self.report({"ERROR"}, "Not on disk any more: %s" % path)
                return {"CANCELLED"}

            settings, record, source = import_settings(context.scene, path)
            if source is None:
                self.report({"WARNING"},
                            "No record of how this file was imported. "
                            "Refreshing with the current defaults")

            old_objs = file_objects(path)
            old_cols = file_collections(path)
            if not old_objs:
                self.report({"ERROR"}, "Nothing in the scene from this file")
                return {"CANCELLED"}

            # The reader caches by path, and the whole point of a refresh is
            # to read the file again.
            try:
                _main._cache_drop(path)
            except Exception:
                pass

            try:
                result = _main.load_step(context, path, **settings)
            except Exception as exc:
                self.report({"ERROR"}, "Re-import failed: %s" % exc)
                return {"CANCELLED"}
            if result is False:
                self.report({"ERROR"}, "Re-import failed. See the console")
                return {"CANCELLED"}

            kept, added, gone = apply_import(path, old_objs, old_cols)

            msg = "Refreshed %s: %d object(s) updated in place" % (
                os.path.basename(path), kept)
            level = "INFO"
            if added or gone:
                level = "WARNING"
                msg += (". The assembly changed: %d component(s) gone, %d new"
                        % (len(gone), len(added)))
                for name in gone[:10]:
                    print("[STEPper refresh] gone from the file: %s" % name)
                for name in added[:10]:
                    print("[STEPper refresh] new in the file: %s" % name)

            # The size must not change on a refresh. It can only change if
            # the file's units changed, and a user who sees their assembly
            # resize needs to be told why rather than left to guess.
            was = (record or {}).get("unit_scale")
            now = (stamped_settings(path) or {}).get("unit_scale")
            if was and now and abs(was - now) > 1e-9 * max(was, now):
                level = "WARNING"
                msg += (". The file's units changed, so the size changed by "
                        "%.4g times" % (now / was))
            self.report({level}, msg)
            return {"FINISHED"}

    class STEP_OT_SelectFile(bpy.types.Operator):
        """Select every object that came from this STEP file"""
        bl_idname = "stepper.select_file_objects"
        bl_label = "Select"
        bl_options = {"REGISTER", "UNDO"}

        filepath: bpy.props.StringProperty()

        def execute(self, context):
            objs = file_objects(self.filepath)
            try:
                bpy.ops.object.select_all(action="DESELECT")
            except RuntimeError:
                pass
            n = 0
            for obj in objs:
                try:
                    obj.select_set(True)
                    n += 1
                except RuntimeError:
                    pass       # not in the view layer
            if n:
                context.view_layer.objects.active = objs[0]
            self.report({"INFO"}, "Selected %d object(s)" % n)
            return {"FINISHED"}

    class STEP_OT_ForgetFile(bpy.types.Operator):
        """Forget how this file was imported. The objects stay. Only the
        remembered import settings go."""
        bl_idname = "stepper.forget_file"
        bl_label = "Forget settings"
        bl_options = {"REGISTER", "UNDO"}

        filepath: bpy.props.StringProperty()

        def execute(self, context):
            forget(context.scene, self.filepath)
            self.report({"INFO"}, "Forgotten")
            return {"FINISHED"}

    class STEP_PT_ImportedFiles(bpy.types.Panel):
        bl_label = "STEPper NEXT: Imported files"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "STEPper NEXT"

        def draw(self, context):
            layout = self.layout
            files = imported_files()
            if not files:
                layout.label(text="No STEP file imported in this blend",
                             icon="INFO")
                return

            for rec in files:
                path = rec["path"]
                box = layout.box()
                head = box.row(align=True)
                head.label(text=os.path.basename(path), icon="FILE_3D")

                missing = not os.path.isfile(bpy.path.abspath(path))
                changed = None if missing else changed_on_disk(context.scene, path)
                if missing:
                    box.label(text="Not on disk any more", icon="ERROR")
                elif changed:
                    box.label(text="Changed on disk since import",
                              icon="TEMP")
                elif changed is None:
                    box.label(text="Imported before settings were recorded",
                              icon="QUESTION")

                box.label(text="%d object(s), %d collection(s)"
                               % (rec["objects"], rec["collections"]))

                row = box.row(align=True)
                op = row.operator("stepper.refresh_file", icon="FILE_REFRESH")
                op.filepath = path
                op = row.operator("stepper.select_file_objects",
                                  icon="RESTRICT_SELECT_OFF")
                op.filepath = path
                sub = box.row()
                sub.enabled = not missing
                sub.label(text=path)

    classes = (
        STEP_OT_RefreshFile,
        STEP_OT_SelectFile,
        STEP_OT_ForgetFile,
        STEP_PT_ImportedFiles,
    )
