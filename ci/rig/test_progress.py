# SPDX-License-Identifier: GPL-3.0-or-later
"""The job reporter: its arithmetic, and that it never fails a job.

The status bar itself needs a window. What is tested here is what the
stages hand it: a percentage that only moves forward, a stage that counts
its steps, and a reporter that stays quiet instead of raising when Blender
gives it nothing to draw on.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import progress  # noqa: E402


class FakeWindowManager:
    def __init__(self, fail=False):
        self.values = []
        self.began = False
        self.ended = False
        self.fail = fail

    def progress_begin(self, low, high):
        self.began = True

    def progress_update(self, value):
        if self.fail:
            raise RuntimeError("no bar here")
        self.values.append(value)

    def progress_end(self):
        self.ended = True


class FakeWorkspace:
    def __init__(self):
        self.texts = []

    def status_text_set(self, text):
        self.texts.append(text)


class FakeContext:
    def __init__(self, wm, ws):
        self.window = object()
        self.window_manager = wm
        self.workspace = ws


def _reporter(fail=False):
    wm = FakeWindowManager(fail=fail)
    ws = FakeWorkspace()
    return progress.JobProgress(FakeContext(wm, ws)), wm, ws


def test_a_stage_with_steps_runs_from_its_first_to_its_last():
    said, wm, _ = _reporter()
    said.stage("placing the parts", 20, 85, 4)
    assert said.percent == 20
    said.step(2)
    assert said.percent == 52.5
    said.step(4)
    assert said.percent == 85
    assert wm.values == [20, 52, 85]


def test_the_percentage_never_goes_backwards_across_stages():
    said, wm, _ = _reporter()
    plan = [("preparing the scene", 0, 5, 0),
            ("reading the manifest", 5, 10, 0),
            ("placing the parts", 20, 85, 3),
            ("building the rig", 88, 96, 0),
            ("attaching the parts to the rig", 96, 100, 0)]
    for label, first, last, steps in plan:
        said.stage(label, first, last, steps)
        for i in range(1, steps + 1):
            said.step(i)
    assert wm.values == sorted(wm.values)
    assert wm.values[-1] == 96


def test_the_status_text_names_the_stage_and_counts_its_steps():
    said, _, ws = _reporter()
    said.stage("placing the parts", 20, 85, 10)
    said.step(7)
    assert ws.texts[0] == "CADder: placing the parts (1 of 10)" \
        or ws.texts[0] == "CADder: placing the parts (0 of 10)"
    assert "placing the parts (7 of 10)" in ws.texts[-1]


def test_a_stage_with_nothing_to_count_sits_at_its_start():
    said, wm, _ = _reporter()
    said.stage("reading the manifest", 5, 10)
    said.step(3)
    assert said.percent == 5
    assert wm.values == [5]


def test_a_bar_that_throws_is_dropped_rather_than_failing_the_job():
    said, wm, _ = _reporter(fail=True)
    said.stage("placing the parts", 0, 100, 2)
    said.step(1)
    said.step(2)
    said.close()
    assert wm.values == []


def test_without_a_window_the_reporter_shows_nothing():
    class Headless:
        window = None

    said = progress.JobProgress(Headless())
    said.stage("placing the parts", 0, 100, 2)
    said.step(1)
    said.close()
    assert said.percent == 50


def test_the_silent_reporter_accepts_every_call():
    progress.NONE.stage("x", 0, 100, 2)
    progress.NONE.step(1)
    progress.NONE.close()
