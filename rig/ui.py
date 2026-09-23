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
import re

from . import (graph, inputs, joining, manifest as manifest_mod, matching,
               parenting, pose_sync, rig_build, defeature)
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
    # an Rebuild from CAD that asks for the whole assembly again lands it the
    # same way round and in the same shape. The file keeps them too
    # (remember_import_options), because this state ends with the session.
    "import_options": None,
    # Which bone drove which parts, taken before an update moves the parts
    # on: what lets a rebuilt bone come back under the name an animation
    # already keys against (rig_update.py).
    "rig_snapshot": None,
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


# ── The import options of a send ─────────────────────────────────────────
#
# Refresh and Full Reimport ask the CAD application for the assembly again,
# and they must build it as the scene was built: the same up axis and the
# same tree. The session keeps the options of the last send, but a session
# ends. After a restart, a Refresh of a scene sent Y-up with parented
# empties came back Z-up and flat, and every part on the rig turned 90
# degrees. So each send also writes its options into the file.

_OPTIONS_TAG = "CADLINK_import_options"
# The add-in's own defaults, for a scene that tells nothing.
_DEFAULT_OPTIONS = {"up_as": "YPOS", "hierarchy_types": "EMPTIES"}
_UP_AXES = ("XPOS", "YPOS", "ZPOS")


def remember_import_options(scene, options):
    """Keeps the import options of a send, for this session and in the
    file."""
    options = dict(options or {})
    _STATE["import_options"] = options
    try:
        scene[_OPTIONS_TAG] = json.dumps(options)
    except (TypeError, ValueError, AttributeError) as exc:
        print("[CADLink] the import options were not saved in the file:", exc)


def _import_options(context, stem=None):
    """The import options to build the assembly `stem` with again.

    What the file says comes first. A file saved before the options were
    kept says nothing, so the up axis and the tree are then read off the
    scene itself. The last send of this session comes only after that,
    because it can be the send of another file opened in the same session.
    The add-in's defaults come last."""
    options = {}
    try:
        stored = json.loads(context.scene.get(_OPTIONS_TAG) or "{}")
    except (TypeError, ValueError):
        stored = {}
    if isinstance(stored, dict):
        options.update(stored)
    keys = ("up_as", "hierarchy_types")
    if all(options.get(k) for k in keys):
        return options
    found = _options_in_scene(stem)
    session = _STATE.get("import_options") or {}
    for key in keys:
        if not options.get(key):
            options[key] = (found.get(key) or session.get(key)
                            or _DEFAULT_OPTIONS[key])
    return options


def _options_in_scene(stem=None):
    """The up axis and the tree of a direct-link import, read off the
    scene: the frame its rig was built in or its parts were placed in, and
    the collections and empties it was built with. Returns only what it
    could read."""
    roots = [c for c in bpy.data.collections
             if c.get("SWMESH_file") is not None
             and c.get("SWMESH_role") in ("flat", "hierarchy")]
    if any(c.get("SWMESH_file") == stem for c in roots):
        roots = [c for c in roots if c.get("SWMESH_file") == stem]
    if not roots:
        return {}
    root = roots[0]
    stem = root.get("SWMESH_file")
    found = {"hierarchy_types": _hierarchy_of(root, stem)}
    up = _up_of_rig(stem) or _up_of_parts(stem)
    if up:
        found["up_as"] = up
    return found


def _hierarchy_of(root, stem):
    """The hierarchy option an import was built with, from what it left."""
    if root.get("SWMESH_role") == "flat":
        return "FLAT"
    roles = {c.get("SWMESH_role") for c in bpy.data.collections
             if c.get("SWMESH_file") == stem}
    if "components" in roles:
        return "COLLECTION_INSTANCES"
    if "node" in roles:
        return "TREE"
    # Parented empties, or a tree too shallow to hold a branch, where the
    # two options build the same scene.
    return "EMPTIES"


def _up_axis_of(rotation):
    """The up axis whose turn is this 3x3 rotation, or None."""
    from . import native_import
    for axis in _UP_AXES:
        want = native_import.up_frame(axis)
        if all(abs(rotation[i][j] - want[i][j]) < 1e-4
               for i in range(3) for j in range(3)):
            return axis
    return None


def _up_of_rig(stem):
    """The up axis of the frame the rig of this import was built in. The
    rig is built in the frame the parts were placed in."""
    counts = {}
    for obj in bpy.data.objects:
        if obj.get("SWMESH_file") != stem:
            continue
        holder = obj.parent
        while holder is not None and holder.type != "ARMATURE":
            holder = holder.parent
        if holder is not None and holder.get("RIG_rig"):
            counts[holder] = counts.get(holder, 0) + 1
    for arm in sorted(counts, key=counts.get, reverse=True):
        values = list(arm.get("RIG_frame") or [])
        if len(values) == 16:
            return _up_axis_of([values[i * 4:i * 4 + 3] for i in range(3)])
    return None


