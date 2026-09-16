# SPDX-License-Identifier: GPL-3.0-or-later
"""CAD Link rig subpackage: builds a constrained armature from the
.rig.json manifest written by a CAD add-in (today: Peak.SwToBlender for
SolidWorks)
(schema and semantics live in the SW-To-Blender repo, schema/SCHEMA.md).

Registered from STEPper NEXT's main.register(), guarded there so a rig
failure never costs STEP import. Importable without Blender: manifest.py
and graph.py run under plain Python in CI. The bpy-dependent classes only
exist inside Blender."""

from . import constraints, drivers, graph, loops  # noqa: F401
from . import manifest, matching, parenting, rig_build, ui  # noqa: F401

try:
    import bpy
except ImportError:
    bpy = None


if bpy is not None:

    class CadLinkSettings(bpy.types.PropertyGroup):
        manifest_path: bpy.props.StringProperty(
            name="Manifest",
            description="Path to the .rig.json written by Peak.SwToBlender",
            # Not FILE_PATH: that subtype draws its own UNFILTERED browse
            # button beside the panel's filtered one (cadlink.pick_manifest),
            # and two file buttons with different behavior reads as a bug.
            default="",
        )
        # How fine Update from CAD asks the CAD application to tessellate.
        # The four names are the names of its own Export Options, so the
        # same name gives the same triangles over either route.
        update_quality: bpy.props.EnumProperty(
            name="Quality",
            description="How fine the CAD application cuts the parts that "
                        "Update from CAD brings over",
            items=ui.QUALITY_ITEMS,
            default="FINE",
        )
        update_quality_factor: bpy.props.FloatProperty(
            name="Chord",
            description="Chord tolerance against the size of each part: "
                        "0 is a coarse preview, 1 is a smooth close-up",
            default=0.75, min=0.0, max=1.0, subtype="FACTOR",
        )
        # One entry per mechanism that offers a choice of input
        # (inputs.py); filled on manifest load, drawn as dropdowns.
        mechanisms: bpy.props.CollectionProperty(type=ui.CADLINK_MechanismChoice)

    _classes = (ui.CADLINK_MechanismChoice, CadLinkSettings) + ui.classes

    def register():
        for cls in _classes:
            bpy.utils.register_class(cls)
        bpy.types.Scene.cad_link = bpy.props.PointerProperty(
            type=CadLinkSettings)

    def unregister():
        # The pointer references the PropertyGroup class, so it must be
        # gone before the class it points at.
        del bpy.types.Scene.cad_link
        for cls in reversed(_classes):
            bpy.utils.unregister_class(cls)

else:

    def register():
        raise RuntimeError("rig.register() requires Blender (bpy)")

    def unregister():
        raise RuntimeError("rig.unregister() requires Blender (bpy)")
