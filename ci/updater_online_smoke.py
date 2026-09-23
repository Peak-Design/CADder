# SPDX-License-Identifier: GPL-3.0-or-later
"""The update check obeys Blender's Allow Online Access switch.

    blender -b --factory-startup --python-exit-code 1 -P ci/updater_online_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Blender's add-on rules say an add-on makes no network request while online
access is off, in the preferences or with --offline-mode. The daily update
check read only its own preference, so it still asked GitHub.

No request leaves this test: the fetch is replaced before the check starts.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import updater as U

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


calls = []


def fake_fetch():
    calls.append(1)
    return {"tag": "v0.0.1", "url": ""}


U._fetch = fake_fetch
U.stop()

system = bpy.context.preferences.system
prefs = bpy.context.preferences.addons["CADder"].preferences
prefs.check_for_updates = True
prefs.update_last_check = ""


def run_tick():
    """One pass of the timer, as Blender runs it, and the thread it starts."""
    U._thread = None
    U._timer_running = True
    U._tick()
    if U._thread is not None:
        U._thread.join(5.0)
    U._timer_running = False
    U._thread = None


print("\n== online access off")
system.use_online_access = False
check(not bpy.app.online_access, "Blender says online access is off")
check(not U._should_check(prefs), "the update check does not run")
run_tick()
check(not calls, "and nothing is fetched (%d request(s))" % len(calls))

print("\n== online access on")
prefs.update_last_check = ""
system.use_online_access = True
check(bpy.app.online_access, "Blender says online access is on")
check(U._should_check(prefs), "the daily check runs")
run_tick()
check(len(calls) == 1, "and fetches once (%d request(s))" % len(calls))

system.use_online_access = False

if FAILS:
    print("\nupdater_online_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nupdater_online_smoke: OK: the update check follows Allow Online "
      "Access")
