# SPDX-License-Identifier: GPL-3.0-or-later
"""CAD Link bridge: a localhost HTTP endpoint a CAD add-in (today: SW To
Blender for SolidWorks) drives directly: export in the CAD application,
geometry + rig appear in Blender with no file dialogs in between.

Threading contract: the HTTP server lives on a daemon thread and NEVER
touches bpy. It enqueues jobs. A bpy.app.timers pump executes them on the
main thread and signals the waiting handler, which then writes the HTTP
response. Everything bpy happens on the main thread, always.

Discovery: on start the server binds 127.0.0.1 on an ephemeral port and
writes %LOCALAPPDATA%/PeakDesign/CADder/bridge/<pid>.json with the
port and a random token. The CAD side lists that directory, pings
each entry, and prunes the corpses. Every request must carry the token in
X-CADLink-Token: the file is user-readable only, so possession proves the
caller is the same desktop user.

Endpoints:
  GET  /cadlink/ping    -> instance info (fast, main thread not involved)
  POST /cadlink/import  -> full pipeline job, synchronous (import STEP,
                           load manifest, match, snap poses, build rig,
                           parent, tidy leftovers: each stage optional)
"""

import atexit
import json
import os
import queue
import secrets
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import bpy
except ImportError:
    bpy = None

_JOB_TIMEOUT_S = 30 * 60
_PUMP_INTERVAL_S = 0.2

# Importer options a job may forward verbatim to occ_import_step: every
# property the operator has, less the file and UI ones the bridge fills in
# itself. Anything else in the payload's import_options is reported back
# as ignored, not silently dropped.
#
# ci/bridge_smoke.py holds this set to the operator in BOTH directions. An
# option the operator lost stays here and is forwarded to nothing; an
# option the operator gained and this set lacks is dropped in silence,
# which is how the UV rework of 2026-09-14 left the bridge unable to pass
# the smart-unwrap, packing and tris-to-quads settings for a while.
_IMPORT_OPTION_KEYS = {
    "up_as", "fw_as", "hierarchy_types", "quality_preset",
    "custom_scale", "user_scale", "apply_scale",
    "lin_deflection", "ang_deflection", "lin_deflection_len",
    "ang_deflection_rot", "tessellation_relative", "lin_deflection_rel",
    "uv_mode", "uv_normalize", "uv_closed_seams", "uv_smart_distortion",
    "uv_smart_sharp", "uv_smart_split", "uv_unwrap_compound", "uv_pack", "uv_pack_tiles",
    "uv_pack_margin", "box_uv_scale", "tris_to_quads",
    "eng_materials", "material_database", "import_curves",
    "skip_construction", "group_in_collection", "separate_solids",
}

_state = {
    "server": None,
    "thread": None,
    "port": None,
    "token": None,
    "registry_path": None,
    "queue": None,       # queue.Queue of _Job
    "timer_running": False,
    "last_job": None,    # summary dict for the UI
}


def registry_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "PeakDesign", "CADder", "bridge")


def _addon_version() -> str:
    try:
        from . import bl_info
        return ".".join(str(v) for v in bl_info.get("version", ()))
    except Exception:
        return "unknown"


def _instance_info() -> dict:
    info = {
        "ok": True,
        "app": "blender",
        "pid": os.getpid(),
        "port": _state["port"],
        "addon_version": _addon_version(),
    }
    if bpy is not None:
        info["blender_version"] = ".".join(str(v) for v in bpy.app.version)
        # During addon registration bpy.data is a _RestrictData without
        # .filepath. The ping recomputes this later with full access.
        try:
            info["blend_file"] = bpy.data.filepath or ""
        except AttributeError:
            info["blend_file"] = ""
    return info


class _Job:
    def __init__(self, payload):
        self.payload = payload
        self.done = threading.Event()
        self.result = None


_PING_PATH = "/cadlink/ping"
_IMPORT_PATH = "/cadlink/import"


