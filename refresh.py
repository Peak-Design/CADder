# SPDX-License-Identifier: GPL-3.0-or-later
"""Re-import a STEP file over the top of the one already in the scene, and
keep the arrangement the user built around it.

A refresh is not "delete and import again". Between the first import and the
refresh a user has been WORKING: they have dragged the assembly into a
collection of their own, ctrl-dragged a part into a second collection so it
shows up in two places, parented a light to a bracket, hidden the fasteners.
None of that lives in the STEP file, and all of it is lost by a naive
re-import. What this module does is write it down first and put it back
after.

Three things are recorded, and each one answers a real workflow:

  * The collections each object is linked into that the IMPORT did not
    create. That is the ctrl-drag case, and the reason the importer marks
    its own collections as it makes them (main._own_collection): after the
    fact there is no way to tell "Cad Curves" that the importer made from
    "Cad Curves" that a user made, and names collide freely.

  * Where the import's own root collections were LINKED. A user who drags
    the whole "part.hierarchy" into their "STEP" collection has rearranged
    nothing inside it, so no object has a user collection to restore, but
    the fresh import would still appear at the scene root instead of where
    they put it.

  * Per-object parenting to objects OUTSIDE the import, and the hide flags.

Identity across a re-import is a cascade, because none of the single keys
survives every edit. An object's own NAME first, which is what the user sees
and what the importer reproduces deterministically for an unchanged file. Then the CAD node index with the CAD name, which survives a rename. Then the
CAD name alone where it is unique, which survives a node index shifting
because a component was added earlier in the tree. What matches by none of
them is reported rather than guessed at: an object that has gone from the
file, or one that is new in it, is exactly the "the assembly structure
changed" signal worth showing a user.
"""

import json
import os

try:
    import bpy
except ImportError:
    bpy = None

FILE_PROP = "STEP_file"
ROLE_PROP = "STEP_role"

# Scene-level record of what was imported and how, so a refresh can reproduce
# the import rather than guess at its settings.
REGISTRY_PROP = "step_import_registry"


# ── the registry ────────────────────────────────────────────────────────────

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


def changed_on_disk(scene, path):
    """Whether the file has been written since it was imported. None when
    that cannot be told: no record, or the file is gone."""
    entry = _registry_read(scene).get(path)
    was = (entry or {}).get("stat")
    now = file_stat(path)
    if not was or not now:
        return None
    return [round(was[0], 3), was[1]] != [round(now[0], 3), now[1]]


