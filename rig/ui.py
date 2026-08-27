# SPDX-License-Identifier: GPL-3.0-or-later
"""N-panel and operators. All state lives in a module dict, not in scene
properties: Manifest/RigPlan/MatchReport are Python objects that cannot
survive a .blend round-trip, and pretending otherwise (pickling into an ID
property) breaks undo. Re-link works from scene tags alone, so a reopened
file only needs Load Manifest again.

Operators report through self.report and return CANCELLED on a bad
manifest. The panel itself only ever draws already-validated state, so a
broken file can never take the UI down with it.
"""

import os

from . import graph, manifest as manifest_mod, matching, parenting, pose_sync, rig_build
from .manifest import ManifestError

try:
    import bpy
except ImportError:
    bpy = None

_STATE = {
    "manifest": None,
    "plan": None,
    "match_report": None,
    "pose_report": None,
    "build": None,
    "parent_report": None,
    "error": "",
}


def _reset_state():
    _STATE["manifest"] = None
    _STATE["plan"] = None
    _STATE["match_report"] = None
    _STATE["pose_report"] = None
    _STATE["build"] = None
    _STATE["parent_report"] = None
    _STATE["error"] = ""


def _stepper_available():
    if bpy is None:
        return False
    op = getattr(getattr(bpy.ops, "import_scene", None), "occ_import_step", None)
    if op is None:
        return False
    try:
        op.get_rna_type()
        return True
    except Exception:
        return False


def _sw_integration_enabled(context):
    """True when the user has switched on the experimental SolidWorks
    integration in the addon preferences.

    Gates the whole tab: without it the panel never polls true, so the
    "SW To Blender" category does not appear in the sidebar at all. Fails
    closed, because a user who has not opted in should never see it.
    """
    if bpy is None:
        return False
    # __package__ is "<addon>.rig" here. The preferences live on the addon.
    addon = __package__.rpartition(".")[0] or __package__
    try:
        prefs = context.preferences.addons[addon].preferences
    except (AttributeError, KeyError):
        return False
    return bool(getattr(prefs, "enable_bridge", False))


def _find_rig(context):
    build = _STATE.get("build")
    if build is not None and build.armature_object is not None:
        try:
            if build.armature_object.name in context.scene.objects:
                return build.armature_object
        except ReferenceError:
            pass
    for obj in context.scene.objects:
        if obj.type == "ARMATURE" and obj.get("RIG_rig"):
            return obj
    return None


