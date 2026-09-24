# SPDX-License-Identifier: GPL-3.0-or-later
"""Which import a part, a rig or a manifest belongs to.

A direct send from the CAD application is one import. Its name, the stem,
is the name of its files: "<document>_<configuration>" from CADder Bridge
1.2 on, and "<document>" before. Two configurations of one assembly are
two imports, side by side in one scene, each with its own rig (a user
exports five to ten configurations of each assembly, 2026-09-24).

The two imports hold the same parts: the same paths, the same persistent
ids and the same component ids. Only the stem tells them apart. So every
step that finds parts by those ids asks here first which import it works
on, and leaves the parts of the other imports alone.
"""

import os

TAG_FILE = "SWMESH_file"
TAG_DOCUMENT = "SWMESH_document"
TAG_CONFIGURATION = "SWMESH_configuration"
# On the armature of a send: the stem of the import it drives.
TAG_IMPORT = "RIG_import"

_MANIFEST_SUFFIXES = (".rig.json", ".json")


def stem_of(manifest):
    """The stem of the import a manifest describes, or None.

    The add-in names the manifest, the geometry and the STEP file of one
    send with one stem, and the manifest names its STEP file."""
    try:
        step = getattr(manifest, "step_file", None) or ""
    except ReferenceError:
        return None
    if step:
        return os.path.splitext(os.path.basename(step))[0] or None
    return stem_of_path(getattr(manifest, "source_path", None))


def stem_of_path(path):
    """The stem of a manifest file name, or None."""
    if not path:
        return None
    name = os.path.basename(str(path))
    low = name.lower()
    for suffix in _MANIFEST_SUFFIXES:
        if low.endswith(suffix):
            return name[:-len(suffix)] or None
    return os.path.splitext(name)[0] or None


def rig_stems(arm_obj):
    """The stems of the imports this rig drives. The tag of the build
    first. A rig built before the tag names its manifest, and the manifest
    name gives the stem. A joined rig can name several."""
    if arm_obj is None:
        return set()
    try:
        tagged = arm_obj.get(TAG_IMPORT)
    except ReferenceError:
        return set()
    if tagged:
        return {str(tagged)}
    from .parenting import rig_sources
    out = set()
    for source in rig_sources(arm_obj):
        stem = stem_of_path(source)
        if stem:
            out.add(stem)
    return out


def in_scene(stems, objects=None):
    """The stems of `stems` that name an import with parts in the file, or
    an empty set.

    A rig is kept to its own import only when that import is there to keep
    it to. A manifest that names another file than the geometry it came
    with (a test, or a manifest loaded by hand) gives a stem no part has,
    and every part would be left out. The old rule then holds: every part
    is in."""
    if not stems:
        return set()
    if objects is None:
        import bpy
        objects = bpy.data.objects
    found = set()
    for obj in objects:
        try:
            own = obj.get(TAG_FILE)
        except ReferenceError:
            continue
        if own is not None and str(own) in stems:
            found.add(str(own))
            if found == stems:
                break
    return found


def belongs(obj, stems):
    """True when the object may take part in work on the imports `stems`.

    A part of another direct send does not. An object with no stem (a
    part of a STEP import, or the user's own) keeps the old rule, and so
    does every object when the stems are not known."""
    if not stems:
        return True
    try:
        own = obj.get(TAG_FILE)
    except ReferenceError:
        return False
    return own is None or str(own) in stems


def rig_may_take(obj, stems):
    """True when a rig of the imports `stems` may put this object on a
    bone, as far as the import goes.

    As belongs(), except for a part that names the manifest of its rig
    (RIG_source): the Joints module of CADder Pro tags the parts it rigs
    so, also parts that came in over the link. The manifest decides for
    those (parenting._rig_maps), not the import."""
    if belongs(obj, stems):
        return True
    try:
        return bool(obj.get("RIG_source"))
    except ReferenceError:
        return False


def only(objects, stems):
    """The objects of `objects` that belong to the imports `stems`."""
    if isinstance(stems, str):
        stems = {stems}
    if not stems:
        return list(objects)
    return [o for o in objects if belongs(o, stems)]


def stems_of(scene, document, configuration):
    """The stems of the imports in `scene` of this document and this
    configuration, or None when the scene cannot say, which leaves every
    import in.

    From the tags of the top collections. An import from before 1.2 has no
    configuration: it is taken when the document has no import of this
    configuration."""
    if not document:
        return None
    exact, legacy = set(), set()
    want = _same_path_key(document)
    for coll in _collections_of(scene):
        try:
            if coll.get("SWMESH_role") != "top" or not coll.get(TAG_FILE):
                continue
            if _same_path_key(coll.get(TAG_DOCUMENT)) != want:
                continue
            own = coll.get(TAG_CONFIGURATION)
            if not own:
                legacy.add(str(coll[TAG_FILE]))
            elif configuration is None or str(own) == str(configuration):
                exact.add(str(coll[TAG_FILE]))
        except ReferenceError:
            continue
    return exact or legacy or None


def held(scene):
    """document path -> the configurations the imports of `scene` hold, as
    the CAD add-in asks it: which configurations does this Blender hold."""
    out = {}
    for coll in _collections_of(scene):
        try:
            if coll.get("SWMESH_role") != "top":
                continue
            document = coll.get(TAG_DOCUMENT)
            configuration = coll.get(TAG_CONFIGURATION)
        except ReferenceError:
            continue
        if document and configuration:
            names = out.setdefault(str(document), [])
            if str(configuration) not in names:
                names.append(str(configuration))
    return out


def _collections_of(scene):
    out, seen, stack = [], set(), [scene.collection]
    while stack:
        for child in stack.pop().children:
            key = child.as_pointer()
            if key not in seen:
                seen.add(key)
                out.append(child)
                stack.append(child)
    return out


def _same_path_key(path):
    if not path:
        return None
    return os.path.normcase(os.path.abspath(str(path)))


def configuration_of(obj):
    """(document path, configuration name) of a part, as the send tagged
    it. Either can be None: a part from before 1.2 has no configuration."""
    try:
        document = obj.get(TAG_DOCUMENT)
        configuration = obj.get(TAG_CONFIGURATION)
    except ReferenceError:
        return None, None
    return (str(document) if document else None,
            str(configuration) if configuration else None)
