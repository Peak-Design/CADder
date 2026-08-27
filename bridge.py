# SPDX-License-Identifier: GPL-3.0-or-later
"""SW ⇄ Blender bridge: a localhost HTTP endpoint the SolidWorks add-in
drives directly — export in SW, geometry + rig appear in Blender with no
file dialogs in between.

Threading contract: the HTTP server lives on a daemon thread and NEVER
touches bpy. It enqueues jobs; a bpy.app.timers pump executes them on the
main thread and signals the waiting handler, which then writes the HTTP
response. Everything bpy happens on the main thread, always.

Discovery: on start the server binds 127.0.0.1 on an ephemeral port and
writes %LOCALAPPDATA%/PeakDesign/SwToBlender/bridge/<pid>.json with the
port and a random token. The SolidWorks side lists that directory, pings
each entry, and prunes the corpses. Every request must carry the token in
X-SWTB-Token — the file is user-readable only, so possession proves the
caller is the same desktop user.

Endpoints:
  GET  /swtb/ping    -> instance info (fast, main thread not involved)
  POST /swtb/import  -> full pipeline job, synchronous (import STEP, load
                        manifest, match, snap poses, build rig, parent,
                        tidy leftovers — each stage optional)
"""

import atexit
import json
import os
import queue
import secrets
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import bpy
except ImportError:
    bpy = None

_JOB_TIMEOUT_S = 30 * 60
_PUMP_INTERVAL_S = 0.2