class _Handler(BaseHTTPRequestHandler):
    # Default handler logs every request to stderr. One line per poll would
    # drown the console.
    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json. charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass

    def _authorized(self) -> bool:
        sent = self.headers.get("X-CADLink-Token", "")
        return sent == _state["token"]

    def do_GET(self):
        if self.path != _PING_PATH:
            self._reply(404, {"ok": False, "error": "unknown endpoint"})
            return
        if not self._authorized():
            self._reply(403, {"ok": False, "error": "bad token"})
            return
        self._reply(200, _instance_info())

    def do_POST(self):
        if self.path != _IMPORT_PATH:
            self._reply(404, {"ok": False, "error": "unknown endpoint"})
            return
        if not self._authorized():
            self._reply(403, {"ok": False, "error": "bad token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._reply(400, {"ok": False, "error": "bad payload: %s" % exc})
            return
        job = _Job(payload)
        _state["queue"].put(job)
        if not _wait_with_watchdog(job):
            self._reply(504, {"ok": False,
                              "error": "job timed out after %ds. Blender may "
                                       "still be working" % _JOB_TIMEOUT_S})
            return
        self._reply(200 if job.result.get("ok") else 500, job.result)


_STALL_S = 60
_HEARTBEAT_S = 1.0
_stall_log = None
_last_beat = 0.0


def stall_log_path() -> str:
    return os.path.join(registry_dir(), "stall-%d.txt" % os.getpid())


def _open_stall_log():
    global _stall_log
    if _stall_log is None:
        os.makedirs(registry_dir(), exist_ok=True)
        # Kept open for the session: faulthandler writes through the file
        # descriptor, from outside the interpreter.
        _stall_log = open(stall_log_path(), "w", encoding="utf-8")
        _stall_log.write(
            "Stacks below are written when Blender's main thread stayed away "
            "from the bridge for %ds: a freeze, or one long operation.\n"
            % _STALL_S)
        # The GPU backend matters for a freeze that leaves no Python
        # stack: a Vulkan render-graph hang looks exactly like one.
        try:
            _stall_log.write("blender %s, gpu backend %s\n" % (
                bpy.app.version_string,
                bpy.context.preferences.system.gpu_backend))
        except Exception:
            pass
        _stall_log.flush()
    return _stall_log


def _heartbeat():
    """Re-arms the stall dump from the pump, which the main thread runs
    every _PUMP_INTERVAL_S while the bridge is up. If the main thread stops
    running the pump for _STALL_S seconds, for any reason (a bpy call that
    never returns, a redraw that loops, a modal dialog nobody sees), the C
    timer fires and every thread's Python stack goes to the stall file.
    Re-armed once a second, not every pump: each arm restarts
    faulthandler's own thread."""
    global _last_beat
    now = time.monotonic()
    if now - _last_beat < _HEARTBEAT_S:
        return
    _last_beat = now
    try:
        import faulthandler
        faulthandler.dump_traceback_later(_STALL_S, repeat=False,
                                          file=_open_stall_log())
    except Exception:
        pass


def _arm_stall_dump(payload: dict):
    """Starts faulthandler's C-level timer for the job about to run. It
    writes every thread's Python stack to the stall file after _STALL_S
    seconds, and again every _STALL_S seconds until the job disarms it.

    Why a C timer and not a Python thread: a Blender that "froze on send"
    is a main thread stuck INSIDE a bpy call, which holds the interpreter
    lock, so no Python thread can run to take the dump. The first watchdog
    (2026-09-14) was such a thread and never fired for the live freeze it
    was written for. faulthandler's timer needs no lock: it names the bpy
    call that never returned."""
    try:
        import faulthandler
        log = _open_stall_log()
        log.write("job started: manifest=%s step=%s mesh=%s\n" % (
            payload.get("manifest"), payload.get("step"), payload.get("mesh")))
        log.flush()
        faulthandler.dump_traceback_later(_STALL_S, repeat=True, file=log)
    except Exception as exc:                # never let the watchdog fail the job
        print("[CADLink bridge] stall watchdog not armed:", exc)


def _disarm_stall_dump(result: dict):
    """Ends the repeating job timer and leaves a one-shot armed, so a
    freeze that starts in the redraw right after the job (live 2026-09-15:
    the job returned ok, then Blender's Vulkan submission thread spun
    forever) is still dumped, not silently lost between the job and the
    next heartbeat."""
    try:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        if _stall_log is not None:
            _stall_log.write("job finished: %s\n" % (
                "ok" if result.get("ok") else result.get("error", "failed")))
            _stall_log.flush()
            faulthandler.dump_traceback_later(_STALL_S, repeat=False,
                                              file=_stall_log)
    except Exception:
        pass


def _wait_with_watchdog(job) -> bool:
    """Waits for the main thread to finish the job. A job still running
    after _STALL_S seconds is announced on the console with the path of
    the stall file the C timer writes to (see _arm_stall_dump). Returns
    False on the full timeout."""
    waited = 0
    while waited < _JOB_TIMEOUT_S:
        slice_s = min(_STALL_S, _JOB_TIMEOUT_S - waited)
        if job.done.wait(slice_s):
            return True
        waited += slice_s
        print("[CADLink bridge] job still running after %ds; if Blender is "
              "frozen, the stack is in %s" % (waited, stall_log_path()))
    return False


# ── Main-thread job execution ───────────────────────────────────────────

def _ops_context():
    """bpy.ops inside a timer callback sees a window-less context. Borrow
    the first real window so operator poll()/report() behave."""
    wm = bpy.context.window_manager
    win = wm.windows[0] if wm.windows else None
    if win is None:
        return bpy.context.temp_override()
    return bpy.context.temp_override(window=win, scene=win.scene)


def _remove_previous_import(step_path: str, stages: dict, by_stem: bool = False):
    """Re-sending the same assembly must REPLACE the last send, not stack a
    copy next to it: leftover objects carry stale RIG_* tags that hijack
    matching and the frame vote (found live 2026-08-23: six re-sends of one
    hinge left the rig built against a previous send's rotated leaf).
    Removes every object imported from this STEP file, the import
    collections it left behind, and the importer's cache entry for it.

    by_stem: match on the file name without its extension, for the direct
    link, whose .swmesh replaces a STEP import of the same assembly. Both
    standing at once left every part in the scene twice, both copies
    parented to one rig (live 2026-09-14)."""
    want_full = os.path.normcase(os.path.abspath(step_path))
    want_base = os.path.basename(step_path).casefold()
    want_stem = os.path.splitext(want_base)[0]

    def is_same_file(value):
        if not value:
            return False
        s = str(value)
        base = os.path.basename(s).casefold()
        return (os.path.normcase(os.path.abspath(s)) == want_full
                or base == want_base
                or (by_stem and os.path.splitext(base)[0] == want_stem))

    removed = 0
    for obj in list(bpy.data.objects):
        try:
            if is_same_file(obj.get("STEP_file")):
                bpy.data.objects.remove(obj)
                removed += 1
        except ReferenceError:
            continue
    # Import collections ("<name>.flat/.hierarchy/.components" and their
    # per-part children) die once emptied: bottom-up, and never a
    # collection that still holds anything. Only the ones the importer
    # made for this file, which it tags: a match on the name also took the
    # user's own empty "gearbox renders".
    def prune_collection(coll):
        for child in list(coll.children):
            if is_same_file(child.get("STEP_file")):
                prune_collection(child)
        if not coll.objects and not coll.children:
            bpy.data.collections.remove(coll)

    for coll in list(bpy.data.collections):
        try:
            if is_same_file(coll.get("STEP_file")):
                prune_collection(coll)
        except ReferenceError:
            continue
    # The importer cache: freshness-checked since 2026-08-23, purged here as
    # well so a bridge import always re-reads the file it was just sent.
    try:
        from . import main as main_mod
        for key in list(main_mod.global_file_cache.keys()):
            if is_same_file(key):
                del main_mod.global_file_cache[key]
                main_mod.global_file_cache_meta.pop(key, None)
    except Exception as exc:
        print("[CADLink bridge] cache purge failed:", exc)
    if removed:
        print("[CADLink bridge] replaced previous import: removed %d object(s)"
              % removed)
    stages["replace"] = {"removed_objects": removed}


def _cleanup_leftover_empties(stages: dict, step_path: str = ""):
    """After relink the matched parts hang from the rig. The import's
    occurrence empties are dead weight. Childless STEP empties are removed
    repeatedly until stable (parents become childless as leaves go).

    Only the empties of this job's import, the STEP file `step_path`: the
    empties of a different import in the scene are not this job's to
    remove."""
    removed = 0
    want = os.path.normcase(os.path.abspath(step_path)) if step_path else None

    def of_this_import(obj):
        value = obj.get("STEP_file")
        return (want is not None and bool(value)
                and os.path.normcase(os.path.abspath(str(value))) == want)

    while True:
        doomed = []
        for obj in list(bpy.data.objects):
            try:
                if obj.type != "EMPTY" or obj.instance_type == "COLLECTION":
                    continue
                if obj.get("STEP_uuid") is None and obj.get("STEP_name") is None:
                    continue
                if not of_this_import(obj):
                    continue
                if obj.get("RIG_group_empty") or obj.get("RIG_helper"):
                    continue
                if obj.children:
                    continue
                doomed.append(obj)
            except ReferenceError:
                continue
        if not doomed:
            break
        for obj in doomed:
            bpy.data.objects.remove(obj)
            removed += 1
    stages["cleanup"] = {"removed_empties": removed}


def _leave_object_modes():
    """Every object out of pose/edit sculpt/etc. before the job touches the
    scene. context.mode reports only the ACTIVE object's mode, and object
    mode is per-object since 2.8. Live hang (2026-08-23): a send while the
    previous rig's armature sat in Pose mode. The import re-pointed the
    active object so every context.mode guard read OBJECT, and the rig
    rebuild then deleted the armature that still owned the session's pose
    state, freezing Blender. So the sweep reads each object's own mode.
    Returns the names it had to fix (logged into the job)."""
    view_layer = bpy.context.view_layer
    in_layer = set(view_layer.objects)
    prev_active = view_layer.objects.active
    left = []
    for obj in list(bpy.data.objects):
        try:
            if obj.mode == "OBJECT" or obj not in in_layer:
                continue
            view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="OBJECT")
            left.append(obj.name)
        except (RuntimeError, ReferenceError):
            continue
    try:
        if prev_active is not None:
            view_layer.objects.active = prev_active
    except (RuntimeError, ReferenceError):
        pass
    return left


def _ask_again_without_small_features(asked, opts, separate_solids):
    """Asks the CAD application for the marked parts again, AFTER the send
    this is answering has finished.

    A send arrives from the CAD application, which is sitting on the HTTP
    call and waiting for the answer. Calling back into it from here would
    ask it to run a COM operation on a thread it has blocked, and both
    sides would wait for each other until one timed out. So this goes on a
    one-shot timer: the send is answered first, the CAD application is free
    again, and the marked parts come back a moment later.

    The quality is the one the send itself used, which the import options
    carry, so the parts that come back match the ones beside them.
    """
    def run():
        from . import quality as quality_mod
        from .rig import cad_link, native_import
        try:
            reply = cad_link.retessellate(
                [row["component"] for row in asked],
                quality_mod.cad_request(quality_mod.spec_of(opts)),
                separate_solids=separate_solids, defeature=asked)
            changed = native_import.refine(bpy.context, reply["mesh"])
            print("[CADLink defeature] %d part(s) came back without their "
                  "small features" % len(changed))
        except Exception as exc:                       # noqa: BLE001
            print("[CADLink defeature] the marked parts could not be asked "
                  "for again: %s" % exc)
        return None

    bpy.app.timers.register(run, first_interval=0.1)


def _run_job(payload: dict) -> dict:
    from .rig import progress as rig_progress

    stages = {}
    log = []
    manifest_path = payload.get("manifest") or ""
    step_path = payload.get("step") or ""
    mesh_path = payload.get("mesh") or ""
    steps = payload.get("steps") or {}

    def want(name, default=True):
        return bool(steps.get(name, default))

    have_manifest = bool(manifest_path)
    if have_manifest and not os.path.isfile(manifest_path):
        return {"ok": False, "error": "manifest not found: %s" % manifest_path}

    # What the job is doing, in the status bar. A send of a large
    # assembly is minutes of work inside one call, and Blender draws
    # nothing meanwhile. The stage names match the ones the add-in shows
    # in SolidWorks.
    said = rig_progress.JobProgress(bpy.context)
    rig_hold = {}
    try:
        return _run_stages(payload, stages, log, manifest_path, step_path,
                           mesh_path, want, have_manifest, said, rig_hold)
    finally:
        # An update takes the parts and the animation off the rig while it
        # binds the parts again. Whatever ended the job, the rig gets both
        # back: a part nothing bound again goes back on its bone.
        released = rig_hold.get("release")
        if released is not None:
            if not rig_hold.get("relinked"):
                try:
                    released.rebind(bpy.context)
                except Exception as exc:        # noqa: BLE001
                    print("[CADLink] could not put the parts back on the "
                          "rig:", exc)
            released.finish()
        said.close()


def _remember_document(payload: dict):
    """Writes on the scene which CAD document the payload came from. A
    request back to the CAD application names it (rig/cad_link.py), so the
    CAD application answers for this document and not for whichever one
    the user has made active there since. An older add-in names none, and
    the scene keeps what it has."""
    document = payload.get("source_document")
    if not document:
        return
    from .rig import cad_link
    try:
        bpy.context.scene[cad_link.DOCUMENT_TAG] = str(document)
    except (AttributeError, TypeError, RuntimeError) as exc:
        print("[CADLink bridge] could not record the source document:", exc)


def _apply_poses_from(poses: dict) -> int:
    """Moves the scene onto the poses the CAD application pushed. The same
    path an Rebuild from CAD takes, so one rule decides how a pose reaches
    the objects."""
    from .rig import ui as rig_ui

    return rig_ui._apply_poses(bpy.context, poses)


def _rig_of(stem):
    """The rig that holds the parts of the import `stem`, or None."""
    counts = {}
    for obj in bpy.data.objects:
        arm = obj.parent
        if (arm is not None and arm.type == "ARMATURE" and arm.get("RIG_rig")
                and obj.get("SWMESH_file") == stem):
            counts[arm] = counts.get(arm, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _same_file(a, b) -> bool:
    try:
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))
    except (TypeError, ValueError):
        return False