# ── what is in the scene ────────────────────────────────────────────────────

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
    """Every STEP file this blend holds, newest-first by nothing in
    particular, sorted by path so the panel does not reorder itself."""
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
    """The import's own collections that are NOT inside another of its own.
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


# ── snapshot and restore ────────────────────────────────────────────────────

def snapshot(path):
    """Everything about the current arrangement that the file itself does not
    describe."""
    owned_cols = {c.name for c in file_collections(path)}
    own_objs = {o.name for o in file_objects(path)}

    # Objects of the user's that hang off this import, indexed by the part
    # they hang off. Deleting the part orphans them, and nothing else in the
    # scene records where they belonged.
    external_children = {}
    for obj in bpy.data.objects:
        if obj.parent is None or _own(obj, path):
            continue
        if obj.parent.name in own_objs:
            external_children.setdefault(obj.parent.name, []).append(obj.name)

    objects = {}
    for obj in file_objects(path):
        user_cols = [c.name for c in obj.users_collection
                     if c.name not in owned_cols]
        parent = obj.parent.name if (obj.parent is not None
                                     and obj.parent.name not in own_objs) else None
        objects[obj.name] = {
            "name": obj.name,
            "step_name": obj.get("STEP_name"),
            "uuid": obj.get("STEP_uuid"),
            "user_collections": user_cols,
            # Whether it was still inside the import's own hierarchy. If it
            # was, the fresh hierarchy is where it belongs and the user's
            # collections are EXTRA. If it was not, the user took it out and
            # their collections are the whole answer.
            "in_owned": any(c.name in owned_cols for c in obj.users_collection),
            "external_children": external_children.get(obj.name, []),
            "in_scene_root": [s.name for s in bpy.data.scenes
                              if obj.name in [o.name for o in s.collection.objects]],
            "parent": parent,
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
        }

    roots = {}
    for col in owned_roots(path):
        roots[col.get(ROLE_PROP) or col.name] = {
            "name": col.name,
            "parents": _parents_of(col),
        }

    # Collections of somebody else's nested INSIDE the import's own, such as a rig
    # collection put with the assembly it drives, or anything the user
    # dragged in. Removing the import unlinks them from their only home, and
    # a collection linked nowhere is gone from the scene, so where they sat
    # has to be written down before that happens. Keyed by the owner's ROLE,
    # which the re-import recreates. Its NAME may come back as name.001.
    external_collections = {}
    for col in file_collections(path):
        strays = [c.name for c in col.children if c.name not in owned_cols]
        if strays:
            external_collections.setdefault(
                col.get(ROLE_PROP) or col.name, []).extend(strays)

    return {"objects": objects, "roots": roots,
            "external_collections": external_collections}


def _surviving_home(col, owned):
    """The nearest place above `col` that this clear is NOT about to remove."""
    seen = set()
    cur = col
    while cur is not None and cur.name not in seen:
        seen.add(cur.name)
        nxt = None
        for kind, name in _parents_of(cur):
            if kind == "SCENE":
                scene = bpy.data.scenes.get(name)
                if scene is not None:
                    return scene.collection
            elif name not in owned:
                return bpy.data.collections.get(name)
            elif nxt is None:
                nxt = bpy.data.collections.get(name)
        cur = nxt
    scene = bpy.data.scenes[0] if bpy.data.scenes else None
    return scene.collection if scene is not None else None


def clear(path):
    """Removes the import: its objects and its OWN collections, and nothing
    else. A user collection that held them survives, empty."""
    removed_objs = 0
    for obj in file_objects(path):
        bpy.data.objects.remove(obj, do_unlink=True)
        removed_objs += 1

    # Anything of somebody else's nested inside first moves somewhere that
    # will still exist afterwards. Blender does not delete a child
    # collection along with its parent. It simply unlinks it, and a
    # collection linked nowhere is out of the scene. `restore` puts these
    # back inside. This is what keeps them ALIVE for it to find.
    owned = {c.name for c in file_collections(path)}
    for col in file_collections(path):
        for child in list(col.children):
            if child.name in owned:
                continue
            home = _surviving_home(col, owned)
            col.children.unlink(child)
            if home is not None and child.name not in [c.name for c in home.children]:
                home.children.link(child)

    removed_cols = 0
    for col in file_collections(path):
        bpy.data.collections.remove(col)
        removed_cols += 1
    return removed_objs, removed_cols


def _index(snap):
    """The three lookups the identity cascade uses."""
    by_name, by_uuid, by_step = {}, {}, {}
    for rec in snap["objects"].values():
        by_name[rec["name"]] = rec
        if rec.get("uuid") is not None:
            by_uuid[(rec["uuid"], rec.get("step_name"))] = rec
        by_step.setdefault(rec.get("step_name"), []).append(rec)
    return by_name, by_uuid, by_step


def restore(path, snap):
    """Puts the arrangement back onto the freshly imported objects.

    Returns (restored, unmatched_old, unmatched_new), the last two being the
    components that have gone from the file and the ones that are new in it.
    """
    by_name, by_uuid, by_step = _index(snap)
    used = set()
    restored = 0
    fresh = file_objects(path)
    owned_cols = {c.name for c in file_collections(path)}

    for obj in fresh:
        rec = by_name.get(obj.name)
        if rec is None or id(rec) in used:
            rec = by_uuid.get((obj.get("STEP_uuid"), obj.get("STEP_name")))
        if rec is None or id(rec) in used:
            same = [r for r in by_step.get(obj.get("STEP_name"), [])
                    if id(r) not in used]
            rec = same[0] if len(same) == 1 else None
        if rec is None or id(rec) in used:
            continue
        used.add(id(rec))
        restored += 1

        # Collections. Only when the user had put this object somewhere of
        # their own: an object still sitting in the import's own hierarchy
        # has nothing to restore and must keep the fresh placement.
        wanted = [bpy.data.collections.get(n) for n in rec["user_collections"]]
        wanted = [c for c in wanted if c is not None and c.name not in owned_cols]
        scenes = [bpy.data.scenes.get(n) for n in rec.get("in_scene_root") or []]
        scenes = [s for s in scenes if s is not None]
        if wanted or scenes:
            # An object still inside the import's own hierarchy keeps the
            # fresh placement and gains the user's collections on top: that
            # is what a ctrl-drag did, a second home and not a move. One the
            # user had taken OUT of the hierarchy is placed only where they
            # put it, or the fresh import would drag it back in.
            if not rec.get("in_owned"):
                for col in list(obj.users_collection):
                    col.objects.unlink(obj)
            for col in wanted:
                if obj.name not in col.objects:
                    col.objects.link(obj)
            for scene in scenes:
                if obj.name not in scene.collection.objects:
                    scene.collection.objects.link(obj)

        parent = bpy.data.objects.get(rec["parent"] or "")
        if parent is not None and parent.name not in {o.name for o in fresh}:
            world = obj.matrix_world.copy()
            obj.parent = parent
            obj.matrix_parent_inverse = parent.matrix_world.inverted()
            obj.matrix_world = world

        # Whatever of the user's hung off this part goes back onto it.
        for child_name in rec.get("external_children") or []:
            child = bpy.data.objects.get(child_name)
            if child is None or _own(child, path):
                continue
            world = child.matrix_world.copy()
            child.parent = obj
            child.matrix_parent_inverse = obj.matrix_world.inverted()
            child.matrix_world = world

        obj.hide_viewport = rec["hide_viewport"]
        obj.hide_render = rec["hide_render"]

    # The import's own roots go back where the user had put them.
    fresh_roots = {(c.get(ROLE_PROP) or c.name): c for c in owned_roots(path)}
    for role, rec in snap["roots"].items():
        col = fresh_roots.get(role)
        if col is None or not rec["parents"]:
            continue
        placed = False
        for kind, name in rec["parents"]:
            if kind == "SCENE":
                scene = bpy.data.scenes.get(name)
                if scene is not None and col.name not in [
                        c.name for c in scene.collection.children]:
                    scene.collection.children.link(col)
                    placed = True
            else:
                parent = bpy.data.collections.get(name)
                if parent is not None and col.name not in [
                        c.name for c in parent.children]:
                    parent.children.link(col)
                    placed = True
        if placed:
            # Drop the placement the fresh import chose, but only once the
            # recorded one is in: a collection linked nowhere is invisible.
            for scene in bpy.data.scenes:
                names = [c.name for c in scene.collection.children]
                if col.name in names and not any(
                        k == "SCENE" and n == scene.name for k, n in rec["parents"]):
                    scene.collection.children.unlink(col)

    # ...and whatever of the user's had been nested inside them goes back
    # in. `clear` parked these somewhere that would survive it. This is the
    # move that returns them, so a rig collection kept with its assembly
    # stays with it across a refresh.
    fresh_by_role = {(c.get(ROLE_PROP) or c.name): c
                     for c in file_collections(path)}
    for role, names in (snap.get("external_collections") or {}).items():
        owner = fresh_by_role.get(role)
        if owner is None:
            continue
        for name in names:
            child = bpy.data.collections.get(name)
            if child is None or _own(child, path):
                continue
            if child.name in [c.name for c in owner.children]:
                continue
            for kind, parent_name in _parents_of(child):
                if kind == "SCENE":
                    scene = bpy.data.scenes.get(parent_name)
                    if scene is not None:
                        scene.collection.children.unlink(child)
                else:
                    parent = bpy.data.collections.get(parent_name)
                    if parent is not None:
                        parent.children.unlink(child)
            owner.children.link(child)

    unmatched_old = [r["name"] for r in snap["objects"].values()
                     if id(r) not in used]
    unmatched_new = len(fresh) - restored
    return restored, unmatched_old, unmatched_new


# ── operators and panel ─────────────────────────────────────────────────────

if bpy is not None:

    class STEP_OT_RefreshFile(bpy.types.Operator):
        """Re-import this STEP file from disk, keeping the collections,
        parenting and visibility you arranged around it"""
        bl_idname = "stepper.refresh_file"
        bl_label = "Refresh from disk"
        bl_options = {"REGISTER", "UNDO"}

        filepath: bpy.props.StringProperty()

        def execute(self, context):
            from . import main as _main

            path = self.filepath
            if not os.path.isfile(bpy.path.abspath(path)):
                self.report({"ERROR"},
                            "Not on disk any more: %s" % path)
                return {"CANCELLED"}

            settings = settings_for(context.scene, path)
            if settings is None:
                self.report({"WARNING"},
                            "No record of how this file was imported. "
                            "Refreshing with the current defaults")
                settings = _main.import_defaults()

            snap = snapshot(path)
            n_obj, n_col = clear(path)

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

            restored, gone, added = restore(path, snap)
            record_import(context.scene, path, settings)

            msg = ("Refreshed %s: %d object(s) replaced, %d arrangement(s) "
                   "restored" % (os.path.basename(path), n_obj, restored))
            level = "INFO"
            if gone or added:
                level = "WARNING"
                msg += (". The assembly changed - %d component(s) gone, %d "
                        "new" % (len(gone), added))
                for name in gone[:10]:
                    print("[STEPper refresh] gone from the file: %s" % name)
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
