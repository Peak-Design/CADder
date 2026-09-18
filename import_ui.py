# Import dialog UI, quality presets, preference seeding, drag & drop and
# folder batch import for CADder.
#
# The import operator class lives in main.py (registration hub). This module
# holds the preset table, deflection resolution and the dialog draw code so
# main.py stops growing.

import json
import os

import bpy

from . import quality as quality_mod
from . import uv as uv_mod

STEP_EXTENSIONS = (".step", ".stp", ".st", ".iges", ".igs", ".brep", ".brp")

# The quality names and what they cut to live in quality.py, shared with
# the Mesh Quality panel and the bridge.
QUALITY_PRESETS = quality_mod.PRESETS
QUALITY_PRESET_ITEMS = quality_mod.ITEMS

# The five quality settings, for the import dialog and for the scene's Mesh
# Quality settings alike. One definition, so the two cannot come to differ
# in a name, a default or a limit.
QUALITY_KW = {
    "quality_preset": dict(
        items=QUALITY_PRESET_ITEMS, name="Quality",
        description="How fine the parts are cut. A name cuts the same way "
                    "for a file import and for SolidWorks",
        default=quality_mod.DEFAULT),
    "lin_deflection_len": dict(
        name="Distance",
        description="Largest distance between the mesh and the true "
                    "surface, for Custom. A smaller distance gives more "
                    "polygons",
        unit="LENGTH", default=0.0008, min=0.000002, soft_max=0.01,
        precision=4),
    "ang_deflection_rot": dict(
        name="Angle",
        description="Largest angle one facet may turn through, for Custom "
                    "and Relative Tessellation. A smaller angle gives more "
                    "polygons",
        unit="ROTATION", default=0.5, min=0.002, max=1.5),
    "tessellation_relative": dict(
        name="Relative Tessellation",
        description="Cut to a share of the size of each feature instead of a "
                    "distance. Small parts keep their detail and large parts "
                    "do not explode the triangle count",
        default=False),
    "lin_deflection_rel": dict(
        name="Relative Distance",
        description="Largest distance between the mesh and the true "
                    "surface, as a share of the size of each feature. A file "
                    "import measures each edge, SolidWorks each body",
        default=quality_mod.DEFAULT_RELATIVE, min=0.00001, max=0.5,
        precision=4),
}

# Operators seeded from preferences this session (keyed by operator idname)
_session_seeded = set()

# Import dialog options saved after each import and restored next session
# when the "Remember import settings" preference is on. Deliberately NOT
# included: filepath/files/directory/override_file (per-call), the legacy
# lin_deflection/ang_deflection props (assigning them would switch the
# operator into legacy deflection mode), and material_database (follows the
# active_matdb preference, whose enum items may change between sessions).
PERSISTED_PROPS = (
    "up_as", "hierarchy_types", "custom_scale", "user_scale", "apply_scale",
    "tessellation_relative", "quality_preset", "lin_deflection_len",
    "ang_deflection_rot", "lin_deflection_rel",
    "eng_materials", "uv_mode", "uv_normalize", "uv_closed_seams",
    "uv_smart_distortion", "uv_smart_sharp", "uv_smart_split",
    "uv_unwrap_compound",
    "box_uv_scale", "uv_pack", "uv_pack_tiles",
    "uv_pack_margin", "tris_to_quads",
    "skip_construction", "import_curves",
    "group_in_collection", "separate_solids",
)


def save_last_used(op, prefs):
    """Persist the operator's current dialog options into the preferences.

    Written on every user-initiated import. Lands on disk with Blender's
    normal preferences save (auto on quit by default).
    """
    if not getattr(prefs, "remember_import_settings", False):
        return
    try:
        vals = {}
        for key in PERSISTED_PROPS:
            v = getattr(op, key)
            vals[key] = float(v) if isinstance(v, float) else v
        prefs.last_import_settings = json.dumps(vals)
    except (AttributeError, TypeError, ValueError) as e:
        print(f"CADder: could not save import settings: {e}")


def _restore_last_used(op, prefs):
    try:
        stored = json.loads(prefs.last_import_settings)
    except (TypeError, ValueError):
        return
    if not isinstance(stored, dict):
        return
    # Settings saved by an older version name the UV map in other terms.
    uv_mod.migrate_settings(stored)
    for key, value in stored.items():
        if key not in PERSISTED_PROPS:
            continue
        try:
            setattr(op, key, value)
        except (AttributeError, TypeError, ValueError):
            pass  # stale value for a changed/removed option


def make_deflection_spec(op, prefs):
    """Build a deflection spec dict from the operator's properties.

    Modes:
      legacy:    raw file-unit values (old lin_deflection/ang_deflection
                 semantics). Used when the legacy props were explicitly set
                 (scripts, parity harness) so old callers are unaffected.
      physical:  a quality name, or Custom: a distance in metres, converted
                 to file units once the unit scale is known.
      relative:  Relative Tessellation: a share of each feature's size.
    """
    legacy_set = (op.properties.is_property_set("lin_deflection")
                  or op.properties.is_property_set("ang_deflection"))
    if legacy_set:
        return {"mode": "legacy", "lin": op.lin_deflection, "ang": op.ang_deflection}
    return quality_mod.spec_of(op)


