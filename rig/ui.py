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

import json
import os

from . import (graph, inputs, joining, manifest as manifest_mod, matching,
               parenting, pose_sync, rig_build)
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
    "join_report": None,
    # The import options of the last send (hierarchy, up axis, quality), so
    # an Update from CAD that asks for the whole assembly again lands it the
    # same way round and in the same shape.
    "import_options": None,
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


# ── The driver choice (inputs.py) ───────────────────────────────────────
#
# One dropdown per mechanism that offers more than one input. Picking one
# applies the candidate to the loaded manifest and, when a rig is standing,
# rebuilds it on the spot: the build is fast enough to try every input
# until the rig suits the job. The choice is remembered on the armature by
# a key that survives re-export (part paths, not ids), so a manifest sent
# again keeps it.

_ENUM_CACHE = {}       # mechanism index -> items (Blender needs them kept alive)
_SYNCING = [False]     # set while the panel state is being filled, not chosen
_CHOICE_TAG = "RIG_driver_choice"


def _driver_items(self, context):
    m = _STATE["manifest"]
    fallback = [("NONE", "(no choice)", "")]
    if m is None:
        return fallback
    mechs = inputs.mechanisms(m)
    if self.index >= len(mechs):
        return fallback
    cands = inputs.candidates(m, mechs[self.index])
    labels = [inputs.label(m, jid) for jid in cands]
    # Two pins of one link on one arm read the same; the joint id tells
    # them apart (live plunger.sldasm, 2026-09-15).
    items = [(jid, lab if labels.count(lab) == 1 else "{} ({})".format(lab, jid),
              "joint " + jid)
             for jid, lab in zip(cands, labels)]
    if not items:
        return fallback
    _ENUM_CACHE[self.index] = items
    return items


def _driver_changed(self, context):
    if _SYNCING[0] or bpy is None:
        return
    m = _STATE["manifest"]
    if m is None or self.driver == "NONE":
        return
    mechs = inputs.mechanisms(m)
    if self.index >= len(mechs):
        return
    if not inputs.apply(m, mechs[self.index], self.driver):
        return
    _STATE["plan"] = None
    if _find_rig(context) is None:
        return          # applied at the next Build Rig
    try:
        bpy.ops.cadlink.build_rig()
        if bpy.ops.cadlink.relink_geometry.poll():
            bpy.ops.cadlink.relink_geometry()
    except RuntimeError as exc:
        _STATE["error"] = str(exc)


def _stored_choices(context):
    arm = _find_rig(context)
    if arm is None:
        return set()
    try:
        return set(json.loads(arm.get(_CHOICE_TAG) or "[]"))
    except (TypeError, ValueError):
        return set()


def _store_choices(context, arm_obj):
    """Writes the current choices onto the rig, keyed by part paths."""
    m = _STATE["manifest"]
    if m is None or arm_obj is None:
        return
    keys = []
    for entry in context.scene.cad_link.mechanisms:
        if entry.driver and entry.driver != "NONE":
            k = inputs.key(m, entry.driver)
            if k:
                keys.append(k)
    arm_obj[_CHOICE_TAG] = json.dumps(keys)


def _sync_mechanisms(context, m):
    """Fills the panel's dropdowns for a freshly loaded manifest, applying
    any choice the standing rig remembers."""
    settings = context.scene.cad_link
    stored = _stored_choices(context)
    _SYNCING[0] = True
    try:
        settings.mechanisms.clear()
        _ENUM_CACHE.clear()
        for index, mech in enumerate(inputs.mechanisms(m)):
            cands = inputs.candidates(m, mech)
            if len(cands) < 2:
                continue
            for jid in cands:
                if inputs.key(m, jid) in stored:
                    inputs.apply(m, mech, jid)
                    break
            entry = settings.mechanisms.add()
            entry.index = index
            entry.name = "Mechanism {}".format(len(settings.mechanisms))
            cur = inputs.current(m, mech)
            if cur in cands:
                entry.driver = cur
    finally:
        _SYNCING[0] = False


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


