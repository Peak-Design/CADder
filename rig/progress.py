# SPDX-License-Identifier: GPL-3.0-or-later
"""What a long CAD Link job is doing, shown in Blender.

A send of a large assembly builds hundreds of objects, their materials and
then the rig, all inside one operator. Blender draws nothing while that
runs, so the application looks stopped. This writes the stage into the
status bar and moves the cursor's progress wheel.

Nothing here is allowed to fail a job. Blender in the background has no
window and no workspace, and a job can run from a timer where the context
is thinner still, so every call is guarded and the reporter simply shows
nothing when it cannot.

The add-in reports the same way on the SolidWorks side, and the two use
the same stage names, so a slow send reads as one timeline across both
applications.
"""

try:
    import bpy
except ImportError:      # the tests run this module outside Blender
    bpy = None

__all__ = ["JobProgress", "NONE"]


class JobProgress:
    """Stages of one job, and the steps inside a stage.

    Stages take a share of the bar, given as `first` and `last` percent,
    which is written where the stage is. A stage with countable work also
    gives the number of steps and reports them as they finish.
    """

    def __init__(self, context=None, title="CADder"):
        self.title = title
        self.stage_label = None
        self.percent = 0.0
        self._first = 0.0
        self._last = 100.0
        self._steps = 0
        self._window_manager = None
        self._workspace = None
        self._shown = -1
        if context is None and bpy is not None:
            context = getattr(bpy, "context", None)
        if context is None:
            return
        try:
            if getattr(context, "window", None) is None:
                return          # background: no bar to draw on
            self._window_manager = context.window_manager
            self._workspace = getattr(context, "workspace", None)
            self._window_manager.progress_begin(0, 100)
        except Exception:
            self._window_manager = None

    def stage(self, label, first, last, steps=0):
        self.stage_label = label
        self._first = float(first)
        self._last = float(max(first, last))
        self._steps = int(steps)
        self._place(0)

    def step(self, done):
        self._place(done)

    def close(self):
        try:
            if self._window_manager is not None:
                self._window_manager.progress_end()
        except Exception:
            pass
        self._window_manager = None
        try:
            if self._workspace is not None:
                self._workspace.status_text_set(None)
        except Exception:
            pass
        self._workspace = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ── inside ──────────────────────────────────────────────────────────
    def _place(self, done):
        span = self._last - self._first
        if self._steps > 0:
            share = min(1.0, max(0.0, float(done) / self._steps))
        else:
            share = 0.0
        self.percent = self._first + span * share
        rounded = int(self.percent)
        if rounded == self._shown and done != 0:
            return
        self._shown = rounded
        text = "%s: %s" % (self.title, self.stage_label or "working")
        if self._steps > 1:
            text += " (%d of %d)" % (min(done, self._steps), self._steps)
        try:
            if self._workspace is not None:
                self._workspace.status_text_set(text)
        except Exception:
            self._workspace = None
        try:
            if self._window_manager is not None:
                self._window_manager.progress_update(rounded)
        except Exception:
            self._window_manager = None


class _Silent(JobProgress):
    """Shows nothing. Used where a caller gives no reporter, so the call
    sites stay free of "if progress is not None"."""

    def __init__(self):
        self.title = "CADder"
        self.stage_label = None
        self.percent = 0.0
        self._first = 0.0
        self._last = 100.0
        self._steps = 0
        self._window_manager = None
        self._workspace = None
        self._shown = -1

    def stage(self, label, first, last, steps=0):
        pass

    def step(self, done):
        pass

    def close(self):
        pass


NONE = _Silent()
