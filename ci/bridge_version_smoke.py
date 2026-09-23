# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: the addon says when the CADder Bridge in SolidWorks does
not match it, as soon as the add-in runs, and says what to update.

    blender -b --factory-startup --python-exit-code 1 -P ci/bridge_version_smoke.py

Oscar, 2026-09-23: "we should add a warning to both side of the SW bridge
addons if there are version mismatches ... Blender side print the info,
also with download link in the info panel where SA listener status is
shown. We should show this info immediately to the user and not wait for a
send."

The two halves work together when the first two numbers of their versions
match. The add-in writes its version in its registry file when SolidWorks
starts, and the bridge reads those files from its pump. The registry of
the add-ins goes to a temporary folder here: no add-in on this machine is
read, and nothing is written where one would look.
"""

import json
import os
import shutil
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import bridge  # noqa: E402
from CADder.rig import cad_link  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


# 1. The rule: the first two numbers.
for a, b, want in (("1.1.0", "1.1.9", True), ("1.1.1", "1.1.0", True),
                   ("1.2.0", "1.1.9", False), ("2.1.0", "1.1.0", False),
                   ("1.1.0.0", "1.1.3", True),      # four parts
                   ("1.2.0.0", "1.1.3", False),
                   # the add-in says "v1.1.0" (AddIn.AddInVersion)
                   ("v1.1.0", "1.1.3", True), ("v1.2.0", "1.1.3", False),
                   (None, "1.1.0", None), ("unknown", "1.1.0", None),
                   ("", "1.1.0", None)):
    check(bridge.versions_match(a, b) is want,
          "%r and %r match: %r" % (a, b, want))

# 2. What it says, and which half to update.
m = bridge.mismatch_for("1.2.0", "1.1.1", "CADder")
check(m is not None and m["url"] == bridge.CADDER_RELEASES
      and m["advice"] == "Update CADder to 1.2"
      and m["button"] == "Download CADder 1.2",
      "a newer add-in: update CADder, with its download (%s)" % m)
check(m is not None and m["versions"] == ["CADder Bridge: 1.2.0",
                                          "CADder: 1.1.1"],
      "the panel names both versions (%s)" % (m and m["versions"]))
check(m is not None and "CADder Bridge 1.2.0 does not match CADder 1.1.1"
      in m["message"] and m["url"] in m["message"],
      "the console line says it all, with the download (%s)"
      % (m and m["message"]))
m = bridge.mismatch_for("v1.1.0", "1.2.0", "CADder")
check(m is not None and m["url"] == bridge.BRIDGE_RELEASES
      and m["advice"] == "Update CADder Bridge to 1.2",
      "an older add-in: update CADder Bridge, with its download (%s)" % m)
check(m is not None and m["versions"][0] == "CADder Bridge: 1.1.0",
      "the version reads as a person reads it (%s)"
      % (m and m["versions"]))
m = bridge.mismatch_for("1.3.0", "1.2.5", "CADder Pro")
check(m is not None and not m["url"] and "CADder Pro" in m["advice"],
      "an older CADder Pro: no public download to point to (%s)" % m)
check(bridge.mismatch_for("1.2.0", "1.2.5", "CADder Pro") is None,
      "CADder Pro 1.2.5 works with CADder Bridge 1.2.0")
check(bridge.mismatch_for(None, "1.1.1", "CADder") is None,
      "an add-in that does not say its version gives no warning")
# The Pro build carries routing/ and calls itself CADder Pro.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
name = "CADder Pro" if os.path.isdir(os.path.join(_REPO, "routing")) else "CADder"
check(bridge._addon_name() == name,
      "this build calls itself %s (%s)" % (name, bridge._addon_name()))
check(bridge._instance_info().get("addon_name") == bridge._addon_name(),
      "the registry file of this Blender says which addon it runs")

# 3. Found from the registry files of the add-ins, without a send.
tmp = tempfile.mkdtemp(prefix="cadder_version_")
saved = cad_link._REGISTRY
cad_link._REGISTRY = tmp
try:
    def addin(pid, version, name=None):
        with open(os.path.join(tmp, "%s.json" % (name or pid)), "w",
                  encoding="utf-8") as f:
            json.dump({"pid": pid, "port": 50123, "token": "t",
                       "addin_version": version}, f)

    ours = bridge._addon_version()
    major, minor = (int(x) for x in ours.split(".")[:2])
    newer = "v%d.%d.0" % (major, minor + 1)
    bridge._state["mismatch"] = None
    bridge._check_addin_version(force=True)
    check(bridge.version_mismatch() is None, "no add-in: no warning")

    # An add-in whose process has gone does not count.
    addin(999999, newer)
    bridge._check_addin_version(force=True)
    check(bridge.version_mismatch() is None,
          "the file of an add-in that has gone gives no warning")

    # A running one (this process stands in for SolidWorks).
    addin(os.getpid(), newer)
    bridge._check_addin_version(force=True)
    found = bridge.version_mismatch()
    check(found is not None and ("CADder Bridge: " + newer[1:])
          in found["versions"],
          "a running add-in of %s is found at once (%s)" % (newer, found))

    # The panel: the title, the advice and the download.
    class Layout:
        def __init__(self, rec):
            self.rec = rec
            self.alert = False

        def box(self):
            return self

        def column(self, align=False):
            return self

        def row(self, align=False):
            return self

        def label(self, text="", icon="NONE"):
            self.rec.append(("label", text))

        def operator(self, idname, text="", icon="NONE"):
            rec = self.rec

            class Props:
                def __setattr__(self, key, value):
                    rec.append(("operator", idname, text, key, value))
            return Props()
    drawn = []
    bridge.draw_version_mismatch(Layout(drawn))
    check(all(("label", t) in drawn for t in
              [found["title"]] + found["versions"] + [found["advice"]]),
          "the panel shows the title, both versions and the advice (%s)"
          % drawn)
    check(("operator", "wm.url_open", found["button"], "url",
           bridge.CADDER_RELEASES) in drawn,
          "the panel has the download button (%s)" % drawn)

    # The same add-in, updated: the warning goes.
    addin(os.getpid(), "v" + ours)
    bridge._check_addin_version(force=True)
    check(bridge.version_mismatch() is None,
          "a matching add-in takes the warning away")
    drawn = []
    bridge.draw_version_mismatch(Layout(drawn))
    check(not drawn, "the panel draws nothing when the versions match")

    # The listener goes off: so does the warning.
    addin(os.getpid(), newer)
    bridge._check_addin_version(force=True)
    bridge.stop()
    check(bridge.version_mismatch() is None,
          "stopping the listener takes the warning away")
finally:
    cad_link._REGISTRY = saved
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print("\nbridge_version_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nbridge_version_smoke: OK")
