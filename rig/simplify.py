# SPDX-License-Identifier: GPL-3.0-or-later
"""Which parts travel without their small features.

A bolt hole costs far more triangles than the plate it is in, and a model
going to a game engine rarely wants it: the bolts are modelled and the
holes are not visible. Leaving it out is therefore worth doing, and it is
NOT a property of the CAD document. The same hole matters in the part on
the cover of the manual and does not matter in the bracket behind it, and
only the person building the scene knows which is which.

So the switch lives here, on the part, and on the collection above it when
the assembly came in as a tree. A part inside a collection that is set to
simplify follows the collection and its own switch is shown greyed, with a
line saying where the setting came from. One rule decides, and it is
visible from the part.

The setting rides the request to the CAD application every time geometry is
asked for, so Rebuild from CAD gives back what the scene had rather than
undoing it. It also survives a rebuild of the whole assembly, which
replaces every object: the settings are taken first, by component, and put
back on what comes in.
"""

try:
    import bpy
except ImportError:                                    # pragma: no cover
    bpy = None


#: What the CAD application is told when nothing says otherwise.
DEFAULT_SIZE = 0.012


# -- Reading the setting ---------------------------------------------------

def _parents(scene):
    """Every collection of the scene mapped to the one above it. The scene's
    own collection is the root and has no parent."""
    parent = {}
    if scene is None:
        return parent
    stack = [scene.collection]
    while stack:
        here = stack.pop()
        for child in here.children:
            if child in parent:
                continue
            parent[child] = here
            stack.append(child)
    return parent


def _chain(collection, parents):
    """A collection and everything above it, nearest first."""
    chain = []
    at = collection
    guard = 0
    while at is not None and guard < 1000:
        guard += 1
        chain.append(at)
        at = parents.get(at)
    return chain


def _set(target):
    """The settings of an object or a collection, or None before the addon
    has registered them."""
    return getattr(target, "cad_simplify", None) if target is not None else None


def source_of(obj, scene=None):
    """The collection whose setting an object follows, or None when the
    object answers for itself.

    An object can sit in more than one collection and a collection can be
    linked in more than one place, so the nearest collection that is set
    wins: the level someone reached for last is the level they meant.
    """
    if bpy is None or obj is None:
        return None
    scene = scene or bpy.context.scene
    if scene is None:
        return None
    parents = _parents(scene)
    known = set(parents)
    known.add(scene.collection)
    best, depth = None, None
    for holder in obj.users_collection:
        if holder not in known:
            continue
        for step, collection in enumerate(_chain(holder, parents)):
            if collection is scene.collection:
                continue
            settings = _set(collection)
            if settings is None or not settings.enabled:
                continue
            if depth is None or step < depth:
                best, depth = collection, step
            break
    return best


def above(collection, scene=None):
    """The nearest collection ABOVE this one that is set to simplify, or
    None. What makes a collection's own switch read only."""
    if bpy is None or collection is None:
        return None
    scene = scene or bpy.context.scene
    if scene is None:
        return None
    parents = _parents(scene)
    for step, holder in enumerate(_chain(collection, parents)):
        if step == 0 or holder is scene.collection:
            continue
        settings = _set(holder)
        if settings is not None and settings.enabled:
            return holder
    return None


def settings_for(obj, scene=None):
    """What an object travels as: (enabled, size in metres, curved).

    A collection above it that is set to simplify decides for it. Otherwise
    it answers for itself.
    """
    if bpy is None or obj is None:
        return False, DEFAULT_SIZE, False
    holder = source_of(obj, scene)
    settings = _set(holder or obj)
    if settings is None:
        return False, DEFAULT_SIZE, False
    if holder is None and not settings.enabled:
        return False, settings.size, settings.curved
    return True, settings.size, settings.curved


def orders(objects, scene=None):
    """The simplify entries for a request: one per component, naming the
    size and whether curved faces are included.

    Several objects can carry one component id, because a multibody part
    arrives as one object per body. They are one piece of geometry to the
    CAD application, so the first that asks for anything decides.
    """
    rows, seen = [], set()
    for obj in objects or []:
        component = obj.get("RIG_component_id")
        if not component or component in seen:
            continue
        enabled, size, curved = settings_for(obj, scene)
        if not enabled or not size > 0.0:
            continue
        seen.add(component)
        rows.append({
            "component": component,
            "size_m": float(size),
            "curved": bool(curved),
        })
    return rows


# -- Keeping the setting across a whole rebuild ----------------------------

def snapshot(scene=None):
    """The settings of the scene, by component id and by collection name.

    Rebuilding the whole assembly replaces every object and every
    collection, so what the scene was holding has to be written down before
    it goes and put back on what arrives.
    """
    if bpy is None:
        return {"components": {}, "collections": {}}
    scene = scene or bpy.context.scene
    components, collections = {}, {}
    for obj in bpy.data.objects:
        component = obj.get("RIG_component_id")
        settings = _set(obj)
        if not component or settings is None or not settings.enabled:
            continue
        components[component] = {
            "size": settings.size, "curved": settings.curved}
    for collection in _parents(scene):
        settings = _set(collection)
        if settings is None or not settings.enabled:
            continue
        collections[collection.name] = {
            "size": settings.size, "curved": settings.curved}
    return {"components": components, "collections": collections}


