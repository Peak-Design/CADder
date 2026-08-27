# SPDX-License-Identifier: GPL-3.0-or-later
"""SW To Blender rig subpackage: builds a constrained armature from the
.rig.json manifest written by the Peak.SwToBlender SolidWorks add-in
(schema and semantics live in the SW-To-Blender repo, schema/SCHEMA.md).

Registered from STEPper NEXT's main.register(), guarded there so a rig
failure never costs STEP import. Importable without Blender — manifest.py
and graph.py run under plain Python in CI; the bpy-dependent classes only
exist inside Blender."""

from . import constraints, drivers, graph, loops  # noqa: F401
from . import manifest, matching, parenting, rig_build, ui  # noqa: F401

try:
    import bpy
except ImportError:
    bpy = None


if bpy is not None:

    class SwToBlenderSettings(bpy.types.PropertyGroup):
        manifest_path: bpy.props.StringProperty(
            name="Manifest",
            description="Path to the .rig.json written by Peak.SwToBlender",
            # Not FILE_PATH: that subtype draws its own UNFILTERED browse
            # button beside the panel's filtered one (swtb.pick_manifest),
            # and two file buttons with different behaviour reads as a bug.
            default="",
        )

    _classes = (SwToBlenderSettings,) + ui.classes

    def register():
        for cls in _classes:
            bpy.utils.register_class(cls)
        bpy.types.Scene.sw_to_blender = bpy.props.PointerProperty(
            type=SwToBlenderSettings)

    def unregister():
        # The pointer references the PropertyGroup class, so it must be
        # gone before the class it points at.
        del bpy.types.Scene.sw_to_blender
        for cls in reversed(_classes):
            bpy.utils.unregister_class(cls)

else:

    def register():
        raise RuntimeError("sw_to_blender.register() requires Blender (bpy)")

    def unregister():
        raise RuntimeError("sw_to_blender.unregister() requires Blender (bpy)")