def _up_of_parts(stem):
    """The up axis most parts were placed with: the turn between where a
    part stands and the CAD pose it was placed at. A part that was moved
    since matches no axis and has no vote."""
    from mathutils import Matrix
    votes = {}
    for obj in bpy.data.objects:
        values = obj.get("SWMESH_transform")
        if obj.get("SWMESH_file") != stem or values is None \
                or len(values) != 16 or obj.get("SWMESH_prototype"):
            continue
        cad = Matrix([tuple(values[i * 4:i * 4 + 4]) for i in range(4)])
        try:
            turn = (obj.matrix_world.to_3x3().normalized()
                    @ cad.to_3x3().normalized().inverted())
        except ValueError:
            continue
        axis = _up_axis_of([list(row) for row in turn])
        if axis is not None:
            votes[axis] = votes.get(axis, 0) + 1
    return max(votes, key=votes.get) if votes else None


# ── Poses from the CAD application ──────────────────────────────────────
#
# The CAD application answers "poses" under the component ids of its
# CURRENT walk. Those ids are handed out per export, so they shift when a
# part is added or deleted in front of another, while the scene and its
# manifest keep the ids of the export they came from. Keyed by the id,
# a part took the pose of the part that now has its old number. So a row
# is matched by the persistent id first, which names the same occurrence
# after an edit. The id is only for a side that has no persistent id.


# The name the add-in gives the file of a geometry reply when the request
# names no file: cadlink-refine-<guid>.swmesh, in the temp folder.
_REPLY_MESH = re.compile(r"^cadlink-refine-[0-9a-fA-F]+\.swmesh$")


def refine_from_reply(context, reply):
    """Swaps in the geometry a reply to "retessellate" names, and deletes
    the reply's file once it is read. Returns the objects that changed.

    No request names a file for the answer, so the add-in writes one to
    the temp folder, and it is Blender's to delete: nothing else reads it.
    Kept, each rebuild left one there, hundreds of MB over a session at a
    fine quality. Only a file with the name the add-in gives such a reply
    is deleted."""
    from . import native_import
    path = reply["mesh"]
    try:
        return native_import.refine(context, path)
    finally:
        if _REPLY_MESH.match(os.path.basename(str(path or ""))):
            try:
                os.unlink(path)
            except OSError as exc:
                print("[CADLink] the geometry reply was not deleted:", exc)


class PosesNotApplied(Exception):
    """The poses of a reply could not be put on this scene. The message
    says why and what to do."""


def _pose_rows(entries):
    """(component id, persistent ids, 4x4 rows) for each row of a poses
    reply that carries a whole transform. A row can hold two persistent
    ids: its own, and the one it was asked for by (requested_id)."""
    out = []
    for entry in entries or []:
        values = entry.get("transform") if isinstance(entry, dict) else None
        if not values or len(values) != 16:
            continue
        pids = {p for p in (entry.get("sw_persistent_id"),
                            entry.get("requested_id")) if p}
        rows = [[float(v) for v in values[i * 4:i * 4 + 4]] for i in range(4)]
        out.append((entry.get("id") or "", pids, rows))
    return out


def match_pose_rows(entries, identities):
    """key -> the 4x4 rows of the reply row that stands for it.

    `identities` maps each key (a manifest component, a scene object) to
    its (persistent id, component id), either of which can be None. A row
    whose persistent id is the key's is its row. Otherwise the component
    id can pair them, but not when both sides have a persistent id (they
    disagree, so the id names another occurrence now) and not when the
    row's persistent id belongs to another key."""
    rows = _pose_rows(entries)
    by_pid, by_id = {}, {}
    for cid, pids, matrix in rows:
        for pid in pids:
            by_pid.setdefault(pid, matrix)
        if cid:
            by_id.setdefault(cid, (pids, matrix))
    known = {pid for pid, _cid in identities.values() if pid}
    out = {}
    for key, (pid, cid) in identities.items():
        if pid and pid in by_pid:
            out[key] = by_pid[pid]
            continue
        hit = by_id.get(cid) if cid else None
        if hit is None:
            continue
        pids, matrix = hit
        if pid and pids:
            continue
        if pids & known:
            continue
        out[key] = matrix
    return out