def restore(taken, scene=None):
    """Puts a snapshot back. Returns how many objects and collections it
    reached. A part or a collection that is no longer in the assembly is
    simply not there to set, which is the right answer and not an error."""
    if bpy is None or not taken:
        return 0, 0
    scene = scene or bpy.context.scene
    components = taken.get("components") or {}
    collections = taken.get("collections") or {}
    objects_set = 0
    for obj in bpy.data.objects:
        row = components.get(obj.get("RIG_component_id"))
        settings = _set(obj)
        if row is None or settings is None:
            continue
        settings.enabled = True
        settings.size = row["size"]
        settings.curved = row["curved"]
        objects_set += 1
    collections_set = 0
    for collection in _parents(scene):
        row = collections.get(collection.name)
        settings = _set(collection)
        if row is None or settings is None:
            continue
        settings.enabled = True
        settings.size = row["size"]
        settings.curved = row["curved"]
        collections_set += 1
    return objects_set, collections_set


# -- The property group and the panels ------------------------------------

if bpy is not None:

    class CADLINK_SimplifySettings(bpy.types.PropertyGroup):
        """Set on a part, or on a collection to cover everything in it."""

        enabled: bpy.props.BoolProperty(
            name="Simplify",
            description="Ask the CAD application for this geometry without "
                        "its bolt holes and other small features. Nothing in "
                        "the CAD document is changed",
            default=False,
        )
        size: bpy.props.FloatProperty(
            name="Smaller Than",
            description="Set how wide a feature may be and still be left "
                        "out. Measured across the hole it makes in the face "
                        "it breaks into",
            default=DEFAULT_SIZE, min=0.0001, max=1.0, soft_max=0.05,
            subtype="DISTANCE", unit="LENGTH",
        )
        curved: bpy.props.BoolProperty(
            name="Curved Faces",
            description="Include a feature that breaks into a curved face, "
                        "such as a hole drilled into a boss. The face keeps "
                        "its own shape and the hole is covered over",
            default=False,
        )

    def draw_for(layout, target, inherited=None):
        """The three controls for one part or one collection. inherited is
        the collection that decides instead, when there is one."""
        settings = _set(target)
        if settings is None:
            return
        column = layout.column()
        column.use_property_split = True
        column.use_property_decorate = False
        if inherited is not None:
            # Color is never the only signal, so the row that says a setting
            # is out of reach carries the reason in words as well.
            column.label(text='Simplify is set on the collection "%s"'
                              % inherited.name, icon="OUTLINER_COLLECTION")
            body = column.column()
            body.enabled = False
            body.prop(settings, "enabled")
            body.prop(_set(inherited), "size")
            body.prop(_set(inherited), "curved")
            return
        column.prop(settings, "enabled")
        body = column.column()
        body.enabled = settings.enabled
        body.prop(settings, "size")
        body.prop(settings, "curved")

    class CADLINK_PT_simplify_object(bpy.types.Panel):
        """On the part, in the object properties, where a part's own
        settings live."""

        bl_label = "CAD Simplify"
        bl_idname = "CADLINK_PT_simplify_object"
        bl_space_type = "PROPERTIES"
        bl_region_type = "WINDOW"
        bl_context = "object"
        bl_options = {"DEFAULT_CLOSED"}

        @classmethod
        def poll(cls, context):
            return context.object is not None

        def draw(self, context):
            draw_for(self.layout, context.object,
                     source_of(context.object, context.scene))

    class CADLINK_PT_simplify_collection(bpy.types.Panel):
        """On the collection, which is how a whole subassembly is covered at
        once in the tree hierarchy modes."""

        bl_label = "CAD Simplify"
        bl_idname = "CADLINK_PT_simplify_collection"
        bl_space_type = "PROPERTIES"
        bl_region_type = "WINDOW"
        bl_context = "collection"
        bl_options = {"DEFAULT_CLOSED"}

        @classmethod
        def poll(cls, context):
            return context.collection is not None

        def draw(self, context):
            draw_for(self.layout, context.collection,
                     above(context.collection, context.scene))

    classes = (
        CADLINK_SimplifySettings,
        CADLINK_PT_simplify_object,
        CADLINK_PT_simplify_collection,
    )

    def register():
        bpy.types.Object.cad_simplify = bpy.props.PointerProperty(
            type=CADLINK_SimplifySettings)
        bpy.types.Collection.cad_simplify = bpy.props.PointerProperty(
            type=CADLINK_SimplifySettings)

    def unregister():
        del bpy.types.Collection.cad_simplify
        del bpy.types.Object.cad_simplify

else:                                                  # pragma: no cover
    classes = ()

    def register():
        pass

    def unregister():
        pass