# Importer options a job may forward verbatim to occ_import_step. Anything
# else in the payload's import_options is reported back as ignored, not
# silently dropped.
_IMPORT_OPTION_KEYS = {
    "up_as", "fw_as", "hierarchy_types", "quality_preset", "detail_level",
    "custom_scale", "user_scale", "apply_scale", "uv_mode", "uv_normalize",
    "uv_split_closed", "box_uv_scale", "eng_materials", "import_curves",
    "skip_construction", "material_database", "tessellation_relative",
    "lin_deflection_rel", "group_in_collection", "separate_solids",
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
        # .filepath; the ping recomputes this later with full access.
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


class _Handler(BaseHTTPRequestHandler):
    # Default handler logs every request to stderr; one line per poll would
    # drown the console.
    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass

    def _authorized(self) -> bool:
        return self.headers.get("X-SWTB-Token", "") == _state["token"]

    def do_GET(self):
        if self.path != "/swtb/ping":
            self._reply(404, {"ok": False, "error": "unknown endpoint"})
            return
        if not self._authorized():
            self._reply(403, {"ok": False, "error": "bad token"})
            return
        self._reply(200, _instance_info())

    def do_POST(self):
        if self.path != "/swtb/import":
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
        if not job.done.wait(_JOB_TIMEOUT_S):
            self._reply(504, {"ok": False,
                              "error": "job timed out after %ds; Blender may "
                                       "still be working" % _JOB_TIMEOUT_S})
            return
        self._reply(200 if job.result.get("ok") else 500, job.result)


# ── Main-thread job execution ───────────────────────────────────────────

def _ops_context():
    """bpy.ops inside a timer callback sees a window-less context; borrow
    the first real window so operator poll()/report() behave."""
    wm = bpy.context.window_manager
    win = wm.windows[0] if wm.windows else None
    if win is None:
        return bpy.context.temp_override()
    return bpy.context.temp_override(window=win, scene=win.scene)


def _remove_previous_import(step_path: str, stages: dict):
    """Re-sending the same assembly must REPLACE the last send, not stack a
    copy next to it: leftover objects carry stale RIG_* tags that hijack
    matching and the frame vote (found live 2026-08-23 — six re-sends of one
    hinge left the rig built against a previous send's rotated leaf).
    Removes every object imported from this STEP file, the import
    collections it left behind, and the importer's cache entry for it."""
    want_full = os.path.normcase(os.path.abspath(step_path))
    want_base = os.path.basename(step_path).casefold()

    def is_same_file(value):
        if not value:
            return False
        s = str(value)
        return (os.path.normcase(os.path.abspath(s)) == want_full
                or os.path.basename(s).casefold() == want_base)

    removed = 0
    for obj in list(bpy.data.objects):
        try:
            if is_same_file(obj.get("STEP_file")):
                bpy.data.objects.remove(obj)
                removed += 1
        except ReferenceError:
            continue
    # Import collections ("<name>.flat/.hierarchy/.components" and their
    # per-part children) die once emptied — bottom-up, and never a
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
        print("[SWTB bridge] cache purge failed:", exc)
    if removed:
        print("[SWTB bridge] replaced previous import: removed %d object(s)"
              % removed)
    stages["replace"] = {"removed_objects": removed}


def _cleanup_leftover_empties(stages: dict):
    """After relink the matched parts hang from the rig; the import's
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
    mode is per-object since 2.8 — live hang (2026-08-23): a send while the
    previous rig's armature sat in Pose mode; the import re-pointed the
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
            scene.sw_to_blender.manifest_path = manifest_path
            if "FINISHED" not in bpy.ops.swtb.load_manifest():
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
        # tagged with the component ids the manifest uses — so it replaces
        # both the STEP import and the matching pass that follows it, and
        # the report it hands back is exact rather than inferred.
        if mesh_path:
            if not os.path.isfile(mesh_path):
                return {"ok": False, "error": "mesh not found: %s" % mesh_path,
                        "stages": stages}
            from .rig import native_import
            try:
                objects, report = native_import.build(
                    bpy.context, mesh_path,
                    manifest=rig_ui._STATE.get("manifest"))
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
            # override_file forces the synchronous path — the background
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
            if "FINISHED" not in bpy.ops.swtb.match_geometry():
                return {"ok": False, "error": "matching failed", "stages": stages}
            rep = rig_ui._STATE["match_report"]
            stages["match"] = {
                "matched": len(rep.matched),
                "ambiguous": [cid for cid, _ in rep.ambiguous],
                "unmatched": list(rep.unmatched),
                "frame": matching.describe_frame(rep.frame_rows),
            }

        # A skipped earlier stage can fail a later operator's poll(), and a
        # failed poll RAISES instead of returning CANCELLED — every optional
        # stage below runs only when its precondition actually holds.
        if have_manifest and want("sync_poses") \
                and rig_ui._STATE.get("match_report") is not None:
            if "FINISHED" in bpy.ops.swtb.sync_poses():
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
            if "FINISHED" not in bpy.ops.swtb.build_rig():
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
                and bpy.ops.swtb.relink_geometry.poll():
            if "FINISHED" in bpy.ops.swtb.relink_geometry():
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
        log.append("some components did not match — see the SW To Blender "
                   "panel in Blender")
    return {"ok": ok, "stages": stages, "log": log}


def _pump():
    q = _state["queue"]
    if q is None:
        _state["timer_running"] = False
        return None
    try:
        job = q.get_nowait()
    except queue.Empty:
        return _PUMP_INTERVAL_S
    # BaseException, and the hand-off in a finally: a single escaped
    # exception would unregister the timer while the HTTP server keeps
    # listening — a bridge that looks alive but stalls every send for the
    # full 30-minute timeout, surviving until Blender restarts.
    try:
        job.result = _run_job(job.payload)
    except BaseException:
        job.result = {"ok": False, "error": traceback.format_exc()}
    finally:
        if job.result is None:
            job.result = {"ok": False, "error": "job produced no result"}
        _state["last_job"] = job.result
        job.done.set()
    print("[SWTB bridge] job finished: %s"
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
        # A -b session has no timer-pumping event loop; the smoke test
        # pumps by hand instead of starting the timer.
        pass
    _state["token"] = secrets.token_hex(16)
    _state["queue"] = queue.Queue()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    _state["server"] = server
    _state["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              name="swtb-bridge", daemon=True)
    thread.start()
    _state["thread"] = thread
    # The pump comes up before anything that can fail — a bridge that
    # listens but never executes its jobs is worse than no bridge.
    if not bpy.app.background and not _state["timer_running"]:
        bpy.app.timers.register(_pump, first_interval=_PUMP_INTERVAL_S,
                                persistent=True)
        _state["timer_running"] = True
    try:
        _write_registry()
    except OSError as exc:
        print("[SWTB bridge] registry write failed:", exc)
    atexit.register(_remove_registry)
    print("[SWTB bridge] listening on 127.0.0.1:%d (registry %s)"
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
