# Non-blocking background import for STEPper NEXT.
#
# OCP holds the GIL during STEP parsing (measured), so responsiveness
# requires a separate process. A headless Blender runs worker.py (the
# exact same import pipeline) and saves a .blend. The modal operator here
# keeps the UI alive, shows progress, supports ESC-cancel and appends the
# result when the worker finishes.

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import uuid

import bpy


# Addon preferences that change import results. Forwarded to the worker so
# the background path matches the synchronous path exactly.
FORWARDED_PREFS = (
    "build_materials",
    "hack_skip_zero_solids",
    "skip_empty_objects",
    "construction_filter_names",
    "simpler_parameters",
    "active_matdb",
)


def prefs_snapshot(prefs):
    snap = {}
    for name in FORWARDED_PREFS:
        try:
            snap[name] = getattr(prefs, name)
        except Exception:
            pass
    return snap


def _job_root():
    root = os.path.join(tempfile.gettempdir(), "STEPperNEXT", "jobs")
    os.makedirs(root, exist_ok=True)
    _sweep_stale_jobs(root)
    return root


def _sweep_stale_jobs(root, max_age_s=2 * 24 * 3600):
    """Remove leftover job dirs from crashed/killed sessions (best effort)."""
    import shutil
    import time
    now = time.time()
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for name in entries:
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > max_age_s:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _reader_thread(pipe, out_queue):
    try:
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith("@STEPPER "):
                try:
                    out_queue.put(json.loads(line[9:]))
                except json.JSONDecodeError:
                    pass
            elif "Phase 1:" in line or "Phase 2/3" in line or "Phase 3/3" in line:
                out_queue.put({"phase": "progress", "text": line.strip()})
    finally:
        pipe.close()


