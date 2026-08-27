# Import dialog UI, quality presets, preference seeding, drag & drop and
# folder batch import for STEPper NEXT.
#
# The import operator class lives in main.py (registration hub); this module
# holds the preset table, deflection resolution and the dialog draw code so
# main.py stops growing.

import json
import os

import bpy

STEP_EXTENSIONS = (".step", ".stp", ".st", ".iges", ".igs", ".brep", ".brp")

# Quality presets: name -> (linear deflection in METERS, angular in RADIANS).
# BALANCED reproduces the historical defaults (0.8 file units on a mm file).
QUALITY_PRESETS = {
    "DRAFT": (0.002, 0.6),
    "BALANCED": (0.0008, 0.5),
    "FINE": (0.0002, 0.25),
    "ULTRA": (0.00005, 0.1),
}

QUALITY_PRESET_ITEMS = [
    ("DRAFT", "Draft", "Fast preview quality (2 mm deflection)", 0),
    ("BALANCED", "Balanced", "Good default quality (0.8 mm deflection)", 1),
    ("FINE", "Fine", "High quality (0.2 mm deflection)", 2),
    ("ULTRA", "Ultra Fine", "Maximum quality (0.05 mm deflection)", 3),
    ("CUSTOM", "Custom", "Manually set linear/angular deflection", 4),
]

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
    "ang_deflection_rot", "lin_deflection_rel", "detail_level",
    "eng_materials", "uv_mode", "uv_normalize", "uv_split_closed",
    "box_uv_scale", "skip_construction", "import_curves",
    "group_in_collection", "separate_solids",
)