def _rig_for(manifest, stem=None):
    """The rig of the assembly this job brings in, or None.

    First the rig that holds the parts of the import `stem`, then a rig
    built from this manifest (the build writes the manifest path on it).
    Never merely the first rig in the scene: each assembly keeps a rig of
    its own beside the others, and an update that took the other rig
    released its parts, cleared its bones and built this assembly's bones
    inside it."""
    if stem:
        rig = _rig_of(stem)
        if rig is not None:
            return rig
    source = getattr(manifest, "source_path", None)
    if not source:
        return None
    from .rig import parenting
    for obj in bpy.context.scene.objects:
        if (obj.type == "ARMATURE" and obj.get("RIG_rig")
                and any(_same_file(s, source)
                        for s in parenting.rig_sources(obj))):
            return obj
    return None


def _update_rig(mode, log, rig=None):
    """The rig half of an update: KEEP, APPEND or REGENERATE. `rig` is the
    rig of the assembly being updated (see _rig_for), or None when it has
    none yet. Then KEEP and APPEND build a new one, as REGENERATE does."""
    from .rig import rig_update, ui as rig_ui

    manifest = rig_ui._STATE.get("manifest")
    if manifest is None:
        return {"error": "no manifest to build the rig from"}
    report = rig_ui._STATE.get("match_report")
    frame_rows = (report.frame_rows
                  if report is not None and report.frame_agree > 0 else None)
    arm = rig
    # Where the user placed the rig. The parts were put on their poses in
    # its frame, so a new rig takes the same place and the machine stays
    # where it was put.
    placed = arm.matrix_world.copy() if arm is not None else None
    try:
        result, rig_report = rig_update.apply(
            bpy.context, mode, manifest, arm, bpy.data.objects,
            frame_rows=frame_rows,
            before=rig_ui._STATE.get("rig_snapshot") or {})
    except Exception as exc:
        return {"error": "the rig could not be brought up to date: %s" % exc}
    if result is not None:
        rig_ui._STATE["build"] = result
        new_arm = result.armature_object
        if placed is not None and new_arm is not None and new_arm is not arm:
            new_arm.matrix_world = placed
            bpy.context.view_layer.update()
    log.append("rig (%s): %s" % (mode.lower(), rig_report.describe()))
    return {
        "mode": rig_report.mode,
        "bones": rig_report.bones_after,
        "kept": len(rig_report.kept),
        "added": rig_report.added,
        "removed": rig_report.removed,
        "warnings": rig_report.warnings,
    }