class STEPPER_OT_background_import(bpy.types.Operator):
    """Import STEP files in a background process. The interface stays responsive
    and Esc cancels the import."""
    bl_idname = "stepper.background_import"
    bl_label = "Background STEP import"

    # JSON: {"files": ["path", ...], "op_kwargs": {...}}
    job_json: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    _timer = None
    _proc = None
    _queue = None
    _thread = None
    _jobs = None
    _job_dir = None
    _status = ""
    _appended = 0
    _pct = 0.0        # progress within the current file (0-100)
    _total_files = 1
    _file_index = 0
    _running = False  # class-level: only one background import at a time

    def execute(self, context):
        if STEPPER_OT_background_import._running:
            self.report({"WARNING"},
                        "A background import is already running. "
                        "Wait for it to finish (or press Esc to cancel it)")
            return {"CANCELLED"}
        try:
            job = json.loads(self.job_json)
            self._jobs = list(job["files"])
            self._op_kwargs = job.get("op_kwargs", {})
            self._prefs_snapshot = job.get("prefs", {})
        except Exception as e:
            self.report({"ERROR"}, f"Bad background job: {e}")
            return {"CANCELLED"}
        if not self._jobs:
            return {"CANCELLED"}

        self._job_dir = os.path.join(_job_root(), uuid.uuid4().hex[:12])
        os.makedirs(self._job_dir, exist_ok=True)
        self._total_files = len(self._jobs)
        self._file_index = 0
        self._reap = []          # finished workers still shutting down
        self._exit_ticks = 0
        STEPPER_OT_background_import._running = True

        if not self._spawn_next(context):
            self._cleanup(context)
            return {"CANCELLED"}

        wm = context.window_manager
        # Cursor percentage indicator (the little counter at the mouse)
        wm.progress_begin(0, 100)
        self._timer = wm.event_timer_add(0.15, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _spawn_next(self, context):
        if not self._jobs:
            return False
        filepath = self._jobs.pop(0)
        self._current_file = filepath
        out_blend = os.path.join(
            self._job_dir, uuid.uuid4().hex[:8] + ".blend")
        request = {
            "addon_module": __package__,
            "filepath": filepath,
            "out_blend": out_blend,
            "op_kwargs": self._op_kwargs,
            "prefs": self._prefs_snapshot,
            "parent_pid": os.getpid(),
        }
        req_path = os.path.join(self._job_dir, "request.json")
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(request, f)

        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "worker.py")
        # --python-exit-code makes ANY worker script failure a nonzero exit,
        # so the modal loop cannot hang waiting for a "done" that never comes
        cmd = [bpy.app.binary_path, "-b", "--factory-startup",
               "--python-exit-code", "1",
               "--python", worker, "--", req_path]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=flags)
        except OSError as e:
            self.report({"ERROR"}, f"Could not start worker: {e}")
            return False
        self._out_blend = out_blend
        self._pct = 0.0
        self._exit_ticks = 0
        self._queue = queue.Queue()
        self._thread = threading.Thread(
            target=_reader_thread, args=(self._proc.stdout, self._queue),
            daemon=True)
        self._thread.start()
        self._status = f"Importing {os.path.basename(filepath)}… (Esc to cancel)"
        return True

    def modal(self, context, event):
        if event.type == "ESC":
            self._kill()
            self._cleanup(context)
            self.report({"WARNING"}, "Background import cancelled")
            return {"CANCELLED"}

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        # Reap workers from earlier files in the batch (non-blocking)
        self._reap = [p for p in self._reap if p.poll() is None]

        done_blend = None
        error = None
        got_msg = False
        while True:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            got_msg = True
            phase = msg.get("phase")
            if phase == "progress":
                text = msg.get("text", "")
                m = re.search(r"Phase 1: (\d+)%", text)
                if m:
                    self._pct = 10.0 + 0.6 * int(m.group(1))
                elif "Phase 2/3" in text:
                    self._pct = max(self._pct, 75.0)
                elif "Phase 3/3" in text:
                    self._pct = max(self._pct, 90.0)
                self._status = (f"{os.path.basename(self._current_file)}: "
                                f"{text} (Esc to cancel)")
            elif phase in ("startup", "importing", "saving"):
                self._pct = {"startup": 2.0, "importing": 8.0,
                             "saving": 96.0}[phase]
                self._status = (f"{os.path.basename(self._current_file)}: "
                                f"{phase}… (Esc to cancel)")
            elif phase == "done":
                self._pct = 100.0
                done_blend = msg.get("blend")
            elif phase == "error":
                error = msg.get("message", "unknown worker error")

        # The STEP parse emits no progress (OCP is opaque there), so creep
        # gently toward 60% between messages to show the import is alive.
        if not got_msg and 5.0 <= self._pct < 60.0:
            self._pct = min(self._pct + 0.4, 60.0)

        # Overall progress across the batch, shown at the mouse cursor
        overall = ((self._file_index + self._pct / 100.0)
                   / max(self._total_files, 1)) * 100.0
        context.window_manager.progress_update(int(overall))
        pct_txt = f"{int(overall)}%"
        self._status = re.sub(r"^\d+% \| ", "", self._status or "")
        self._status = f"{pct_txt} | {self._status}"
        context.workspace.status_text_set(self._status)

        rc = self._proc.poll()
        if rc is None and done_blend is None and error is None:
            return {"RUNNING_MODAL"}

        # Worker exited cleanly but no "done" arrived yet: give the reader
        # thread a few ticks to drain, then treat it as a failure instead of
        # spinning in the modal loop forever.
        if rc == 0 and done_blend is None and error is None:
            self._exit_ticks += 1
            if self._exit_ticks < 20:
                return {"RUNNING_MODAL"}
            error = "worker exited without a result"

        if error is not None or (rc is not None and rc != 0 and done_blend is None):
            self._cleanup(context)
            self.report({"ERROR"},
                        f"Background import failed: {error or f'worker exit {rc}'}"
                        " (try disabling background import in preferences)")
            return {"CANCELLED"}

        if done_blend is not None:
            try:
                self._append_result(context, done_blend)
                self._appended += 1
            except Exception as e:
                self._cleanup(context)
                self.report({"ERROR"}, f"Could not append result: {e}")
                return {"CANCELLED"}
            # Don't block the UI while the worker Blender shuts down.
            # Poll it on later timer ticks instead.
            if self._proc.poll() is None:
                self._reap.append(self._proc)
            self._file_index += 1
            if self._jobs:
                if not self._spawn_next(context):
                    self._cleanup(context)
                    return {"CANCELLED"}
                return {"RUNNING_MODAL"}
            self._cleanup(context)
            self.report({"INFO"},
                        f"Background import finished ({self._appended} file(s))")
            return {"FINISHED"}

        return {"RUNNING_MODAL"}

    def _append_result(self, context, blend_path):
        """Append the worker scene's content into the current scene."""
        pre_scenes = set(bpy.data.scenes)
        with bpy.data.libraries.load(blend_path, link=False) as (df, dt):
            dt.scenes = list(df.scenes)
        new_scenes = [s for s in bpy.data.scenes if s not in pre_scenes]
        target = context.scene.collection
        for ws in new_scenes:
            for col in list(ws.collection.children):
                ws.collection.children.unlink(col)
                target.children.link(col)
            for obj in list(ws.collection.objects):
                ws.collection.objects.unlink(obj)
                target.objects.link(obj)
            bpy.data.scenes.remove(ws)
        # Appended objects need a depsgraph pass before their matrices are valid
        context.view_layer.update()

    def cancel(self, context):
        # Called by the window manager if the modal is force-terminated
        # (file load, window closed, Blender quit). Without this the
        # headless worker would keep running as an orphan.
        self._cleanup(context)

    def _kill(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def _cleanup(self, context):
        STEPPER_OT_background_import._running = False
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        try:
            wm.progress_end()
        except Exception:
            pass
        # The user may have switched workspace mid-import. Clear the status
        # text everywhere, not just on the current workspace.
        for ws in bpy.data.workspaces:
            try:
                ws.status_text_set_internal(None)
            except Exception:
                pass
        self._kill()
        # Wait for killed/finishing workers to release their file handles
        # (Windows would otherwise block the job dir removal below).
        for p in [self._proc] + list(getattr(self, "_reap", [])):
            if p is None:
                continue
            try:
                p.wait(timeout=3)
            except Exception:
                pass
        # Best-effort job dir removal
        try:
            import shutil
            shutil.rmtree(self._job_dir, ignore_errors=True)
        except Exception:
            pass


def should_background(prefs, files, in_background_mode):
    """Route to the background path? (headless Blender always stays sync)"""
    if in_background_mode or not getattr(prefs, "background_import", False):
        return False
    try:
        total = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
    except OSError:
        return False
    return total >= getattr(prefs, "background_min_mb", 2.0) * 1e6


classes = (STEPPER_OT_background_import,)