def poses_into_manifest(manifest, entries):
    """Writes the poses of a reply into the manifest's components. Returns
    how many took one."""
    if manifest is None:
        return 0
    found = match_pose_rows(
        entries, {c.id: (c.sw_persistent_id, c.id) for c in manifest.components})
    for component in manifest.components:
        rows = found.get(component.id)
        if rows is not None:
            component.transform = rows
    return len(found)


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
    _STATE["error"] = ""
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
    """True when the user has switched the SolidWorks Bridge on in the
    addon preferences.

    Gates the panels of the link: without it they never poll true, so
    nothing about the bridge appears in the sidebar. Fails closed, because
    a user who has not opted in should never see it.
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
        bl_label = "Browse for Manifest..."
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
        bl_options = {"REGISTER", "UNDO"}
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
        bl_description = "Import the manifest's STEP file with CADder"

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
                            "CADder is not installed. Import {} "
                            "manually with any STEP importer, then run Match "
                            "Geometry".format(step_path))
                return {"CANCELLED"}
            # The normal CADder import dialog, with the file filled in:
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
        bl_options = {"REGISTER", "UNDO"}
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
        bl_options = {"REGISTER", "UNDO"}
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

    class CADLINK_OT_lock_rig(bpy.types.Operator):
        """The lock lives on the armature, so it is saved with the file and
        a rig sent to somebody else arrives locked as it was left."""

        bl_idname = "cadlink.lock_rig"
        bl_label = "Lock Rig"
        bl_description = ("Keep this rig when the assembly is sent again, so "
                          "edits made to it by hand survive. The geometry is "
                          "still replaced and attached to it")
        bl_options = {"REGISTER", "UNDO"}

        @classmethod
        def poll(cls, context):
            if _find_rig(context) is None:
                cls.poll_message_set("No rig in this scene to lock")
                return False
            return True

        def execute(self, context):
            arm = _find_rig(context)
            locked = not rig_build.is_locked(arm)
            if locked:
                arm[rig_build.LOCK_TAG] = True
            elif rig_build.LOCK_TAG in arm.keys():
                del arm[rig_build.LOCK_TAG]
            self.report({"INFO"}, "%s is %s"
                        % (arm.name, "locked" if locked else "unlocked"))
            return {"FINISHED"}

    class CADLINK_OT_build_rig(bpy.types.Operator):
        bl_idname = "cadlink.build_rig"
        bl_label = "Build Rig"
        bl_options = {"REGISTER", "UNDO"}
        bl_description = "Build the constrained armature from the manifest"

        @classmethod
        def poll(cls, context):
            if _STATE["manifest"] is None:
                return False
            standing = rig_build.locked_rig(context)
            if standing is not None:
                cls.poll_message_set(
                    "%s is locked. Unlock it to build the rig again"
                    % standing.name)
                return False
            return True

        def execute(self, context):
            # An error stays in the panel only until this runs again.
            _STATE["error"] = ""
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
            except rig_build.RigLocked as exc:
                # Not an error: the user asked for this rig to stay.
                self.report({"INFO"}, str(exc))
                return {"CANCELLED"}
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
        bl_label = "Relink Geometry"
        bl_options = {"REGISTER", "UNDO"}
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

    def _scope_objects(context):
        """The parts a rebuild covers, taken from what is selected.

        Parts that are selected are what it covers. With no part selected
        it is the collection that is active in the outliner and every
        collection below it, so one level of the tree is rebuilt on its own
        and the root rebuilds the whole assembly. One rule, shared with
        every other button that works on parts, so there is nothing to set.
        """
        from .. import tools as tools_mod
        return tools_mod.scope_objects(
            context, lambda o: bool(o.get("RIG_component_id")))

    def _rig_holding(obj):
        """The CAD rig this object rides, directly or through the tree
        empties above it, or None."""
        holder = obj
        while holder is not None:
            parent = holder.parent
            if parent is not None and holder.parent_type == "BONE" \
                    and parent.type == "ARMATURE" and parent.get("RIG_rig"):
                return parent
            holder = parent
        return None

    def _frame_rows_of_rig(arm):
        """The CAD frame the rig was built in, as rows, or None."""
        values = list(arm.get("RIG_frame") or [])
        if len(values) != 16:
            return None
        return [[float(v) for v in values[i * 4:i * 4 + 4]] for i in range(4)]

    def _scene_frame_rows(context, obj):
        """The CAD frame the parts of the scene were placed in, as rows, or
        None when nothing says. The last match knows it in this session.
        Otherwise the up axis of the send does, for a part of the direct
        link: kept in the file, or read off the scene."""
        from . import native_import
        report = _STATE.get("match_report")
        if report is not None and report.frame_agree > 0 and report.frame_rows:
            return [list(r) for r in report.frame_rows]
        stem = obj.get("SWMESH_file")
        if not stem:
            return None
        try:
            stored = json.loads(context.scene.get(_OPTIONS_TAG) or "{}")
        except (TypeError, ValueError):
            stored = {}
        up = (stored.get("up_as") if isinstance(stored, dict) else None) \
            or _options_in_scene(stem).get("up_as")
        return native_import.up_frame(up) if up else None

    def _cad_world(rows, obj, unit_scale, frame):
        """Where `obj` stands when its component is at `rows` (a CAD pose,
        meters), in the frame `frame` (a 4x4 Matrix), with its own scale
        kept. None for a mirrored object, which no turn can put there."""
        from mathutils import Matrix
        cad = Matrix([tuple(r) for r in rows])
        local = obj.get("SWMESH_local")
        if local is not None and len(local) == 16:
            # A part inside a rigid subassembly: its place in the component.
            cad = cad @ Matrix([tuple(local[i * 4:i * 4 + 4]) for i in range(4)])
        cad.translation = cad.translation * unit_scale
        if obj.matrix_world.to_3x3().determinant() < 0.0:
            return None
        loc, rot, _scale = (frame @ cad).decompose()
        _loc, _rot, scale = obj.matrix_world.decompose()
        return Matrix.LocRotScale(loc, rot, scale)

    def _apply_poses(context, reply, strict=False):
        """Puts the CAD poses the reply carries on the parts of the scene.
        Returns how many parts moved.

        A part on a rig is moved by posing the rig: the bones go where the
        parts go, so every joint moves with its part and the rig stays the
        rig (its rest, its animation, the bones the user added and its
        place). Building the rig again from the manifest threw all of that
        away, and put each bone back where its joint was at the export. A
        rig that is locked is posed as well: posing does not change it.
        What the rig does not let a part reach is reported. Refresh is what
        brings a rig whose rest is the new pose.

        A part on no rig is simply moved. The poses also go into the
        manifest, so it says where the CAD has the parts.

        With `strict`, a reply that moves nothing because it names no part
        of the scene raises PosesNotApplied. Without it, that is 0."""
        from mathutils import Matrix
        entries = (reply or {}).get("components") or []
        manifest = _STATE.get("manifest")
        poses_into_manifest(manifest, entries)

        comps = manifest.component_by_id() if manifest is not None else {}
        objects = [o for o in _linked_objects() if not o.get("SWMESH_prototype")]

        def identity(obj):
            cid = obj.get("RIG_component_id") or None
            pid = obj.get("SWMESH_persistent_id")
            if not pid and cid in comps:
                pid = comps[cid].sw_persistent_id
            return pid or None, cid

        by_name = {o.name: o for o in objects}
        found = match_pose_rows(entries, {o.name: identity(o) for o in objects})
        report = pose_sync.PoseSyncReport()
        _STATE["pose_report"] = report
        if not found:
            if strict:
                raise PosesNotApplied(
                    "The CAD application sent no pose for a part in this "
                    "scene. Use Refresh to get the assembly again"
                    if entries else "The CAD application sent no poses")
            return 0

        unit_scale = 1.0 / matching._scene_scale()
        rigs, free = {}, []
        for name in sorted(found):
            obj = by_name[name]
            arm = _rig_holding(obj)
            rows = _frame_rows_of_rig(arm) if arm is not None else None
            if rows is None:
                rows = _scene_frame_rows(context, obj)
            if rows is None:
                report.skipped.append(
                    (name, "the CAD frame of this part is not known. Use Refresh"))
                continue
            if arm is None:
                free.append((obj, found[name], rows))
                continue
            target = _cad_world(found[name], obj, unit_scale,
                                arm.matrix_world @ Matrix([tuple(r) for r in rows]))
            if target is None:
                report.skipped.append((name, "mirrored instance: cannot repose "
                                       "without flipping the geometry"))
                continue
            rigs.setdefault(arm, {})[obj] = target

        for arm, targets in rigs.items():
            posed = parenting.pose_onto(context, arm, targets,
                                        scene_scale=1.0 / unit_scale)
            report.moved.extend(posed.moved)
            report.already_ok += posed.already_ok
            for name, metres in posed.held:
                report.skipped.append(
                    (name, "the rig keeps it %.1f mm from its CAD pose"
                     % (metres * 1000.0)))

        if free:
            # Pose sync with a manifest of these poses alone: the frame can
            # differ from part to part only in a scene that holds more
            # than one import, so each frame is its own pass.
            from types import SimpleNamespace
            by_frame = {}
            for obj, rows, frame in free:
                by_frame.setdefault(json.dumps(frame), []).append((obj, rows))
            for key, pairs in by_frame.items():
                stand_in = SimpleNamespace(components=[
                    SimpleNamespace(id=obj.name, transform=rows)
                    for obj, rows in pairs])
                matched = matching.MatchReport(frame_rows=json.loads(key))
                matched.matched = [
                    matching.MatchEntry(component_id=obj.name,
                                        object_name=obj.name, step=0,
                                        confidence="exact")
                    for obj, _rows in pairs]
                # The entries are keyed by object name, not by component
                # id, so pose sync must not check them against the tags.
                synced = pose_sync.sync(stand_in, matched,
                                        objects=[obj for obj, _rows in pairs],
                                        trust_names=True)
                report.moved.extend(synced.moved)
                report.already_ok += synced.already_ok
                report.skipped.extend(synced.skipped)
        context.view_layer.update()
        return len(report.moved)

    def _resend_everything(context, update=False, rig_mode=None):
        """Asks the CAD application to export the whole assembly again.

        This is what catches a part added or deleted in CAD and a mate that
        changed, which no update of the geometry alone can see.

        With `update`, the scene is brought up to date part by part: a part
        that is still there keeps its object, its mesh, its materials and
        its modifiers, and only moves and is re-tagged. `rig_mode` then says
        what happens to the rig (rig_update.MODES). Without it, the import
        is REPLACED, which is what a fresh send does, and everything done in
        Blender on those objects goes with the old ones.

        The import options of the last send are reused, so the assembly
        lands the same way round and in the same shape (_import_options
        finds them in a file from an earlier session too). So are the parts and
        collections the scene holds defeatured: the CAD application is told
        which they are, and after a replace the settings are put back on
        what arrives, because a replace takes every object and collection
        with it. An update keeps them, so it needs none of that."""
        from . import cad_link, manifest as man_mod, defeature
        from .. import bridge
        out = {}
        held = None if update else defeature.snapshot(context.scene)
        asked = defeature.orders(_linked_objects(), context.scene)
        try:
            reply = cad_link.request(
                "export", mesh=True, **({"defeature": asked} if asked else {}))
        except cad_link.CadLinkError as exc:
            return {"error": str(exc)}
        mesh = reply.get("mesh")
        manifest_path = reply.get("manifest")
        if not mesh:
            return {"error": "the CAD application sent no mesh"}
        # Limit mates the CAD application took out to read the assembly and
        # could not put back. The user is here, not at the CAD application,
        # so this is where they are told.
        out["limits"] = list(reply.get("limits_left_suppressed") or [])
        if manifest_path:
            try:
                _STATE["manifest"] = man_mod.load(manifest_path)
            except (OSError, ManifestError) as exc:
                return {"error": "the new manifest could not be read: %s" % exc}
        options = _import_options(
            context, os.path.splitext(os.path.basename(mesh))[0])
        payload = {
            "step": None, "mesh": mesh, "manifest": manifest_path,
            "steps": {"import": False, "replace": not update,
                      "update": update, "match": True,
                      "sync_poses": True, "build_rig": True, "relink": True,
                      "cleanup": True},
            "import_options": options,
        }
        if update and rig_mode:
            payload["rig_mode"] = rig_mode
        result = bridge._run_job(payload)
        if not result.get("ok"):
            return {"error": result.get("error") or "the import failed"}
        stages = result.get("stages") or {}
        out["objects"] = (stages.get("mesh") or {}).get("objects", 0)
        rig = stages.get("rig")
        out["rig"] = ("%d bone(s)" % rig["bones"]) if rig and "bones" in rig \
            else (rig.get("mode", "no rig") if rig else "no rig")
        changed = stages.get("update")
        if changed:
            out["changed"] = ("%d part(s) added, %d removed, %d moved, "
                              "%d re-tessellated, %d unchanged"
                              % (len(changed.get("added") or []),
                                 len(changed.get("removed") or []),
                                 len(changed.get("moved") or []),
                                 len(changed.get("reshaped") or []),
                                 changed.get("kept", 0)))
        out["defeature"] = ((0, 0) if held is None
                            else defeature.restore(held, context.scene))
        return out

    class CADLINK_OT_update_from_cad(bpy.types.Operator):
        # The idname stays as it was. It is what a keymap, a macro or
        # another addon calls, and a rename would break those for a label.
        bl_idname = "cadlink.update_from_cad"
        bl_label = "Rebuild from CAD"
        bl_description = ((
            "Ask the CAD application for the geometry of these parts again, at the "
            "quality set in Mesh Quality. The addon swaps the new geometry in and keeps the "
            "pose, the materials, the rig and which parts are defeatured"
        ))
        bl_options = {"REGISTER", "UNDO"}

        # One choice, not a row of switches. The first three ask the CAD
        # application for the parts THIS SCENE ALREADY HOLDS and change
        # nothing else about it. The last two ask for the ASSEMBLY, which is
        # the only way to see a part added or deleted and a mate changed,
        # and they differ in what they do with the scene that is standing.
        # Checkboxes would offer "neither", which does nothing, and would
        # read as though the last were the others together, which it is not
        # (Oscar, 2026-09-17).
        what: bpy.props.EnumProperty(
            name="Bring",
            items=[
                ("GEOMETRY", "Geometry",
                 "The shape of the parts, at the quality below. Nothing "
                 "moves, and the parts the scene holds stay the parts it "
                 "holds"),
                ("GEOMETRY_POSES", "Geometry and Poses",
                 "The shape of the parts, and where they now sit in the CAD "
                 "assembly. The addon poses the rig to move the parts there. "
                 "Still only the parts the scene already holds"),
                ("POSES", "Poses",
                 "Only where the parts now sit in the CAD assembly. The addon "
                 "poses the rig to move the parts there"),
                ("REFRESH", "Refresh",
                 "Ask the CAD application for the assembly ITSELF again and "
                 "bring the scene up to date part by part: parts added, "
                 "parts deleted, the tree and the poses. A part that is "
                 "still there keeps its object, its mesh, its materials and "
                 "its modifiers. Always the whole assembly, whatever is "
                 "selected"),
                ("EVERYTHING", "Full Reimport",
                 "Ask for the assembly again and build the scene from "
                 "nothing. Work done in Blender on these objects goes with "
                 "the old ones, so use Refresh unless the scene is wrong in "
                 "a way a refresh cannot put right"),
            ],
            default="GEOMETRY")
        rig: bpy.props.EnumProperty(
            name="Rig",
            description="What a Refresh does with the rig that is standing",
            items=[
                ("APPEND", "Add and Remove Bones",
                 "Keep the armature and bring its bones up to date. A body "
                 "made of the same parts keeps its bone name, so an "
                 "animation on it survives"),
                ("KEEP", "Keep",
                 "Leave the rig exactly as it is. The parts still attach to "
                 "it"),
                ("REGENERATE", "Build a New Rig",
                 "Throw the armature away and build one from the new "
                 "manifest"),
            ],
            default="APPEND")
        @classmethod
        def poll(cls, context):
            return bool(_linked_objects())

        def execute(self, context):
            from . import native_import, cad_link, progress, defeature
            # An error from an earlier run stays in the panel only until
            # this runs again: the CAD application can be there now.
            _STATE["error"] = ""
            ids = []
            covered = _scope_objects(context)
            for obj in covered:
                cid = obj.get("RIG_component_id")
                if cid and cid not in ids:
                    ids.append(cid)
            if not ids:
                self.report({"WARNING"}, "Select parts that came from the CAD application")
                return {"CANCELLED"}
            persistent = []
            split = False
            for obj in covered:
                pid = obj.get("SWMESH_persistent_id")
                if pid and pid not in persistent:
                    persistent.append(pid)
                # A part that came in as one object per solid body. The
                # geometry has to come back in the same pieces, so the CAD
                # application is told which it was.
                if native_import._BODY_SUFFIX.search(
                        str(obj.get("SWMESH_path") or "")):
                    split = True
            changed, moved = [], 0
            # The CAD application takes seconds to minutes to answer, and
            # Blender holds still meanwhile, so the status bar says which
            # part of the update is running.
            said = progress.JobProgress(context, title="Rebuild from CAD")
            try:
                if self.what in ("EVERYTHING", "REFRESH"):
                    update = self.what == "REFRESH"
                    said.stage("asking the CAD application for the assembly", 0, 90)
                    stages = _resend_everything(
                        context, update=update,
                        rig_mode=self.rig if update else None)
                    if stages.get("error"):
                        self.report({"ERROR"}, stages["error"])
                        return {"CANCELLED"}
                    parts, groups = stages.get("defeature") or (0, 0)
                    kept = (", {} part(s) and {} collection(s) still defeatured"
                            .format(parts, groups)) if parts or groups else ""
                    if update:
                        self.report({"INFO"}, "Refreshed from CAD: {}, {}"
                                    .format(stages.get("changed",
                                                       "nothing changed"),
                                            stages.get("rig", "no rig")))
                    else:
                        self.report({"INFO"},
                                    "Brought the whole assembly over again: {} object(s), {}{}"
                                    .format(stages.get("objects", 0),
                                            stages.get("rig", "no rig"), kept))
                    # Last, so the status bar shows it.
                    if stages.get("limits"):
                        self.report({"WARNING"},
                                    "These limit mates are still suppressed in "
                                    "SolidWorks: {}. Unsuppress them before you save"
                                    .format(", ".join(stages["limits"])))
                    return {"FINISHED"}
                if self.what != "POSES":
                    said.stage("asking the CAD application for the geometry", 0, 60)
                    # Which parts travel without their small features is
                    # this scene's decision, so it is sent every time. A
                    # rebuild that left it out would quietly put the holes
                    # back.
                    # The quality in Mesh Quality, the same settings that
                    # rebuild a part from a file, with its distance in
                    # metres.
                    reply = cad_link.retessellate(
                        ids, native_import.cad_quality(context.scene),
                        persistent_ids=persistent,
                        separate_solids=split or None,
                        paths=native_import.cad_paths(covered),
                        defeature=defeature.orders(covered, context.scene))
                    said.stage("replacing the geometry", 60, 90, len(ids))
                    changed = refine_from_reply(context, reply)
                    if not changed:
                        self.report({"WARNING"},
                                    "The CAD application sent geometry for parts that "
                                    "are not in this scene")
                        return {"CANCELLED"}
                if self.what != "GEOMETRY":
                    said.stage("moving the parts to where the CAD has them", 90, 100)
                    moved = _apply_poses(
                        context, cad_link.poses(ids, persistent_ids=persistent),
                        strict=True)
            except cad_link.CadLinkError as exc:
                _STATE["error"] = str(exc)
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            except PosesNotApplied as exc:
                _STATE["error"] = str(exc)
                self.report({"ERROR"}, str(exc))
                # New geometry is a change that stays, so it keeps its step
                # in the undo history.
                return {"CANCELLED"} if self.what == "POSES" else {"FINISHED"}
            except (OSError, ValueError) as exc:
                self.report({"ERROR"}, "Could not read what the CAD application sent: %s" % exc)
                return {"CANCELLED"}
            finally:
                said.close()
            pose_report = _STATE.get("pose_report")
            held = list(pose_report.skipped) if pose_report is not None else []
            if self.what == "POSES":
                if moved or held:
                    self.report({"INFO"}, "Moved {} part(s) onto the CAD poses"
                                .format(moved))
                else:
                    self.report({"INFO"}, "All {} part(s) are already at their "
                                "CAD poses".format(pose_report.already_ok))
            elif self.what == "GEOMETRY":
                self.report({"INFO"},
                            "Updated {} part(s) to {} triangles ({:.3g} m chord)"
                            .format(len(changed), reply.get("triangles", 0),
                                    reply.get("tolerance_m", 0.0)))
            else:
                self.report({"INFO"},
                            "Updated {} part(s) to {} triangles, moved {} onto the CAD poses"
                            .format(len(changed), reply.get("triangles", 0), moved))
            if self.what != "GEOMETRY" and held:
                # Last, so the status bar shows it.
                self.report({"WARNING"},
                            "{} part(s) did not move to the CAD pose. {}: {}"
                            .format(len(held), held[0][0], held[0][1]))
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

    class CADLINK_PT_bridge(bpy.types.Panel):
        """The rig, and what the live link does to it.

        Not "SolidWorks Bridge" any more. How fine the geometry is, and the
        button that asks for it again, are in Mesh Quality above, and
        whether the link is up is on the line under the name of the addon.
        What is left is the rig, which is what the link is for, and a name
        that stays right when a second CAD application arrives.
        """

        bl_label = "Rig"
        bl_idname = "CADLINK_PT_bridge"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "CADder"
        bl_order = 1002

        @classmethod
        def poll(cls, context):
            return _cad_link_enabled(context)

        def draw(self, context):
            layout = self.layout

            # The lock is not drawn. A send from the CAD application asks
            # what to do with the rig that is standing, so there is nothing
            # left for the lock to answer. The operator stays registered,
            # because the question could come back (Oscar, 2026-09-17).

            # Joining is offered only when there is something to join, and
            # the count is the whole message, so the button carries it.
            host, others = joining.joinable(context)
            if others:
                layout.operator("cadlink.join_rigs",
                                text="Join %d Rigs" % (len(others) + 1),
                                icon="GROUP_BONE")

            # An error belongs beside the button that produced it. Color
            # is never the only signal, so it carries an icon as well.
            if _STATE["error"]:
                box = layout.box()
                box.alert = True
                box.label(text=_STATE["error"], icon="ERROR")

    class CADLINK_PT_mechanism(bpy.types.Panel):
        """Which joint drives a mechanism.

        A real choice about this model, so it sits in the open, right
        under the button, and only when there is a choice to make.
        """

        bl_label = "Mechanism Input"
        bl_idname = "CADLINK_PT_mechanism"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "CADder"
        bl_parent_id = "CADLINK_PT_bridge"

        @classmethod
        def poll(cls, context):
            return (_cad_link_enabled(context)
                    and _STATE["manifest"] is not None
                    and bool(context.scene.cad_link.mechanisms))

        def draw(self, context):
            layout = self.layout
            layout.use_property_split = True
            layout.use_property_decorate = False
            for entry in context.scene.cad_link.mechanisms:
                layout.prop(entry, "driver", text=entry.name)

    class CADLINK_PT_step(bpy.types.Panel):
        """The manifest and the rig pipeline, by hand. A direct send runs
        every one of these stages itself, so this panel is for a STEP
        import and for putting a stage right when one went wrong."""

        bl_label = "STEP Rig"
        bl_idname = "CADLINK_PT_step"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "CADder"
        bl_parent_id = "CADLINK_PT_bridge"
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

            layout.separator()

            # The stages in the order they run, which is the order a user
            # repairs them in.
            col = layout.column(align=True)
            col.operator("cadlink.import_step", icon="IMPORT")
            col.operator("cadlink.match_geometry")
            col.operator("cadlink.sync_poses")
            col.operator("cadlink.build_rig")
            col.operator("cadlink.relink_geometry")

    class CADLINK_PT_info(bpy.types.Panel):
        """Everything the last import, match, pose sync and rig build had
        to say. Nothing here is a control: it is the record of what
        happened, in one place, closed until it is wanted."""

        bl_label = "Info"
        bl_idname = "CADLINK_PT_info"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "CADder"
        bl_parent_id = "CADLINK_PT_bridge"
        bl_options = {"DEFAULT_CLOSED"}

        @classmethod
        def poll(cls, context):
            return _cad_link_enabled(context)

        def draw(self, context):
            layout = self.layout

            # The line under the name of the addon says whether the link is
            # up. The port is the detail behind it, so it lives here.
            try:
                from .. import bridge
                if bridge.is_running():
                    layout.label(text="Listening on port %d" % bridge.port(),
                                 icon="PLUGIN")
            except Exception:
                pass

            selected = [o for o in context.selected_objects
                        if o.get("RIG_component_id")]
            if selected:
                tolerance = selected[0].get("SWMESH_tolerance_m")
                text = "%d part(s) selected" % len(selected)
                if tolerance:
                    text += ", %.3g m chord" % tolerance
                layout.label(text=text, icon="MESH_DATA")

            host, others = joining.joinable(context)
            if others:
                layout.label(text="%s + %s" % (
                    host.name, ", ".join(o.name for o in others)),
                    icon="ARMATURE_DATA")

            m = _STATE["manifest"]
            if m is not None:
                box = layout.box()
                box.label(
                    text="Manifest v%s: %d joints, %d groups, %d loops"
                    % (m.manifest_version, len(m.joints), len(m.rigid_groups),
                       len(m.loops)), icon="FILE_TEXT")
                for w in m.warnings[:5]:
                    box.label(text="%s: %s" % (w.code, w.message), icon="ERROR")
                if len(m.warnings) > 5:
                    box.label(text="... %d more warnings"
                              % (len(m.warnings) - 5))

            report = _STATE["match_report"]
            if report is not None:
                box = layout.box()
                box.label(
                    text="Matched %d / ambiguous %d / unmatched %d"
                    % (len(report.matched), len(report.ambiguous),
                       len(report.unmatched)), icon="VIEWZOOM")
                box.label(text="Frame: %s"
                          % matching.describe_frame(report.frame_rows),
                          icon="ORIENTATION_GLOBAL")
                for cid in report.unmatched[:10]:
                    box.label(text="unmatched: %s" % cid, icon="X")
                if len(report.unmatched) > 10:
                    box.label(text="... %d more" % (len(report.unmatched) - 10))
                for cid, names in report.ambiguous[:10]:
                    box.label(text="ambiguous: %s (%s)"
                              % (cid, ", ".join(names[:3])), icon="QUESTION")
                if len(report.ambiguous) > 10:
                    box.label(text="... %d more" % (len(report.ambiguous) - 10))
                if report.notes:
                    box.label(text="Matched on Thin Evidence", icon="INFO")
                    for note in report.notes[:5]:
                        box.label(text=note)
                    if len(report.notes) > 5:
                        box.label(text="... %d more" % (len(report.notes) - 5))

            pose_report = _STATE["pose_report"]
            if pose_report is not None and (pose_report.moved
                                            or pose_report.skipped):
                box = layout.box()
                box.label(text="Poses: %d moved, %d in place, %d skipped"
                          % (len(pose_report.moved), pose_report.already_ok,
                             len(pose_report.skipped)), icon="SNAP_ON")
                for name, dist in pose_report.moved[:5]:
                    box.label(text="%s (%.1f mm)" % (name, dist * 1000.0))
                for name, reason in pose_report.skipped[:5]:
                    box.label(text="%s: %s" % (name, reason), icon="ERROR")

            build = _STATE["build"]
            if build is not None:
                box = layout.box()
                box.label(text="Rig: %d bones, %d helpers"
                          % (len(build.bone_names), len(build.helper_names)),
                          icon="ARMATURE_DATA")
                for w in build.warnings[:5]:
                    box.label(text=w, icon="ERROR")

            jreport = _STATE.get("join_report")
            if jreport is not None and jreport.bones_added:
                box = layout.box()
                box.label(text="Joined %d bone(s) onto %s"
                          % (jreport.bones_added,
                             jreport.attached_to or "no bone"),
                          icon="GROUP_BONE")
                if jreport.renamed:
                    box.label(text="%d renamed to keep names unique"
                              % len(jreport.renamed))
                for w in jreport.warnings[:3]:
                    box.label(text=w[:60], icon="ERROR")

            preport = _STATE["parent_report"]
            if preport is not None and preport.violations:
                box = layout.box()
                box.alert = True
                box.label(text="%d transform drift violations"
                          % len(preport.violations), icon="ERROR")
                for name, drift in preport.violations[:5]:
                    box.label(text="%s: %.2e" % (name, drift))
            if preport is not None and preport.posed_bones:
                box = layout.box()
                box.alert = True
                box.label(text="%d bone(s) off rest at relink"
                          % len(preport.posed_bones), icon="ERROR")
                box.label(text="A constraint rejects the rest pose.")
                box.label(text="Check the joint limits against its rest value.")
                for name, off in preport.posed_bones[:5]:
                    box.label(text="%s: %.4f" % (name, off))

    classes = (
        CADLINK_OT_pick_manifest,
        CADLINK_OT_load_manifest,
        CADLINK_OT_import_step,
        CADLINK_OT_match_geometry,
        CADLINK_OT_sync_poses,
        CADLINK_OT_lock_rig,
        CADLINK_OT_build_rig,
        CADLINK_OT_relink_geometry,
        CADLINK_OT_join_rigs,
        CADLINK_OT_update_from_cad,
        CADLINK_PT_bridge,
        CADLINK_PT_mechanism,
        CADLINK_PT_step,
        CADLINK_PT_info,
    )
else:
    classes = ()