def _run_stages(payload, stages, log, manifest_path, step_path, mesh_path,
                want, have_manifest, said, rig_hold=None):
    """The stages themselves. Split out so the reporter closes whatever
    ends the job."""
    from .rig import matching, ui as rig_ui

    if rig_hold is None:
        rig_hold = {}

    # A pose push. The CAD application moved parts and says where they
    # are now: no geometry, no manifest file, no rig rebuild beyond what
    # moving the bones needs. This is the fast half of a send, for
    # iterating on a mechanism's position.
    poses = payload.get("poses")
    if poses and not mesh_path and not step_path and not manifest_path:
        with _ops_context():
            if rig_ui._STATE.get("manifest") is None:
                return {"ok": False, "stages": stages,
                        "error": "Send the assembly to Blender first: this "
                                 "scene has no manifest to update."}
            _remember_document(payload)
            said.stage("moving the parts to where the CAD has them", 0, 100)
            said_count = len(poses.get("components") or [])
            moved = _apply_poses_from(poses)
            stages["poses"] = {"moved": moved, "components": said_count}
            bpy.context.view_layer.update()
        return {"ok": True, "stages": stages, "log": log}

    # An UPDATE brings the scene up to date part by part. A send replaces
    # it. The difference reaches several stages, so it is read once.
    updating = want("update", False)

    with _ops_context():
        scene = bpy.context.scene

        said.stage("preparing the scene", 0, 5)
        left = _leave_object_modes()
        if left:
            log.append("left non-object mode on: %s" % ", ".join(left))

        if have_manifest:
            said.stage("reading the manifest", 5, 10)
            scene.cad_link.manifest_path = manifest_path
            if "FINISHED" not in bpy.ops.cadlink.load_manifest():
                return {"ok": False,
                        "error": rig_ui._STATE["error"] or "manifest load failed",
                        "stages": stages}
            m = rig_ui._STATE["manifest"]
            stages["manifest"] = {
                "joints": len(m.joints),
                "groups": len(m.rigid_groups),
                "loops": len(m.loops),
                "warnings": [{"code": w.code, "message": w.message}
                             for w in m.warnings],
            }
            if not step_path and m.step_file:
                step_path = os.path.join(os.path.dirname(manifest_path),
                                         m.step_file)
        elif mesh_path or step_path:
            # A send without a manifest means "no rig": a part document, or
            # an assembly the user sent without its rig. The manifest of the
            # last send is for a different export, and its component ids
            # (c001, c002) are positions, not parts. Left loaded, it gave
            # these parts the group ids and persistent ids of other parts,
            # and the next refresh paired the parts by those wrong ids.
            rig_ui._reset_state()
        manifest = rig_ui._STATE.get("manifest") if have_manifest else None
        # The direct link's import, by the name its parts carry.
        stem = (os.path.splitext(os.path.basename(mesh_path))[0]
                if mesh_path else None)

        # The DIRECT link. Geometry the add-in tessellated itself, already
        # tagged with the component ids the manifest uses, so it replaces
        # both the STEP import and the matching pass that follows it, and
        # the report it hands back is exact rather than inferred.
        if mesh_path:
            if not os.path.isfile(mesh_path):
                return {"ok": False, "error": "mesh not found: %s" % mesh_path,
                        "stages": stages}
            from .rig import native_import, rig_update
            opts = payload.get("import_options") or {}
            rig_ui._STATE["import_options"] = dict(opts)
            # The CAD application asks for quads or does not, and the scene
            # then holds that answer: a later rebuild or regenerate here
            # gives the same mesh as the send did.
            prg = getattr(bpy.context.scene, "stepper", None)
            for key in ("tris_to_quads", "uv_unwrap_compound"):
                if key in opts and prg is not None:
                    setattr(prg, key, bool(opts[key]))
            # An UPDATE keeps the scene and changes what changed. A send
            # replaces it. The rig snapshot has to be taken before either
            # touches the parts: it reads the group ids of the export the
            # rig was built from, which the update overwrites.
            rig_ui._STATE["rig_snapshot"] = {}

            def before_changes(stem):
                # Runs once the update has found this assembly in the
                # scene, and before a part moves. The rig is the one that
                # holds this assembly's parts, not merely the first one.
                rig = _rig_of(stem)
                rig_hold["rig"] = rig or _rig_for(manifest)
                if rig_hold["rig"] is None:
                    return
                rig_ui._STATE["rig_snapshot"] = rig_update.snapshot(
                    rig_hold["rig"], bpy.data.objects)
                # The rig goes to its rest pose and lets go of its parts,
                # because a part moved while on a posed bone is bound again
                # with the pose in it. Only a rig this assembly's parts
                # hang on, only when relink will bind them again, and only
                # with the manifest of THIS send loaded: the parts are put
                # on its poses below.
                if rig is not None and have_manifest and want("relink"):
                    released = rig_update.release(bpy.context, rig,
                                                  hold=rig_hold)
                    if released.posed:
                        log.append("the rig was put back to its rest pose "
                                   "before the update")
            # Which parts the scene holds without their small features is
            # this scene's own decision, and a send replaces every object
            # and collection that carries it. So it is written down here
            # and put back on what arrives.
            from .rig import defeature as rig_defeature
            held = None if updating else rig_defeature.snapshot()
            if want("replace") and not updating:
                # A STEP import of the same assembly goes too, or the scene
                # holds every part twice.
                said.stage("replacing the last import", 10, 20)
                _remove_previous_import(mesh_path, stages, by_stem=True)
            try:
                if updating:
                    # Without a manifest the update knows no groups, and it
                    # would take the group off every part it touches. A
                    # part that stays keeps the group it has: the rig it
                    # hangs on still knows it by that group.
                    groups = {} if manifest is not None else {
                        o: o["RIG_group"] for o in bpy.data.objects
                        if o.get("SWMESH_file") is not None
                        and o.get("RIG_group") is not None}
                    objects, report, changed = native_import.update(
                        bpy.context, mesh_path,
                        manifest=manifest,
                        up_as=opts.get("up_as") or "ZPOS",
                        hierarchy=opts.get("hierarchy_types") or "FLAT",
                        report_to=said, before_changes=before_changes)
                    for obj, gid in groups.items():
                        try:
                            if obj.get("RIG_group") is None:
                                obj["RIG_group"] = gid
                        except ReferenceError:
                            continue
                    stages["update"] = {
                        "added": changed.added,
                        "removed": changed.removed,
                        "moved": changed.moved,
                        "reshaped": changed.reshaped,
                        "kept": changed.kept,
                        "structural": changed.structural,
                    }
                    log.append("update: " + changed.describe())
                else:
                    objects, report = native_import.build(
                        bpy.context, mesh_path,
                        manifest=manifest,
                        up_as=opts.get("up_as") or "ZPOS",
                        hierarchy=opts.get("hierarchy_types") or "FLAT",
                        report_to=said)
            except Exception as exc:
                return {"ok": False, "error": "native import failed: %s" % exc,
                        "stages": stages}
            rig_ui._STATE["match_report"] = report
            released = rig_hold.get("release")
            if released is not None and released.parts:
                # The parts that were on the rig go back to where the CAD
                # has them, all of them and not only the ones the CAD
                # moved: the rig binds them where they stand, and a part
                # bound off its pose by an earlier update is put right
                # here. A part that was never on the rig is left alone,
                # because a free part may have been placed by hand.
                # In the rig's own frame: bones stand at the armature's
                # transform times the CAD frame, so a rig the user moved
                # keeps its parts with it.
                import copy
                from mathutils import Matrix
                from .rig import pose_sync
                in_rig = copy.copy(report)
                in_rig.frame_rows = [list(r) for r in (
                    released.arm_obj.matrix_world
                    @ Matrix([tuple(r) for r in report.frame_rows]))]
                back = pose_sync.sync(rig_ui._STATE["manifest"], in_rig,
                                      objects=rig_update.alive(released.parts))
                stages["poses"] = {
                    "moved": [{"object": n, "distance_m": d}
                              for n, d in back.moved],
                    "already_ok": back.already_ok,
                    "skipped": [{"object": n, "reason": r}
                                for n, r in back.skipped],
                }
                if back.moved:
                    log.append("%d part(s) of the rig put back on their CAD "
                               "pose" % len(back.moved))
            made = native_import.quads(objects)
            if made:
                log.append("tris to quads: %d mesh(es)" % made)
            # A compound surface has no chart one scale can hold, and the
            # CAD application has none to send. The unwrap is Blender's, so
            # it runs here, on what just arrived.
            if prg is None or prg.uv_unwrap_compound:
                from . import main as main_mod
                faces, islands = main_mod._unwrap_compound_objects(objects)
                if faces:
                    log.append("unwrapped %d compound face(s) into %d "
                               "island(s)" % (faces, islands))
            stages["mesh"] = {
                "file": os.path.basename(mesh_path),
                "objects": len(objects),
                "meshes": len({o.data.name for o in objects if o.data}),
                "unmatched": list(report.unmatched),
            }
            bpy.context.view_layer.update()
            if held is not None:
                parts, groups = rig_defeature.restore(held)
                if parts or groups:
                    log.append(
                        "defeature: %d part(s) and %d collection(s) kept their "
                        "settings" % (parts, groups))
            # The CAD application holds no such setting, so a send always
            # brings the small features back. The marked parts are asked for
            # again, which is what keeps the scene saying one thing.
            asked = rig_defeature.orders(objects)
            if asked:
                stages["mesh"]["defeature"] = len(asked)
                log.append("defeature: asking again for %d part(s) without "
                           "their small features" % len(asked))
                _ask_again_without_small_features(
                    asked, opts,
                    opts.get("separate_solids"))

        if not mesh_path and want("import", bool(step_path)):
            if not step_path or not os.path.isfile(step_path):
                return {"ok": False, "error": "STEP file not found: %s" % step_path,
                        "stages": stages}
            if want("replace"):
                said.stage("replacing the last import", 10, 20)
                _remove_previous_import(step_path, stages)
            said.stage("importing the STEP file", 20, 80)
            opts = payload.get("import_options") or {}
            kwargs = {k: v for k, v in opts.items() if k in _IMPORT_OPTION_KEYS}
            ignored = sorted(set(opts) - set(kwargs))
            if ignored:
                log.append("ignored import options: %s" % ", ".join(ignored))
            kwargs.setdefault("up_as", "ZPOS")
            kwargs.setdefault("fw_as", "YPOS")
            # override_file forces the synchronous path: the background
            # worker returns FINISHED before geometry exists, and every
            # stage after this one would run against an empty scene.
            result = bpy.ops.import_scene.occ_import_step(
                filepath=step_path,
                override_file=os.path.basename(step_path),
                **kwargs)
            if "FINISHED" not in result:
                return {"ok": False, "error": "STEP import failed",
                        "stages": stages}
            stages["import"] = {"file": os.path.basename(step_path)}
            bpy.context.view_layer.update()

        # What the send brings has arrived. Later stages can still fail,
        # but the scene now holds this document.
        _remember_document(payload)

        if have_manifest and not mesh_path and want("match"):
            said.stage("matching the parts to the manifest", 80, 85)
            if "FINISHED" not in bpy.ops.cadlink.match_geometry():
                return {"ok": False, "error": "matching failed", "stages": stages}
            rep = rig_ui._STATE["match_report"]
            stages["match"] = {
                "matched": len(rep.matched),
                "ambiguous": [cid for cid, _ in rep.ambiguous],
                "unmatched": list(rep.unmatched),
                "frame": matching.describe_frame(rep.frame_rows),
            }

        # A skipped earlier stage can fail a later operator's poll(), and a
        # failed poll RAISES instead of returning CANCELLED: every optional
        # stage below runs only when its precondition actually holds.
        # An update has already put every part where the CAD says it is:
        # it moves the parts the CAD moved, and the parts that were on the
        # rig were put on their CAD pose above. A free part it did not move
        # stays where the user put it.
        if have_manifest and want("sync_poses") and not updating \
                and rig_ui._STATE.get("match_report") is not None:
            said.stage("syncing the poses", 85, 88)
            if "FINISHED" in bpy.ops.cadlink.sync_poses():
                rep = rig_ui._STATE["pose_report"]
                if rep is not None:
                    stages["poses"] = {
                        "moved": [{"object": n, "distance_m": d}
                                  for n, d in rep.moved],
                        "already_ok": rep.already_ok,
                        "skipped": [{"object": n, "reason": r}
                                    for n, r in rep.skipped],
                    }

        # The rig of THIS assembly, from here to relink. The first rig in
        # the scene can belong to a different assembly, whose parts and
        # bones this job must not touch.
        own = (rig_hold.get("rig") or _rig_for(manifest, stem)) \
            if have_manifest else None
        built = None
        if have_manifest and want("build_rig"):
            # A rig the user locked is kept, and the send says so rather
            # than failing: the geometry still arrives and still attaches
            # to it, which is the whole point of locking one.
            from .rig import rig_build, rig_update
            mode = (payload.get("rig_mode") or "").upper()
            # rig_build builds no rig at all while any rig in the scene is
            # locked. KEEP on a rig that stands builds nothing, so only
            # that goes on.
            blocking = rig_build.locked_rig(bpy.context)
            if rig_build.is_locked(own):
                said.stage("keeping the locked rig", 88, 96)
                stages["rig"] = {"locked": own.name}
                log.append("the rig %s is locked: it was kept as it is, and "
                           "the parts were attached to it" % own.name)
            elif blocking is not None and not (
                    mode == rig_update.KEEP and own is not None):
                said.stage("keeping the locked rig", 88, 96)
                stages["rig"] = {"locked": blocking.name}
                log.append("the rig %s of a different assembly is locked, so "
                           "this send did not build a rig. Unlock %s and "
                           "send again to build it"
                           % (blocking.name, blocking.name))
            elif mode in rig_update.MODES:
                # An update says what to do with the rig: keep it, rebuild
                # it inside the armature that is there (which keeps the
                # animation), or build a new one.
                said.stage("bringing the rig up to date", 88, 96)
                stages["rig"] = _update_rig(mode, log, own)
                if stages["rig"].get("error"):
                    return {"ok": False, "error": stages["rig"]["error"],
                            "stages": stages}
                build = rig_ui._STATE.get("build")
                if build is not None:
                    built = build.armature_object
            else:
                said.stage("building the rig", 88, 96)
                if "FINISHED" not in bpy.ops.cadlink.build_rig():
                    return {"ok": False,
                            "error": rig_ui._STATE["error"] or "rig build failed",
                            "stages": stages}
                build = rig_ui._STATE["build"]
                built = build.armature_object
                stages["rig"] = {
                    "bones": len(build.bone_names),
                    "helpers": len(build.helper_names),
                    "warnings": list(build.warnings),
                }

        # The parts go on the rig this job built, or else on the rig of
        # their own assembly. Not on the first rig in the scene: parts of
        # a direct send carry no manifest path, so relink would bind them
        # to a different assembly's bones by the group id alone.
        said.stage("attaching the parts to the rig", 96, 100)
        target = built if built is not None else own
        try:
            if target is not None:
                target.name
        except ReferenceError:
            target = None
        if have_manifest and want("relink") and target is not None:
            from .rig import parenting
            rep = parenting.relink(bpy.context, target)
            rig_ui._STATE["parent_report"] = rep
            rig_hold["relinked"] = True
            stages["relink"] = {
                "parented": rep.bone_parented,
                "grounded": len(rep.grounded),
                "drift_violations": len(rep.violations),
                "posed_bones": [name for name, _ in rep.posed_bones],
            }
        # The parts are bound at rest, so the animation can have the rig
        # back.
        released = rig_hold.get("release")
        if released is not None:
            released.finish()

        if want("cleanup", False):
            _cleanup_leftover_empties(stages, step_path)

        # Where the CAD application is looking from, when the user asked
        # for it. It comes last, so the view frames what the whole job
        # left in the scene.
        if payload.get("view"):
            from .rig import native_import as native_mod, viewport
            opts = payload.get("import_options") or {}
            moved = viewport.match(
                payload["view"],
                native_mod.up_frame(opts.get("up_as") or "ZPOS"))
            if moved:
                log.append("view: %d 3D view(s) turned to the CAD angle"
                           % moved)

        bpy.context.view_layer.update()

    ok = True
    match = stages.get("match")
    if match and (match["unmatched"] or match["ambiguous"]):
        log.append("some components did not match: see the SolidWorks "
                   "Bridge panel in Blender")
    return {"ok": ok, "stages": stages, "log": log}


