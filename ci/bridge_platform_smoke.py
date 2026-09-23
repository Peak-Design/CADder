# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: the SolidWorks Bridge is only in the Windows version.

    blender -b --factory-startup --python-exit-code 1 -P ci/bridge_platform_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

SolidWorks runs only on Windows, so on macOS and Linux nothing can connect
to the bridge. There the preferences have no SolidWorks Bridge switch, the
sidebar shows none of the link, and the listener never starts, also when
the preferences came from a Windows machine with the switch on.

The test runs on Windows and sets bridge.SUPPORTED to False for the other
platforms. Every reader takes the value when it runs, so this is the same
code path. The registry goes to a temporary folder: a CAD add-in on this
machine never finds this Blender.
"""

import os
import shutil
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, main  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


class Layout:
    """Stands in for a UILayout and writes down what a draw asks for."""

    def __init__(self, seen):
        object.__setattr__(self, "seen", seen)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            if name == "prop" and len(args) >= 2:
                self.seen.append(("prop", args[1]))
            elif name == "operator" and args:
                self.seen.append(("operator", args[0]))
            elif name == "label":
                self.seen.append(("label", kwargs.get("text", "")))
            return Layout(self.seen)
        return call

    def __setattr__(self, name, value):
        pass


class Holder:
    """A panel or the preferences, with a layout that writes down."""

    def __init__(self, target, seen):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "layout", Layout(seen))

    def __getattr__(self, name):
        return getattr(self._target, name)


def drawn(cls, target=None):
    seen = []
    cls.draw(Holder(target, seen), bpy.context)
    return seen


def main_():
    folder = tempfile.mkdtemp(prefix="cadder_platform_")
    os.environ["LOCALAPPDATA"] = folder
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    print("   CADder from", os.path.dirname(main.__file__))
    prefs = bpy.context.preferences.addons["CADder"].preferences
    from CADder.rig import ui as rig_ui

    # A part that came over the link on a Windows machine.
    part = bpy.data.objects.new("part", bpy.data.meshes.new("part"))
    part["RIG_component_id"] = "c001"
    bpy.context.scene.collection.objects.link(part)

    try:
        for supported in (True, False):
            bridge.SUPPORTED = supported
            print("\n== %s" % ("Windows" if supported else "macOS and Linux"))
            bridge.stop()
            prefs.enable_bridge = False
            prefs.enable_bridge = True

            seen = drawn(main.STEP_AddonPreferences, prefs)
            check((("prop", "enable_bridge") in seen) == supported,
                  "the SolidWorks Bridge switch is %s"
                  % ("drawn" if supported else "not drawn"))
            check((("prop", "cad_link_advanced") in seen) == supported,
                  "the STEP Rig Panel switch is %s"
                  % ("drawn" if supported else "not drawn"))
            check(bridge.is_running() == supported,
                  "the switch %s the listener"
                  % ("starts" if supported else "does not start"))

            bridge.stop()
            bridge.register()
            check(bridge.is_running() == supported,
                  "a preference already on %s the listener at startup"
                  % ("starts" if supported else "does not start"))

            check(rig_ui._cad_link_enabled(bpy.context) == supported,
                  "the panels of the link %s"
                  % ("show" if supported else "do not show"))

            seen = drawn(main.STEP_PT_STEPper_Info)
            status = [s for s in seen if s[0] == "label"]
            check(bool(status) == supported,
                  "the line under the name of the addon %s the link (%s)"
                  % ("shows" if supported else "does not show", status))

            seen = drawn(main.CADLINK_PT_quality)
            check((("operator", "cadlink.update_from_cad") in seen)
                  == supported,
                  "Mesh Quality %s Rebuild from CAD"
                  % ("offers" if supported else "does not offer"))
            check((("operator", "stepper.regenerate") in seen)
                  != supported,
                  "and %s Rebuild from STEP"
                  % ("not" if supported else "offers"))

            check(bpy.ops.cadlink.update_from_cad.poll() == supported,
                  "Rebuild from CAD %s"
                  % ("can run" if supported else "cannot run"))
    finally:
        bridge.SUPPORTED = sys.platform == "win32"
        bridge.stop()
        prefs.enable_bridge = False
        shutil.rmtree(folder, ignore_errors=True)

    if FAILS:
        raise SystemExit("bridge_platform_smoke: FAIL: %d check(s): %s"
                         % (len(FAILS), " | ".join(FAILS)))
    print("\nbridge_platform_smoke: OK: the bridge is in the Windows version "
          "only")


main_()