def resolve_deflections(spec, scale):
    """Resolve a deflection spec to (lin_def, ang_def) in FILE UNITS, or a
    share when the spec is relative.

    scale is meters-per-file-unit (known after the STEP header is read), so
    physical mode yields the same real-world deflection regardless of the
    file's unit system.
    """
    if spec is None:
        return 0.8, 0.5
    if spec["mode"] == "legacy":
        return spec["lin"], spec["ang"]
    lin, ang, _relative = quality_mod.resolve(spec, scale)
    return lin, ang


def seed_from_prefs(op, prefs):
    """Seed dialog defaults from addon preferences, once per session.

    With "Remember import settings" on, the options saved by the last
    import (any session) win. Otherwise the explicit defaults from the
    preferences are used. After the first invoke, the operator's own
    last-used values win (Blender keeps them for the session).
    """
    if op.bl_idname in _session_seeded:
        return
    _session_seeded.add(op.bl_idname)
    try:
        op.up_as = prefs.preferred_up_axis
        op.hierarchy_types = prefs.preferred_hierarchy
        op.quality_preset = prefs.default_quality_preset
    except (AttributeError, TypeError):
        pass
    if getattr(prefs, "remember_import_settings", False) \
            and prefs.last_import_settings:
        _restore_last_used(op, prefs)


def draw_uv_mode(owner, layout):
    """The UV Map dropdown and the settings that go with it.

    The import dialog and the UV panel show the same settings, so both draw
    them here. A setting the chosen mode does not use stays in place but
    inactive, so the layout does not move when the mode changes.
    """
    mode = owner.uv_mode
    layout.prop(owner, "uv_mode")
    sub = layout.row()
    sub.active = mode in uv_mod.SURFACE_MODES
    sub.prop(owner, "uv_normalize")
    sub = layout.row()
    sub.active = mode == "BOX"
    sub.prop(owner, "box_uv_scale")
    sub = layout.row()
    sub.active = mode == "SMART"
    sub.prop(owner, "uv_smart_distortion")
    sub = layout.row()
    sub.active = mode == "SMART"
    sub.prop(owner, "uv_smart_sharp")
    sub = layout.row()
    sub.active = mode == "SMART"
    sub.prop(owner, "uv_smart_split")
    sub = layout.row()
    sub.active = mode == "SMART"
    sub.prop(owner, "uv_unwrap_compound")
    sub = layout.row()
    sub.active = mode == "SURFACE" or mode in uv_mod.UNWRAP_MODES
    sub.prop(owner, "uv_closed_seams")


def draw_quality(owner, layout):
    """Quality, and what goes with it. The import dialog and the Mesh
    Quality panel show the same settings for every route, so both draw them
    here. A setting that does not apply stays in place but inactive, so the
    layout does not move."""
    relative = owner.tessellation_relative
    custom = owner.quality_preset == "CUSTOM"
    sub = layout.column()
    sub.active = not relative
    sub.prop(owner, "quality_preset", text="Quality")
    sub = layout.column()
    sub.active = custom and not relative
    sub.prop(owner, "lin_deflection_len")
    sub = layout.column()
    sub.active = custom or relative
    sub.prop(owner, "ang_deflection_rot")
    layout.prop(owner, "tessellation_relative")
    sub = layout.column()
    sub.active = relative
    sub.prop(owner, "lin_deflection_rel")


def draw_import_dialog(op, layout, prefs):
    """Collapsible import dialog (Blender 4.1+ layout.panel API)."""
    layout.use_property_split = True
    layout.use_property_decorate = False

    header, body = layout.panel("stepper_geometry", default_closed=False)
    header.label(text="Geometry")
    if body:
        draw_quality(op, body)

    header, body = layout.panel("stepper_scene", default_closed=False)
    header.label(text="Scene")
    if body:
        body.prop(op, "up_as", text="Up Axis")
        body.prop(op, "hierarchy_types", text="Hierarchy")
        body.prop(op, "group_in_collection")
        body.prop(op, "custom_scale")
        sub = body.row()
        sub.active = op.custom_scale
        sub.prop(op, "user_scale")
        body.prop(op, "apply_scale")

    header, body = layout.panel("stepper_materials", default_closed=True)
    header.label(text="Materials & UVs")
    if body:
        body.prop(op, "material_database", text="Material DB")
        body.prop(op, "eng_materials")
        draw_uv_mode(op, body)
        body.prop(op, "uv_pack")
        sub = body.row()
        sub.active = op.uv_pack == "UDIM"
        sub.prop(op, "uv_pack_tiles")
        sub = body.row()
        sub.active = op.uv_pack != "NONE"
        sub.prop(op, "uv_pack_margin")
        body.prop(op, "tris_to_quads")

    header, body = layout.panel("stepper_advanced", default_closed=True)
    header.label(text="Advanced")
    if body:
        body.prop(op, "skip_construction")
        body.prop(op, "import_curves")
        body.prop(op, "separate_solids")