def _pump():
    q = _state["queue"]
    if q is None:
        _state["timer_running"] = False
        return None
    _heartbeat()
    try:
        job = q.get_nowait()
    except queue.Empty:
        return _PUMP_INTERVAL_S
    # BaseException, and the hand-off in a finally: a single escaped
    # exception would unregister the timer while the HTTP server keeps
    # listening, leaving a bridge that looks alive but stalls every send for the
    # full 30-minute timeout, surviving until Blender restarts.
    _arm_stall_dump(job.payload)
    try:
        job.result = _run_job(job.payload)
    except BaseException:
        job.result = {"ok": False, "error": traceback.format_exc()}
    finally:
        if job.result is None:
            job.result = {"ok": False, "error": "job produced no result"}
        _disarm_stall_dump(job.result)
        _state["last_job"] = job.result
        job.done.set()
    print("[CADLink bridge] job finished: %s"
          % ("ok" if job.result.get("ok") else job.result.get("error", "failed")))
    return _PUMP_INTERVAL_S


# ── Lifecycle ───────────────────────────────────────────────────────────

def _write_registry():
    path = os.path.join(registry_dir(), "%d.json" % os.getpid())
    os.makedirs(registry_dir(), exist_ok=True)
    info = _instance_info()
    info["token"] = _state["token"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    _state["registry_path"] = path


def _remove_registry():
    path = _state.get("registry_path")
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
        _state["registry_path"] = None


def status():
    """What each CAD bridge is doing, as (text, icon) rows.

    One row today, because there is one bridge. A second CAD application
    would add a row of its own, so the panel that draws this does not have
    to change when one arrives.
    """
    if not is_running():
        return [("SolidWorks bridge is off", "UNLINKED")]
    if _state["last_job"] is not None:
        return [("SolidWorks bridge is active", "LINKED")]
    return [("SolidWorks bridge is waiting for a connection", "PLUGIN")]


def is_running() -> bool:
    return _state["server"] is not None


def port():
    return _state["port"]


def last_job():
    return _state["last_job"]


def start():
    if _state["server"] is not None or bpy is None:
        return
    if bpy.app.background:
        # A -b session has no timer-pumping event loop. The smoke test
        # pumps by hand instead of starting the timer.
        pass
    _state["token"] = secrets.token_hex(16)
    _state["queue"] = queue.Queue()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    _state["server"] = server
    _state["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              name="cadlink-bridge", daemon=True)
    thread.start()
    _state["thread"] = thread
    # The pump comes up before anything that can fail: a bridge that
    # listens but never executes its jobs is worse than no bridge.
    if not bpy.app.background and not _state["timer_running"]:
        bpy.app.timers.register(_pump, first_interval=_PUMP_INTERVAL_S,
                                persistent=True)
        _state["timer_running"] = True
    try:
        _write_registry()
    except OSError as exc:
        print("[CADLink bridge] registry write failed:", exc)
    atexit.register(_remove_registry)
    print("[CADLink bridge] listening on 127.0.0.1:%d (registry %s)"
          % (_state["port"], _state["registry_path"]))


def stop():
    server = _state.get("server")
    if server is not None:
        try:
            server.shutdown()
            server.server_close()
        except OSError:
            pass
    _state["server"] = None
    _state["thread"] = None
    _state["port"] = None
    _state["queue"] = None
    _remove_registry()
    global _stall_log
    if _stall_log is not None:
        try:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
            _stall_log.close()
        except Exception:
            pass
        _stall_log = None
    if bpy is not None and _state["timer_running"]:
        try:
            if bpy.app.timers.is_registered(_pump):
                bpy.app.timers.unregister(_pump)
        except (ValueError, AttributeError):
            pass
        _state["timer_running"] = False


def register():
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        enabled = getattr(prefs, "enable_bridge", False)
    except (AttributeError, KeyError):
        enabled = False
    if enabled:
        start()


def unregister():
    stop()