def save_last_used(op, prefs):
    """Persist the operator's current dialog options into the preferences.

    Written on every user-initiated import; lands on disk with Blender's
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
        print(f"STEPper: could not save import settings: {e}")


def _restore_last_used(op, prefs):
    try:
        stored = json.loads(prefs.last_import_settings)
    except (TypeError, ValueError):
        return
    if not isinstance(stored, dict):
        return
    for key, value in stored.items():
        if key not in PERSISTED_PROPS:
            continue
        # Assigning quality_preset marks it "set" for the session, which
        # would override the artist-friendly detail slider (see
        # make_deflection_spec); same rule as seeding below.
        if key == "quality_preset" and prefs.simpler_parameters:
            continue
        try:
            setattr(op, key, value)
        except (AttributeError, TypeError, ValueError):
            pass  # stale value for a changed/removed option


def make_deflection_spec(op, prefs):
    """Build a deflection spec dict from the operator's properties.

    Modes:
      legacy:    raw file-unit values (old lin_deflection/ang_deflection
                 semantics); used when the legacy props were explicitly set
                 (scripts, parity harness) so old callers are unaffected.
      detail:    simple-mode integer detail level (resolved by caller).
      physical:  linear deflection is a physical length in meters, converted
                 to file units per file once the unit scale is known.
    """
    legacy_set = (op.properties.is_property_set("lin_deflection")
                  or op.properties.is_property_set("ang_deflection"))
    if legacy_set:
        return {"mode": "legacy", "lin": op.lin_deflection, "ang": op.ang_deflection}
    # Artist-friendly mode: the dialog shows only the detail slider, so the
    # detail gate must come before the relative/preset modes (neither is
    # visible or editable in this mode). An explicitly passed quality_preset
    # (scripts, the background worker) still wins.
    preset_set = op.properties.is_property_set("quality_preset")
    if prefs.simpler_parameters and not preset_set:
        return {"mode": "detail", "detail": op.detail_level}
    if op.quality_preset != "CUSTOM":
        lin_m, ang = QUALITY_PRESETS[op.quality_preset]
    else:
        lin_m, ang = op.lin_deflection_len, op.ang_deflection_rot
    if getattr(op, "tessellation_relative", False):
        return {"mode": "relative", "lin": op.lin_deflection_rel, "ang": ang}
    return {"mode": "physical", "lin_m": lin_m, "ang": ang}


def resolve_deflections(spec, scale, calculate_detail_level):
    """Resolve a deflection spec to (lin_def, ang_def) in FILE UNITS.

    scale is meters-per-file-unit (known after the STEP header is read), so
    physical mode yields the same real-world deflection regardless of the
    file's unit system.
    """
    if spec is None:
        return 0.8, 0.5
    if spec["mode"] == "legacy":
        return spec["lin"], spec["ang"]
    if spec["mode"] == "detail":
        a_def, l_def = calculate_detail_level(spec["detail"])
        return l_def, a_def
    if spec["mode"] == "relative":
        return spec["lin"], spec["ang"]
    # physical
    lin_file_units = spec["lin_m"] / scale if scale > 0 else spec["lin_m"]
    return lin_file_units, spec["ang"]


def seed_from_prefs(op, prefs):
    """Seed dialog defaults from addon preferences, once per session.

    With "Remember import settings" on, the options saved by the last
    import (any session) win; otherwise the explicit defaults from the
    preferences are used. After the first invoke, the operator's own
    last-used values win (Blender keeps them for the session).
    """
    if op.bl_idname in _session_seeded:
        return
    _session_seeded.add(op.bl_idname)
    try:
        op.up_as = prefs.preferred_up_axis
        op.hierarchy_types = prefs.preferred_hierarchy
        # Assigning quality_preset marks it "set" for the whole session,
        # which would permanently override the artist-friendly detail
        # slider (see make_deflection_spec), so skip seeding it in that mode.
        if not prefs.simpler_parameters:
            op.quality_preset = prefs.default_quality_preset
    except (AttributeError, TypeError):
        pass
    if getattr(prefs, "remember_import_settings", False) \
            and prefs.last_import_settings:
        _restore_last_used(op, prefs)


def draw_import_dialog(op, layout, prefs):
    """Collapsible import dialog (Blender 4.1+ layout.panel API)."""
    layout.use_property_split = True
    layout.use_property_decorate = False

    header, body = layout.panel("stepper_geometry", default_closed=False)
    header.label(text="Geometry")
    if body:
        if prefs.simpler_parameters:
            body.prop(op, "detail_level")
        else:
            # Relative tessellation replaces the quality preset, so the
            # toggle lives here where its effect is visible.
            body.prop(op, "tessellation_relative")
            if op.tessellation_relative:
                body.prop(op, "lin_deflection_rel")
                body.prop(op, "ang_deflection_rot")
            else:
                body.prop(op, "quality_preset", text="Quality")
                if op.quality_preset == "CUSTOM":
                    body.prop(op, "lin_deflection_len")
                    body.prop(op, "ang_deflection_rot")

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
        body.prop(op, "uv_mode")
        sub = body.row()
        sub.active = op.uv_mode in {"SURFACE", "UNWRAP"}
        sub.prop(op, "uv_normalize")
        sub = body.row()
        sub.active = op.uv_mode == "BOX"
        sub.prop(op, "box_uv_scale")
        body.prop(op, "uv_split_closed")

    header, body = layout.panel("stepper_advanced", default_closed=True)
    header.label(text="Advanced")
    if body:
        body.prop(op, "skip_construction")
        body.prop(op, "import_curves")
        body.prop(op, "separate_solids")


class STEPPER_FH_step(bpy.types.FileHandler):
    """Viewport drag & drop for STEP files (single or multiple)."""
    bl_idname = "STEPPER_FH_step"
    bl_label = "STEP import (STEPper NEXT)"
    bl_import_operator = "import_scene.occ_import_step"
    bl_file_extensions = ";".join(STEP_EXTENSIONS)

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"


class STEPPER_OT_batch_import_folder(bpy.types.Operator):
    """Import every STEP/IGES/BREP file in a folder, each into its own
    collection, using the quality/scene defaults from the addon preferences"""
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
        if prefs.simpler_parameters:
            spec = {"mode": "detail",
                    "detail": context.scene.stepper.detail_level}
        else:
            lin_m, ang = QUALITY_PRESETS[prefs.default_quality_preset]
            spec = {"mode": "physical", "lin_m": lin_m, "ang": ang}

        # Content options follow the import dialog: its current defaults,
        # overlaid with the remembered last-used settings when enabled.
        opts = {"up_as": prefs.preferred_up_axis,
                "htypes": prefs.preferred_hierarchy,
                "apply_scale": True, "skip_construction": False,
                "uv_mode": "SURFACE", "uv_normalize": False,
                "uv_split_closed": True, "box_uv_scale": 1.0,
                "import_curves": False, "eng_materials": True,
                "group_in_collection": False, "separate_solids": False}
        if prefs.remember_import_settings and prefs.last_import_settings:
            try:
                stored = json.loads(prefs.last_import_settings)
            except (TypeError, ValueError):
                stored = {}
            if isinstance(stored, dict):
                for key in ("apply_scale", "skip_construction", "uv_mode",
                            "uv_normalize", "uv_split_closed",
                            "box_uv_scale", "import_curves",
                            "group_in_collection", "separate_solids",
                            "eng_materials"):
                    if key in stored:
                        opts[key] = stored[key]
                if "up_as" in stored:
                    opts["up_as"] = stored["up_as"]
                if "hierarchy_types" in stored:
                    opts["htypes"] = stored["hierarchy_types"]
        # This operator has always grouped each file, and the importer now
        # does it itself — accurately, from what it created, rather than by
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