if bpy is not None:

    class SWTB_OT_pick_manifest(bpy.types.Operator):
        bl_idname = "swtb.pick_manifest"
        bl_label = "Browse for Manifest"
        bl_description = "Pick a .rig.json manifest and load it"

        filepath: bpy.props.StringProperty(subtype="FILE_PATH")
        # The plain FILE_PATH property offers no filtering. This operator's
        # file browser shows only rig manifests (plus bare .json for
        # hand-renamed ones).
        filter_glob: bpy.props.StringProperty(
            default="*.rig.json;*.json", options={"HIDDEN"})

        def invoke(self, context, event):
            context.window_manager.fileselect_add(self)
            return {"RUNNING_MODAL"}

        def execute(self, context):
            context.scene.sw_to_blender.manifest_path = self.filepath
            # Picking a file means "use it": load immediately, the Load
            # button stays for re-reading a re-exported manifest.
            return bpy.ops.swtb.load_manifest()

    class SWTB_OT_load_manifest(bpy.types.Operator):
        bl_idname = "swtb.load_manifest"
        bl_label = "Load Manifest"
        bl_description = "Parse and validate the rig manifest, plan the rig"

        def execute(self, context):
            path = bpy.path.abspath(context.scene.sw_to_blender.manifest_path)
            _reset_state()
            if not path or not os.path.isfile(path):
                _STATE["error"] = "Manifest file not found: {}".format(path)
                self.report({"ERROR"}, _STATE["error"])
                return {"CANCELLED"}
            try:
                m = manifest_mod.load(path)
                plan = graph.build(m)
            except ManifestError as exc:
                _STATE["error"] = str(exc)
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            _STATE["manifest"] = m
            _STATE["plan"] = plan
            self.report({"INFO"}, "Loaded {}: {} joints, {} groups, {} loops, "
                        "{} warnings".format(
                            os.path.basename(path), len(m.joints),
                            len(m.rigid_groups), len(m.loops), len(m.warnings)))
            return {"FINISHED"}

    class SWTB_OT_import_step(bpy.types.Operator):
        bl_idname = "swtb.import_step"
        bl_label = "Import STEP"
        bl_description = "Import the manifest's STEP file with STEPper NEXT"

        @classmethod
        def poll(cls, context):
            return _STATE["manifest"] is not None

        def execute(self, context):
            m = _STATE["manifest"]
            base = os.path.dirname(m.source_path or "")
            step_path = os.path.join(base, m.step_file).replace("\\", "/")
            if not os.path.isfile(step_path):
                self.report({"ERROR"},
                            "STEP file not found next to the manifest: "
                            "{}".format(step_path))
                return {"CANCELLED"}
            if not _stepper_available():
                self.report({"WARNING"},
                            "STEPper NEXT is not installed. Import {} "
                            "manually with any STEP importer, then run Match "
                            "Geometry".format(step_path))
                return {"CANCELLED"}
            # The manifest frame is Z-up. STEPper's default up axis is Y
            # (generic CAD), which would rotate the geometry out of the
            # manifest frame.
            bpy.ops.import_scene.occ_import_step(
                filepath=step_path, up_as="ZPOS", fw_as="YPOS")
            self.report({"INFO"}, "Imported {}".format(os.path.basename(step_path)))
            return {"FINISHED"}

    class SWTB_OT_match_geometry(bpy.types.Operator):
        bl_idname = "swtb.match_geometry"
        bl_label = "Match Geometry"
        bl_description = "Match manifest components to scene objects"

        @classmethod
        def poll(cls, context):
            return _STATE["manifest"] is not None

        def execute(self, context):
            report = matching.match(_STATE["manifest"])
            _STATE["match_report"] = report
            level = ("WARNING" if report.unmatched or report.notes
                     else "INFO")
            message = "Matched {}, ambiguous {}, unmatched {}. Frame: {}".format(
                len(report.matched), len(report.ambiguous),
                len(report.unmatched),
                matching.describe_frame(report.frame_rows))
            if report.hint:
                message += ": " + report.hint
            # What the matcher had to decide on thin evidence. Written only
            # when it could not check its own answer, so it belongs where the
            # answer is read and not only in the system console.
            for note in report.notes:
                message += ": " + note
            self.report({level}, message)
            return {"FINISHED"}

    class SWTB_OT_sync_poses(bpy.types.Operator):
        bl_idname = "swtb.sync_poses"
        bl_label = "Snap to SW Poses"
        bl_description = ((
            "Move matched geometry onto the SolidWorks transforms in the "
            "manifest. This fixes instances that the STEP file could only store"
            " at one shared pose. An example is a flexed subassembly beside its"
            " rigid twin"
        ))

        @classmethod
        def poll(cls, context):
            return (_STATE["manifest"] is not None
                    and _STATE["match_report"] is not None)

        def execute(self, context):
            report = pose_sync.sync(_STATE["manifest"], _STATE["match_report"])
            _STATE["pose_report"] = report
            context.view_layer.update()
            if report.moved:
                worst = max(d for _, d in report.moved)
                self.report({"INFO"},
                            "Moved {} object(s) onto their SW poses (largest "
                            "correction {:.1f} mm). {} already in place".format(
                                len(report.moved), worst * 1000.0,
                                report.already_ok))
            elif report.skipped:
                self.report({"WARNING"},
                            "Nothing moved. {} skipped (see console)".format(
                                len(report.skipped)))
            else:
                self.report({"INFO"}, "All {} matched objects already sit at "
                            "their SW poses".format(report.already_ok))
            return {"FINISHED"}

    class SWTB_OT_build_rig(bpy.types.Operator):
        bl_idname = "swtb.build_rig"
        bl_label = "Build Rig"
        bl_description = "Build the constrained armature from the manifest"

        @classmethod
        def poll(cls, context):
            return _STATE["manifest"] is not None

        def execute(self, context):
            m = _STATE["manifest"]
            # The scene frame comes from the last match, but only when at
            # least one anchor agreed with it. An unanchored frame is just
            # identity-by-default. Passing it through would pin the rig to
            # the world origin, where rig_build's own fallback (the 3D
            # cursor, same place the STEP import lands) is the better guess.
            mreport = _STATE.get("match_report")
            frame_rows = (mreport.frame_rows
                          if mreport is not None and mreport.frame_agree > 0
                          else None)
            try:
                # Re-planned on every build: the dependency pre-flight must
                # run against exactly the plan that gets built.
                plan = graph.build(m)
                result = rig_build.build(context, m, plan, frame_rows=frame_rows)
            except ManifestError as exc:
                _STATE["error"] = str(exc)
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            _STATE["plan"] = plan
            _STATE["build"] = result
            level = "INFO" if not result.warnings else "WARNING"
            self.report({level}, "Built {} bones, {} helpers, {} warnings".format(
                len(result.bone_names), len(result.helper_names),
                len(result.warnings)))
            return {"FINISHED"}

    class SWTB_OT_relink_geometry(bpy.types.Operator):
        bl_idname = "swtb.relink_geometry"
        bl_label = "Re-link Geometry"
        bl_description = "Parent matched geometry to the rig, preserving world transforms"

        @classmethod
        def poll(cls, context):
            return _find_rig(context) is not None

        def execute(self, context):
            arm_obj = _find_rig(context)
            report = parenting.relink(context, arm_obj)
            _STATE["parent_report"] = report
            if report.violations:
                self.report({"WARNING"},
                            "{} objects drifted past 1e-6 during parenting "
                            "(see panel)".format(len(report.violations)))
            elif report.posed_bones:
                self.report({"WARNING"},
                            "{} bone(s) sit off rest while relinking: a "
                            "constraint rejects the rest pose. Check that "
                            "joint's limits (console has names)".format(
                                len(report.posed_bones)))
            elif report.grounded:
                self.report({"INFO"},
                            "Parented {} objects to bones. {} more had no "
                            "bone of their own and now ride the ground, so "
                            "the assembly stays together".format(
                                report.bone_parented - len(report.grounded),
                                len(report.grounded)))
            else:
                self.report({"INFO"}, "Parented {} objects to bones".format(
                    report.bone_parented))
            return {"FINISHED"}

    class SWTB_OT_refine_selected(bpy.types.Operator):
        bl_idname = "swtb.refine_selected"
        bl_label = "Refine in SolidWorks"
        bl_description = ((
            "Ask SolidWorks to tessellate the selected parts again at a finer "
            "tolerance. The addon swaps the new geometry in and keeps the pose "
            "and the rig"
        ))
        bl_options = {"REGISTER", "UNDO"}

        quality: bpy.props.FloatProperty(
            name="Quality", default=0.9, min=0.0, max=1.0, subtype="FACTOR",
            description=("Chord tolerance, relative to each part's own size: "
                         "0 is a coarse preview, 1 is a smooth close-up"))

        @classmethod
        def poll(cls, context):
            return any(o.get("RIG_component_id") for o in context.selected_objects)

        def execute(self, context):
            from . import native_import, sw_link
            ids = []
            for obj in context.selected_objects:
                cid = obj.get("RIG_component_id")
                if cid and cid not in ids:
                    ids.append(cid)
            if not ids:
                self.report({"WARNING"}, "Select parts that came from SolidWorks")
                return {"CANCELLED"}
            try:
                reply = sw_link.retessellate(ids, self.quality)
                changed = native_import.refine(context, reply["mesh"])
            except sw_link.SwLinkError as exc:
                _STATE["error"] = str(exc)
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            except (OSError, ValueError) as exc:
                self.report({"ERROR"}, "Could not read the refined mesh: %s" % exc)
                return {"CANCELLED"}
            if not changed:
                self.report({"WARNING"},
                            "SolidWorks sent geometry for parts that are not "
                            "in this scene")
                return {"CANCELLED"}
            self.report({"INFO"},
                        "Refined {} part(s) to {} triangles ({:.3g} m chord)"
                        .format(len(changed), reply.get("triangles", 0),
                                reply.get("tolerance_m", 0.0)))
            return {"FINISHED"}

    class SWTB_PT_panel(bpy.types.Panel):
        bl_label = "SW To Blender"
        bl_idname = "SWTB_PT_panel"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "SW To Blender"

        @classmethod
        def poll(cls, context):
            return _sw_integration_enabled(context)

        def draw(self, context):
            layout = self.layout
            settings = context.scene.sw_to_blender

            try:
                from .. import bridge
                if bridge.is_running():
                    layout.label(text="SW bridge: listening on port {}".format(
                        bridge.port()), icon="PLUGIN")
            except Exception:
                pass

            # Only offered when the selection came from the direct link:
            # a part imported through STEP has no component to ask about.
            selected = [o for o in context.selected_objects
                        if o.get("RIG_component_id")]
            if selected:
                box = layout.box()
                box.label(text="{} part(s) selected".format(len(selected)),
                          icon="MESH_DATA")
                tolerance = selected[0].get("SWMESH_tolerance_m")
                if tolerance:
                    box.label(text="tessellated at {:.3g} m".format(tolerance))
                box.operator("swtb.refine_selected", icon="MOD_SMOOTH")

            row = layout.row(align=True)
            row.prop(settings, "manifest_path")
            row.operator("swtb.pick_manifest", text="", icon="FILEBROWSER")
            layout.operator("swtb.load_manifest", icon="FILE_REFRESH")

            if _STATE["error"]:
                box = layout.box()
                box.alert = True
                box.label(text=_STATE["error"], icon="ERROR")

            m = _STATE["manifest"]
            if m is not None:
                box = layout.box()
                box.label(text="Manifest v{}".format(m.manifest_version),
                          icon="CHECKMARK")
                box.label(text="{} joints, {} groups, {} loops".format(
                    len(m.joints), len(m.rigid_groups), len(m.loops)))
                for w in m.warnings[:5]:
                    box.label(text="{}: {}".format(w.code, w.message),
                              icon="ERROR")
                if len(m.warnings) > 5:
                    box.label(text="... {} more warnings".format(
                        len(m.warnings) - 5))

            col = layout.column(align=True)
            col.operator("swtb.import_step", icon="IMPORT")
            col.operator("swtb.match_geometry", icon="VIEWZOOM")
            col.operator("swtb.sync_poses", icon="SNAP_ON")
            col.operator("swtb.build_rig", icon="ARMATURE_DATA")
            col.operator("swtb.relink_geometry", icon="LINKED")

            report = _STATE["match_report"]
            if report is not None:
                box = layout.box()
                box.label(text="Matched {} / ambiguous {} / unmatched {}".format(
                    len(report.matched), len(report.ambiguous),
                    len(report.unmatched)))
                box.label(text="Frame: {}".format(
                    matching.describe_frame(report.frame_rows)),
                    icon="ORIENTATION_GLOBAL")
                for cid in report.unmatched[:10]:
                    box.label(text="unmatched: {}".format(cid), icon="X")
                if len(report.unmatched) > 10:
                    box.label(text="... {} more".format(len(report.unmatched) - 10))
                for cid, names in report.ambiguous[:10]:
                    box.label(
                        text="ambiguous: {} ({})".format(cid, ", ".join(names[:3])),
                        icon="QUESTION")
                if len(report.ambiguous) > 10:
                    box.label(text="... {} more".format(len(report.ambiguous) - 10))
                if report.notes:
                    box.label(text="Matched on thin evidence:", icon="INFO")
                    for note in report.notes[:5]:
                        box.label(text=note)
                    if len(report.notes) > 5:
                        box.label(text="... {} more".format(
                            len(report.notes) - 5))

            preport_pose = _STATE["pose_report"]
            if preport_pose is not None and (preport_pose.moved
                                             or preport_pose.skipped):
                box = layout.box()
                box.label(text="Poses: {} moved, {} in place, {} skipped".format(
                    len(preport_pose.moved), preport_pose.already_ok,
                    len(preport_pose.skipped)), icon="SNAP_ON")
                for name, dist in preport_pose.moved[:5]:
                    box.label(text="{} ({:.1f} mm)".format(name, dist * 1000.0))
                for name, reason in preport_pose.skipped[:5]:
                    box.label(text="{}: {}".format(name, reason), icon="ERROR")

            build = _STATE["build"]
            if build is not None:
                box = layout.box()
                box.label(text="Rig: {} bones, {} helpers".format(
                    len(build.bone_names), len(build.helper_names)),
                    icon="ARMATURE_DATA")
                for w in build.warnings[:5]:
                    box.label(text=w, icon="ERROR")

            preport = _STATE["parent_report"]
            if preport is not None and preport.violations:
                box = layout.box()
                box.alert = True
                box.label(text="{} transform drift violations".format(
                    len(preport.violations)), icon="ERROR")
                for name, drift in preport.violations[:5]:
                    box.label(text="{}: {:.2e}".format(name, drift))
            if preport is not None and preport.posed_bones:
                box = layout.box()
                box.alert = True
                box.label(text="{} bone(s) off rest at re-link".format(
                    len(preport.posed_bones)), icon="ERROR")
                box.label(text="A constraint rejects the rest pose.")
                box.label(text="Check the limits of that joint against value_at_rest.")
                for name, off in preport.posed_bones[:5]:
                    box.label(text="{}: {:.4f}".format(name, off))

    classes = (
        SWTB_OT_pick_manifest,
        SWTB_OT_load_manifest,
        SWTB_OT_import_step,
        SWTB_OT_match_geometry,
        SWTB_OT_sync_poses,
        SWTB_OT_build_rig,
        SWTB_OT_relink_geometry,
        SWTB_OT_refine_selected,
        SWTB_PT_panel,
    )
else:
    classes = ()