def _cad_link_enabled(context):
    """True when the user has switched on the experimental CAD Link
    in the addon preferences.

    Gates the whole tab: without it the panel never polls true, so the
    "CAD Link" category does not appear in the sidebar at all. Fails
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

    class CADLINK_MechanismChoice(bpy.types.PropertyGroup):
        """One mechanism's input, as a dropdown of its candidate joints."""
        index: bpy.props.IntProperty(default=0)
        driver: bpy.props.EnumProperty(
            name="Input",
            description="The joint this mechanism is driven from. Changing "
                        "it rebuilds the rig",
            items=_driver_items,
            update=_driver_changed,
        )

    class CADLINK_OT_pick_manifest(bpy.types.Operator):
        bl_idname = "cadlink.pick_manifest"
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
            context.scene.cad_link.manifest_path = self.filepath
            # Picking a file means "use it": load immediately, the Load
            # button stays for re-reading a re-exported manifest.
            return bpy.ops.cadlink.load_manifest()

    class CADLINK_OT_load_manifest(bpy.types.Operator):
        bl_idname = "cadlink.load_manifest"
        bl_label = "Load Manifest"
        bl_description = "Parse and validate the rig manifest, plan the rig"

        def execute(self, context):
            path = bpy.path.abspath(context.scene.cad_link.manifest_path)
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
            _sync_mechanisms(context, m)
            self.report({"INFO"}, "Loaded {}: {} joints, {} groups, {} loops, "
                        "{} warnings".format(
                            os.path.basename(path), len(m.joints),
                            len(m.rigid_groups), len(m.loops), len(m.warnings)))
            return {"FINISHED"}

    class CADLINK_OT_import_step(bpy.types.Operator):
        bl_idname = "cadlink.import_step"
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
            # The normal STEPper import dialog, with the file filled in:
            # every option the user has (up axis, hierarchy, quality), seeded
            # from their preferences, instead of a fixed Z-up empties import
            # nobody asked for. The matcher reads the scene frame from the
            # geometry, so any up axis rigs correctly.
            result = bpy.ops.import_scene.occ_import_step(
                "INVOKE_DEFAULT", filepath=step_path)
            if "FINISHED" in result:
                self.report({"INFO"}, "Imported {}".format(os.path.basename(step_path)))
            return {"FINISHED"}

    class CADLINK_OT_match_geometry(bpy.types.Operator):
        bl_idname = "cadlink.match_geometry"
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

    class CADLINK_OT_sync_poses(bpy.types.Operator):
        bl_idname = "cadlink.sync_poses"
        bl_label = "Snap to CAD Poses"
        bl_description = ((
            "Move matched geometry onto the CAD transforms in the "
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

    class CADLINK_OT_build_rig(bpy.types.Operator):
        bl_idname = "cadlink.build_rig"
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
            _store_choices(context, result.armature_object)
            level = "INFO" if not result.warnings else "WARNING"
            self.report({level}, "Built {} bones, {} helpers, {} warnings".format(
                len(result.bone_names), len(result.helper_names),
                len(result.warnings)))
            return {"FINISHED"}

    class CADLINK_OT_relink_geometry(bpy.types.Operator):
        bl_idname = "cadlink.relink_geometry"
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

    class CADLINK_OT_join_rigs(bpy.types.Operator):
        bl_idname = "cadlink.join_rigs"
        bl_label = "Join Rigs"
        bl_description = ("Fold the other selected rigs into the active one, "
                          "hang each of their roots off one of its bones, and "
                          "re-parent the geometry")
        bl_options = {"REGISTER", "UNDO"}

        attach_bone: bpy.props.StringProperty(
            name="Attach To",
            description=("Bone of the active rig that the joined rigs ride. "
                         "Empty means its root, which is the machine's "
                         "ground"))

        @classmethod
        def poll(cls, context):
            host, others = joining.joinable(context)
            return host is not None and bool(others)

        def invoke(self, context, event):
            # A bone selected in the host is the obvious thing to hang the
            # subassembly off, so offer it rather than asking twice.
            host, _others = joining.joinable(context)
            if host is not None and not self.attach_bone:
                active = host.data.bones.active
                if active is not None and active.name in host.pose.bones:
                    self.attach_bone = active.name
            return context.window_manager.invoke_props_dialog(self)

        def draw(self, context):
            host, others = joining.joinable(context)
            layout = self.layout
            if host is None:
                return
            layout.label(text="Into: {}".format(host.name),
                         icon="ARMATURE_DATA")
            for other in others:
                layout.label(text="Joining: {}".format(other.name),
                             icon="PLUS")
            layout.prop_search(self, "attach_bone", host.pose, "bones")
            if not self.attach_bone:
                layout.label(text="Empty: hang them off the machine ground",
                             icon="INFO")

        def execute(self, context):
            host, others = joining.joinable(context)
            if host is None or not others:
                self.report({"ERROR"},
                            "Select the rigs to join, then the rig to join "
                            "them INTO last so it is the active object")
                return {"CANCELLED"}
            rig_build._ensure_object_mode(context)
            report = joining.join(context, host, others,
                                  attach_bone=self.attach_bone or None)
            _STATE["join_report"] = report

            for w in report.warnings:
                print("[CADLink join]", w)
            if report.drift:
                worst = max(d for _, d in report.drift)
                self.report({"WARNING"},
                            "Joined {} bone(s), but {} thing(s) moved (worst "
                            "{:.3f} mm) - see the console".format(
                                report.bones_added, len(report.drift),
                                worst * 1000.0))
                for name, d in report.drift[:10]:
                    print("[CADLink join] moved {:.6f} m: {}".format(d, name))
            elif report.warnings:
                self.report({"WARNING"}, report.warnings[0])
            else:
                self.report({"INFO"},
                            "Joined {} bone(s) onto {}, re-parented {} "
                            "object(s), nothing moved".format(
                                report.bones_added,
                                report.attached_to or "no bone",
                                report.reparented))
            return {"FINISHED"}

    def _linked_objects():
        """Every object that came in over CAD Link."""
        return [o for o in bpy.data.objects if o.get("RIG_component_id")]

    def _scope_objects(context, scope):
        """The objects an update covers. Selected parts is what is selected.
        Collection widens that to everything in the same collections, which
        is a subassembly in the tree modes. Whole assembly is every object
        of the same send."""
        selected = [o for o in context.selected_objects if o.get("RIG_component_id")]
        if scope == "SELECTED":
            return selected
        if scope == "COLLECTION":
            collections = set()
            for o in selected:
                collections.update(c.name for c in o.users_collection)
            if not collections and context.collection is not None:
                collections.add(context.collection.name)
            return [o for o in _linked_objects()
                    if any(c.name in collections for c in o.users_collection)]
        files = {o.get("SWMESH_file") for o in selected}
        files.discard(None)
        if not files:
            return _linked_objects()
        return [o for o in _linked_objects() if o.get("SWMESH_file") in files]

    def _apply_poses(context, reply):
        """Puts the CAD poses the reply carries into the manifest, then onto
        the scene: the rig is rebuilt first, which releases the geometry
        from its bones, the objects are moved onto the new poses, and the
        geometry goes back on the bones. Without a rig the objects simply
        move. Returns how many moved."""
        manifest = _STATE.get("manifest")
        entries = (reply or {}).get("components") or []
        by_id = {}
        for entry in entries:
            rows = entry.get("transform")
            if entry.get("id") and rows and len(rows) == 16:
                by_id[entry["id"]] = [list(rows[i * 4:i * 4 + 4]) for i in range(4)]
        if manifest is None or not by_id:
            return 0
        for component in manifest.components:
            rows = by_id.get(component.id)
            if rows is not None:
                component.transform = rows

        had_rig = any(o.get("RIG_rig") and o.type == "ARMATURE" for o in bpy.data.objects)
        if had_rig and bpy.ops.cadlink.build_rig.poll():
            bpy.ops.cadlink.build_rig()
        moved = 0
        if bpy.ops.cadlink.sync_poses.poll():
            bpy.ops.cadlink.sync_poses()
            report = _STATE.get("pose_report")
            moved = len(report.moved) if report is not None else 0
        if had_rig and bpy.ops.cadlink.relink_geometry.poll():
            bpy.ops.cadlink.relink_geometry()
        context.view_layer.update()
        return moved

    def _resend_everything(context):
        """Asks the CAD application to export the whole assembly again and
        runs it through the same stages a send does: the old import is
        replaced, the poses are synced, the rig is rebuilt from the new
        manifest and the geometry goes back on its bones. This is what
        catches parts added or removed and mates changed, which no update of
        the geometry alone can see.

        The import options of the last send are reused, so the assembly
        lands the same way round and in the same shape."""
        from . import cad_link, manifest as man_mod
        from .. import bridge
        out = {}
        try:
            reply = cad_link.request("export", mesh=True)
        except cad_link.CadLinkError as exc:
            return {"error": str(exc)}
        mesh = reply.get("mesh")
        manifest_path = reply.get("manifest")
        if not mesh:
            return {"error": "the CAD application sent no mesh"}
        if manifest_path:
            try:
                _STATE["manifest"] = man_mod.load(manifest_path)
            except (OSError, ManifestError) as exc:
                return {"error": "the new manifest could not be read: %s" % exc}
        options = _STATE.get("import_options") or {"hierarchy_types": "FLAT", "up_as": "ZPOS"}
        payload = {
            "step": None, "mesh": mesh, "manifest": manifest_path,
            "steps": {"import": False, "replace": True, "match": True,
                      "sync_poses": True, "build_rig": True, "relink": True,
                      "cleanup": True},
            "import_options": options,
        }
        result = bridge._run_job(payload)
        if not result.get("ok"):
            return {"error": result.get("error") or "the import failed"}
        stages = result.get("stages") or {}
        out["objects"] = (stages.get("mesh") or {}).get("objects", 0)
        rig = stages.get("rig")
        out["rig"] = ("%d bone(s)" % rig["bones"]) if rig else "no rig"
        return out

    class CADLINK_OT_update_from_cad(bpy.types.Operator):
        bl_idname = "cadlink.update_from_cad"
        bl_label = "Update from CAD"
        bl_description = ((
            "Ask the CAD application for the geometry of these parts again, at the "
            "quality set here. The addon swaps the new geometry in and keeps the "
            "pose, the materials and the rig"
        ))
        bl_options = {"REGISTER", "UNDO"}

        scope: bpy.props.EnumProperty(
            name="Scope",
            items=[
                ("SELECTED", "Selected parts", "The parts that are selected"),
                ("COLLECTION", "Collection", "Every part in the same collections as the selection"),
                ("WHOLE", "Whole assembly", "Every part of this send"),
            ],
            default="SELECTED")
        what: bpy.props.EnumProperty(
            name="What",
            items=[
                ("GEOMETRY", "Geometry", "The shape of the parts, at the quality below"),
                ("GEOMETRY_POSES", "Geometry and poses",
                 "The shape of the parts, and where they now sit in the CAD assembly. "
                 "The rig is rebuilt so its rest pose follows"),
                ("POSES", "Poses", "Only where the parts now sit in the CAD assembly"),
                ("EVERYTHING", "Everything",
                 "Ask the CAD application for the whole assembly again: parts added or "
                 "removed, mates changed, the rig rebuilt. Only the whole assembly, "
                 "whatever the scope says"),
            ],
            default="GEOMETRY")
        quality: bpy.props.FloatProperty(
            name="Quality", default=0.9, min=0.0, max=1.0, subtype="FACTOR",
            description=("Chord tolerance, relative to each part's own size: "
                         "0 is a coarse preview, 1 is a smooth close-up"))

        @classmethod
        def poll(cls, context):
            return bool(_linked_objects())

        def execute(self, context):
            from . import native_import, cad_link, progress
            ids = []
            for obj in _scope_objects(context, self.scope):
                cid = obj.get("RIG_component_id")
                if cid and cid not in ids:
                    ids.append(cid)
            if not ids:
                self.report({"WARNING"}, "Select parts that came in over CAD Link")
                return {"CANCELLED"}
            persistent = []
            for obj in _scope_objects(context, self.scope):
                pid = obj.get("SWMESH_persistent_id")
                if pid and pid not in persistent:
                    persistent.append(pid)
            changed, moved = [], 0
            # The CAD application takes seconds to minutes to answer, and
            # Blender holds still meanwhile, so the status bar says which
            # part of the update is running.
            said = progress.JobProgress(context, title="Update from CAD")
            try:
                if self.what == "EVERYTHING":
                    said.stage("asking the CAD application for the assembly", 0, 90)
                    stages = _resend_everything(context)
                    if stages.get("error"):
                        self.report({"ERROR"}, stages["error"])
                        return {"CANCELLED"}
                    self.report({"INFO"},
                                "Brought the whole assembly over again: {} object(s), {}"
                                .format(stages.get("objects", 0), stages.get("rig", "no rig")))
                    return {"FINISHED"}
                if self.what != "POSES":
                    said.stage("asking the CAD application for the geometry", 0, 60)
                    reply = cad_link.retessellate(ids, self.quality, persistent_ids=persistent)
                    said.stage("replacing the geometry", 60, 90, len(ids))
                    changed = native_import.refine(context, reply["mesh"])
                    if not changed:
                        self.report({"WARNING"},
                                    "The CAD application sent geometry for parts that "
                                    "are not in this scene")
                        return {"CANCELLED"}
                if self.what != "GEOMETRY":
                    said.stage("moving the parts to where the CAD has them", 90, 100)
                    moved = _apply_poses(
                        context, cad_link.poses(ids, persistent_ids=persistent))
            except cad_link.CadLinkError as exc:
                _STATE["error"] = str(exc)
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            except (OSError, ValueError) as exc:
                self.report({"ERROR"}, "Could not read what the CAD application sent: %s" % exc)
                return {"CANCELLED"}
            finally:
                said.close()
            if self.what == "POSES":
                self.report({"INFO"}, "Moved {} part(s) onto the CAD poses".format(moved))
            elif self.what == "GEOMETRY":
                self.report({"INFO"},
                            "Updated {} part(s) to {} triangles ({:.3g} m chord)"
                            .format(len(changed), reply.get("triangles", 0),
                                    reply.get("tolerance_m", 0.0)))
            else:
                self.report({"INFO"},
                            "Updated {} part(s) to {} triangles, moved {} onto the CAD poses"
                            .format(len(changed), reply.get("triangles", 0), moved))
            return {"FINISHED"}

    def _advanced(context):
        """True when the user asked for the STEP rig panel in the addon
        preferences. The direct send does the whole pipeline itself, so
        the panel is off by default and most users never see it."""
        addon = __package__.rpartition(".")[0] or __package__
        try:
            prefs = context.preferences.addons[addon].preferences
        except (AttributeError, KeyError):
            return False
        return bool(getattr(prefs, "cad_link_advanced", False))

    class CADLINK_PT_panel(bpy.types.Panel):
        """What a user needs while working: the link, and the one button
        that brings the CAD model over again."""

        bl_label = "CAD Link"
        bl_idname = "CADLINK_PT_panel"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "CAD Link"

        @classmethod
        def poll(cls, context):
            return _cad_link_enabled(context)

        def draw(self, context):
            layout = self.layout
            settings = context.scene.cad_link

            row = layout.row()
            try:
                from .. import bridge
                if bridge.is_running():
                    row.label(text="Listening on port {}".format(bridge.port()),
                              icon="PLUGIN")
                else:
                    row.label(text="Not listening", icon="UNLINKED")
            except Exception:
                pass

            # Update from CAD is the whole panel for most users: the parts
            # came over a direct send, and this asks the CAD application
            # for them again.
            selected = [o for o in context.selected_objects
                        if o.get("RIG_component_id")]
            if selected:
                tolerance = selected[0].get("SWMESH_tolerance_m")
                text = "{} part(s) selected".format(len(selected))
                if tolerance:
                    text += ", {:.3g} m chord".format(tolerance)
                layout.label(text=text, icon="MESH_DATA")
            layout.operator("cadlink.update_from_cad", icon="FILE_REFRESH")

            # The input of a mechanism is a real choice about this model,
            # so it stays in front of the user when there is one to make.
            m = _STATE["manifest"]
            if m is not None and settings.mechanisms:
                box = layout.box()
                box.label(text="Mechanism input", icon="CON_KINEMATIC")
                for entry in settings.mechanisms:
                    box.prop(entry, "driver", text=entry.name)

            host, others = joining.joinable(context)
            if others:
                box = layout.box()
                box.label(text="{} + {}".format(
                    host.name, ", ".join(o.name for o in others)),
                    icon="ARMATURE_DATA")
                box.operator("cadlink.join_rigs", icon="GROUP_BONE")

            if _STATE["error"]:
                err = layout.box()
                err.alert = True
                err.label(text=_STATE["error"], icon="ERROR")

    class CADLINK_PT_step(bpy.types.Panel):
        """The manifest and the rig pipeline, by hand. A direct send runs
        every one of these stages itself, so this panel is for a STEP
        import and for putting a stage right when one went wrong."""

        bl_label = "STEP Rig"
        bl_idname = "CADLINK_PT_step"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "CAD Link"
        bl_parent_id = "CADLINK_PT_panel"
        bl_options = {"DEFAULT_CLOSED"}

        @classmethod
        def poll(cls, context):
            return _cad_link_enabled(context) and _advanced(context)

        def draw(self, context):
            layout = self.layout
            settings = context.scene.cad_link

            row = layout.row(align=True)
            row.prop(settings, "manifest_path", text="")
            row.operator("cadlink.pick_manifest", text="", icon="FILEBROWSER")
            layout.operator("cadlink.load_manifest", icon="FILE_REFRESH")

            col = layout.column(align=True)
            col.operator("cadlink.import_step", icon="IMPORT")
            col.operator("cadlink.match_geometry", icon="VIEWZOOM")
            col.operator("cadlink.sync_poses", icon="SNAP_ON")
            col.operator("cadlink.build_rig", icon="ARMATURE_DATA")
            col.operator("cadlink.relink_geometry", icon="LINKED")

    class CADLINK_PT_info(bpy.types.Panel):
        """Everything the last import, match, pose sync and rig build had
        to say. Nothing here is a control: it is the record of what
        happened, in one place, closed until it is wanted."""

        bl_label = "Info"
        bl_idname = "CADLINK_PT_info"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "CAD Link"
        bl_parent_id = "CADLINK_PT_panel"
        bl_options = {"DEFAULT_CLOSED"}

        @classmethod
        def poll(cls, context):
            return _cad_link_enabled(context)

        def draw(self, context):
            layout = self.layout
            shown = False

            m = _STATE["manifest"]
            if m is not None:
                shown = True
                box = layout.box()
                box.label(text="Manifest v{}: {} joints, {} groups, {} loops".format(
                    m.manifest_version, len(m.joints), len(m.rigid_groups),
                    len(m.loops)), icon="FILE_TEXT")
                for w in m.warnings[:5]:
                    box.label(text="{}: {}".format(w.code, w.message), icon="ERROR")
                if len(m.warnings) > 5:
                    box.label(text="... {} more warnings".format(
                        len(m.warnings) - 5))

            report = _STATE["match_report"]
            if report is not None:
                shown = True
                box = layout.box()
                box.label(text="Matched {} / ambiguous {} / unmatched {}".format(
                    len(report.matched), len(report.ambiguous),
                    len(report.unmatched)), icon="VIEWZOOM")
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
                        box.label(text="... {} more".format(len(report.notes) - 5))

            preport_pose = _STATE["pose_report"]
            if preport_pose is not None and (preport_pose.moved
                                             or preport_pose.skipped):
                shown = True
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
                shown = True
                box = layout.box()
                box.label(text="Rig: {} bones, {} helpers".format(
                    len(build.bone_names), len(build.helper_names)),
                    icon="ARMATURE_DATA")
                for w in build.warnings[:5]:
                    box.label(text=w, icon="ERROR")

            jreport = _STATE.get("join_report")
            if jreport is not None and jreport.bones_added:
                shown = True
                box = layout.box()
                box.label(text="Joined {} bone(s) onto {}".format(
                    jreport.bones_added, jreport.attached_to or "no bone"),
                    icon="GROUP_BONE")
                if jreport.renamed:
                    box.label(text="{} renamed to keep names unique".format(
                        len(jreport.renamed)))
                for w in jreport.warnings[:3]:
                    box.label(text=w[:60], icon="ERROR")

            preport = _STATE["parent_report"]
            if preport is not None and preport.violations:
                shown = True
                box = layout.box()
                box.alert = True
                box.label(text="{} transform drift violations".format(
                    len(preport.violations)), icon="ERROR")
                for name, drift in preport.violations[:5]:
                    box.label(text="{}: {:.2e}".format(name, drift))
            if preport is not None and preport.posed_bones:
                shown = True
                box = layout.box()
                box.alert = True
                box.label(text="{} bone(s) off rest at re-link".format(
                    len(preport.posed_bones)), icon="ERROR")
                box.label(text="A constraint rejects the rest pose.")
                box.label(text="Check the limits of that joint against value_at_rest.")
                for name, off in preport.posed_bones[:5]:
                    box.label(text="{}: {:.4f}".format(name, off))

            if not shown:
                layout.label(text="Nothing has come over the link yet.",
                             icon="INFO")

    classes = (
        CADLINK_OT_pick_manifest,
        CADLINK_OT_load_manifest,
        CADLINK_OT_import_step,
        CADLINK_OT_match_geometry,
        CADLINK_OT_sync_poses,
        CADLINK_OT_build_rig,
        CADLINK_OT_relink_geometry,
        CADLINK_OT_join_rigs,
        CADLINK_OT_update_from_cad,
        CADLINK_PT_panel,
        CADLINK_PT_step,
        CADLINK_PT_info,
    )
else:
    classes = ()
