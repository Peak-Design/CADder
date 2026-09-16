# SPDX-License-Identifier: GPL-3.0-or-later
"""CAD Link bridge: a localhost HTTP endpoint a CAD add-in (today: SW To
Blender for SolidWorks) drives directly: export in the CAD application,
geometry + rig appear in Blender with no file dialogs in between.

Threading contract: the HTTP server lives on a daemon thread and NEVER
touches bpy. It enqueues jobs. A bpy.app.timers pump executes them on the
main thread and signals the waiting handler, which then writes the HTTP
response. Everything bpy happens on the main thread, always.

Discovery: on start the server binds 127.0.0.1 on an ephemeral port and
writes %LOCALAPPDATA%/PeakDesign/SwToBlender/bridge/<pid>.json with the
port and a random token. The CAD side lists that directory, pings
each entry, and prunes the corpses. Every request must carry the token in
X-CADLink-Token: the file is user-readable only, so possession proves the
caller is the same desktop user. (The names from before the rename,
X-SWTB-Token and /swtb/..., are still accepted so an older add-in build
keeps working.)

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
    "up_as", "fw_as", "hierarchy_types", "quality_preset", "detail_level",
    "custom_scale", "user_scale", "apply_scale",
    "lin_deflection", "ang_deflection", "lin_deflection_len",
    "ang_deflection_rot", "tessellation_relative", "lin_deflection_rel",
    "uv_mode", "uv_normalize", "uv_closed_seams", "uv_smart_distortion",
    "uv_smart_sharp", "uv_smart_split", "uv_pack", "uv_pack_tiles",
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
    return os.path.join(base, "PeakDesign", "SwToBlender", "bridge")


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


_PING_PATHS = ("/cadlink/ping", "/swtb/ping")
_IMPORT_PATHS = ("/cadlink/import", "/swtb/import")


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
        sent = (self.headers.get("X-CADLink-Token")
                or self.headers.get("X-SWTB-Token", ""))
        return sent == _state["token"]

    def do_GET(self):
        if self.path not in _PING_PATHS:
            self._reply(404, {"ok": False, "error": "unknown endpoint"})
            return
        if not self._authorized():
            self._reply(403, {"ok": False, "error": "bad token"})
            return
        self._reply(200, _instance_info())

    def do_POST(self):
        if self.path not in _IMPORT_PATHS:
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
    # collection that still holds anything.
    stem = os.path.splitext(want_base)[0]

    def prune_collection(coll):
        for child in list(coll.children):
            prune_collection(child)
        if not coll.objects and not coll.children:
            bpy.data.collections.remove(coll)

    for coll in list(bpy.data.collections):
        try:
            if coll.name.casefold().startswith(stem):
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


def _cleanup_leftover_empties(stages: dict):
    """After relink the matched parts hang from the rig. The import's
    occurrence empties are dead weight. Childless STEP empties are removed
    repeatedly until stable (parents become childless as leaves go)."""
    removed = 0
    while True:
        doomed = []
        for obj in list(bpy.data.objects):
            try:
                if obj.type != "EMPTY" or obj.instance_type == "COLLECTION":
                    continue
                if obj.get("STEP_uuid") is None and obj.get("STEP_name") is None:
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


def _run_job(payload: dict) -> dict:
    from .rig import matching, ui as rig_ui

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

    with _ops_context():
        scene = bpy.context.scene

        left = _leave_object_modes()
        if left:
            log.append("left non-object mode on: %s" % ", ".join(left))

        if have_manifest:
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

        # The DIRECT link. Geometry the add-in tessellated itself, already
        # tagged with the component ids the manifest uses, so it replaces
        # both the STEP import and the matching pass that follows it, and
        # the report it hands back is exact rather than inferred.
        if mesh_path:
            if not os.path.isfile(mesh_path):
                return {"ok": False, "error": "mesh not found: %s" % mesh_path,
                        "stages": stages}
            from .rig import native_import
            if want("replace"):
                # A STEP import of the same assembly goes too, or the scene
                # holds every part twice.
                _remove_previous_import(mesh_path, stages, by_stem=True)
            try:
                opts = payload.get("import_options") or {}
                rig_ui._STATE["import_options"] = dict(opts)
                objects, report = native_import.build(
                    bpy.context, mesh_path,
                    manifest=rig_ui._STATE.get("manifest"),
                    up_as=opts.get("up_as") or "ZPOS",
                    hierarchy=opts.get("hierarchy_types") or "FLAT",
                    group_in_collection=bool(opts.get("group_in_collection")))
            except Exception as exc:
                return {"ok": False, "error": "native import failed: %s" % exc,
                        "stages": stages}
            rig_ui._STATE["match_report"] = report
            stages["mesh"] = {
                "file": os.path.basename(mesh_path),
                "objects": len(objects),
                "meshes": len({o.data.name for o in objects if o.data}),
                "unmatched": list(report.unmatched),
            }
            bpy.context.view_layer.update()

        if not mesh_path and want("import", bool(step_path)):
            if not step_path or not os.path.isfile(step_path):
                return {"ok": False, "error": "STEP file not found: %s" % step_path,
                        "stages": stages}
            if want("replace"):
                _remove_previous_import(step_path, stages)
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

        if have_manifest and not mesh_path and want("match"):
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
        if have_manifest and want("sync_poses") \
                and rig_ui._STATE.get("match_report") is not None:
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

        if have_manifest and want("build_rig"):
            if "FINISHED" not in bpy.ops.cadlink.build_rig():
                return {"ok": False,
                        "error": rig_ui._STATE["error"] or "rig build failed",
                        "stages": stages}
            build = rig_ui._STATE["build"]
            stages["rig"] = {
                "bones": len(build.bone_names),
                "helpers": len(build.helper_names),
                "warnings": list(build.warnings),
            }

        if have_manifest and want("relink") \
                and bpy.ops.cadlink.relink_geometry.poll():
            if "FINISHED" in bpy.ops.cadlink.relink_geometry():
                rep = rig_ui._STATE["parent_report"]
                if rep is not None:
                    stages["relink"] = {
                        "parented": rep.bone_parented,
                        "grounded": len(rep.grounded),
                        "drift_violations": len(rep.violations),
                        "posed_bones": [name for name, _ in rep.posed_bones],
                    }

        if want("cleanup", False):
            _cleanup_leftover_empties(stages)

        bpy.context.view_layer.update()

    ok = True
    match = stages.get("match")
    if match and (match["unmatched"] or match["ambiguous"]):
        log.append("some components did not match: see the CAD Link "
                   "panel in Blender")
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