class STEPPER_FH_step(bpy.types.FileHandler):
    """Viewport drag & drop for STEP files (single or multiple)."""
    bl_idname = "STEPPER_FH_step"
    bl_label = "STEP Import (CADder)"
    bl_import_operator = "import_scene.occ_import_step"
    bl_file_extensions = ";".join(STEP_EXTENSIONS)

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"


class STEPPER_OT_batch_import_folder(bpy.types.Operator):
    """Import every STEP, IGES and BREP file in a folder. Each file goes into its
    own collection. The addon uses the defaults from the preferences."""
    bl_idname = "stepper.batch_import_folder"
    bl_label = "Batch Import Folder"
    bl_options = {"REGISTER", "UNDO"}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")
    filter_glob: bpy.props.StringProperty(
        default="*.step;*.stp;*.st;*.iges;*.igs;*.brep;*.brp", options={"HIDDEN"})
    recursive: bpy.props.BoolProperty(
        name="Recursive",
        description="Also search all subfolders for CAD files",
        default=False,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            if self.recursive:
                found = []
                for root, _dirs, names in os.walk(self.directory):
                    found.extend(os.path.join(root, n) for n in names
                                 if n.lower().endswith(STEP_EXTENSIONS))
            else:
                # os.listdir instead of glob: folder names with glob
                # metacharacters ("Parts [rev2]") must not be treated as
                # patterns
                found = [os.path.join(self.directory, n)
                         for n in os.listdir(self.directory)
                         if n.lower().endswith(STEP_EXTENSIONS)]
        except OSError as e:
            self.report({"ERROR"}, f"Cannot read folder: {e}")
            return {"CANCELLED"}
        files = sorted(found)
        if not files:
            self.report({"WARNING"}, "No STEP files found in the folder")
            return {"CANCELLED"}

        from . import main as _main

        # Honor the user's preference defaults (quality, up axis, hierarchy)
        # rather than the raw file-unit fallback deflections.
        prefs = _main._get_addon_prefs()
        spec = quality_mod.spec(prefs.default_quality_preset)

        # Content options follow the import dialog: its current defaults,
        # overlaid with the remembered last-used settings when enabled.
        opts = {"up_as": prefs.preferred_up_axis,
                "htypes": prefs.preferred_hierarchy,
                "apply_scale": True, "skip_construction": False,
                "uv_mode": "SURFACE", "uv_normalize": False,
                "uv_closed_seams": "SINGLE", "box_uv_scale": 1.0,
                "uv_smart_distortion": _main.UV_SMART_DISTORTION,
                "uv_smart_sharp": False, "uv_smart_split": True,
                "uv_unwrap_compound": True,
                "tris_to_quads": True, "uv_pack": "NONE",
                "uv_pack_tiles": 4, "uv_pack_margin": 0.005,
                "import_curves": False, "eng_materials": True,
                "group_in_collection": False, "separate_solids": False}
        if prefs.remember_import_settings and prefs.last_import_settings:
            try:
                stored = json.loads(prefs.last_import_settings)
            except (TypeError, ValueError):
                stored = {}
            if isinstance(stored, dict):
                uv_mod.migrate_settings(stored)
                for key in ("apply_scale", "skip_construction", "uv_mode",
                            "uv_normalize", "uv_closed_seams",
                            "uv_smart_distortion", "uv_smart_sharp",
                            "uv_smart_split", "uv_unwrap_compound",
                            "box_uv_scale", "tris_to_quads", "uv_pack",
                            "uv_pack_tiles", "uv_pack_margin",
                            "import_curves",
                            "group_in_collection", "separate_solids",
                            "eng_materials"):
                    if key in stored:
                        opts[key] = stored[key]
                if "up_as" in stored:
                    opts["up_as"] = stored["up_as"]
                if "hierarchy_types" in stored:
                    opts["htypes"] = stored["hierarchy_types"]
        # This operator has always grouped each file, and the importer now
        # does it itself, accurately, from what it created, rather than by
        # diffing the scene collection afterwards and sweeping up whatever
        # else happened to be there.
        opts["group_in_collection"] = True

        wm = context.window_manager
        wm.progress_begin(0, len(files))
        n_ok = 0
        try:
            for i, path in enumerate(files):
                wm.progress_update(i)

                result = _main.load_step(
                    context, path,
                    deflection_spec=spec,
                    **opts,
                )
                if result is False:
                    self.report({"WARNING"},
                                f"Could not import {os.path.basename(path)}")
                    continue
                n_ok += 1
        finally:
            wm.progress_end()
        self.report({"INFO"}, f"Imported {n_ok}/{len(files)} STEP files")
        return {"FINISHED"}


# Blender classes contributed by this module (concatenated in main.register)
classes = (STEPPER_FH_step, STEPPER_OT_batch_import_folder)
