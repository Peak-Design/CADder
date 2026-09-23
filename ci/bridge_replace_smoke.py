# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: a send tidies only what its own import made.

    blender -b --factory-startup --python-exit-code 1 -P ci/bridge_replace_smoke.py

A send replaces the last import of its assembly, and then removes the
empty collections that import left. It removed every empty collection
whose name started with the file name, so a user's own empty collection
("gearbox renders") went with it. The cleanup after relink removed every
childless STEP empty in the file, also those of imports the send had
nothing to do with. And the replace took every import of a file with the
same name, from any folder, so a vendor's gearbox.step went with a send of
gearbox.SLDASM.
"""

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge  # noqa: E402


def fail(msg):
    raise SystemExit("bridge_replace_smoke: FAIL: %s" % msg)


def collection(name, parent, step_file=None):
    col = bpy.data.collections.new(name)
    if step_file is not None:
        col["STEP_file"] = step_file
        col["STEP_role"] = "flat"
    parent.children.link(col)
    return col


def empty(name, step_file, home):
    obj = bpy.data.objects.new(name, None)
    obj["STEP_file"] = step_file
    obj["STEP_uuid"] = name
    obj["STEP_name"] = name
    home.objects.link(obj)
    return obj


def run_replace():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    root = bpy.context.scene.collection
    export = os.path.join(tempfile.gettempdir(), "gearbox.step")
    flat = collection("gearbox.flat", root, export)
    part = collection("gearbox part", flat, export)
    empty("gear", export, part)
    # The user's own collections: one beside the import, one inside it.
    collection("gearbox renders", root)
    collection("gearbox notes", flat)

    stages = {}
    bridge._remove_previous_import(
        os.path.join(tempfile.gettempdir(), "gearbox.swmesh"), stages,
        by_stem=True)
    if stages["replace"]["removed_objects"] != 1:
        fail("the import was not replaced: %s" % stages)
    for name in ("gearbox renders", "gearbox notes"):
        if bpy.data.collections.get(name) is None:
            fail("the user's collection %r was removed" % name)
    if bpy.data.collections.get("gearbox part") is not None:
        fail("the emptied import collection was left behind")


def run_same_name():
    """Only an earlier send of the assembly goes, not a file that only has
    its name."""
    base = os.path.join(tempfile.gettempdir(), "cadder_same_name")
    sends = {
        "same folder": os.path.join(base, "exports", "gearbox", "gearbox.step"),
        "other root": os.path.join(base, "project", "gearbox", "gearbox.step"),
        "older add-in": os.path.join(base, "exports", "gearbox.step"),
    }
    vendor = os.path.join(base, "Downloads", "gearbox.step")
    # Exported by hand and rigged: the same assembly, from any folder, and
    # all of it goes, also a part the rig did not match.
    by_hand = os.path.join(base, "Desktop", "gearbox.step")
    for new_file, by_stem in (("gearbox.swmesh", True), ("gearbox.step", False)):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        root = bpy.context.scene.collection
        for label, path in sends.items():
            empty(label, path, collection(label + " import", root, path))
        empty("vendor gear", vendor, collection("vendor import", root, vendor))
        hand = collection("rigged import", root, by_hand)
        empty("rigged gear", by_hand, hand)["RIG_group"] = "g001"
        empty("rigged bolt", by_hand, hand)
        stages = {}
        bridge._remove_previous_import(
            os.path.join(base, "exports", "gearbox", new_file), stages,
            by_stem=by_stem)
        for label in sends:
            if bpy.data.objects.get(label) is not None:
                fail("%s: the send from %r was not replaced" % (new_file, label))
        if bpy.data.objects.get("vendor gear") is None:
            fail("%s: an import of another gearbox.step was removed" % new_file)
        if bpy.data.collections.get("vendor import") is None:
            fail("%s: the other import's collection was removed" % new_file)
        for label in ("rigged gear", "rigged bolt"):
            if bpy.data.objects.get(label) is not None:
                fail("%s: %r of the rigged import was not replaced" % (new_file, label))
        if stages["replace"]["removed_objects"] != len(sends) + 2:
            fail("%s: removed %s" % (new_file, stages))


def run_cleanup():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    root = bpy.context.scene.collection
    mine = os.path.join(tempfile.gettempdir(), "lift.step")
    other = os.path.join(tempfile.gettempdir(), "vendor", "motor.step")
    empty("lift occurrence", mine, root)
    empty("motor occurrence", other, root)
    stages = {}
    bridge._cleanup_leftover_empties(stages, mine)
    if bpy.data.objects.get("lift occurrence") is not None:
        fail("the job's own childless empty was left")
    if bpy.data.objects.get("motor occurrence") is None:
        fail("the cleanup removed an empty of a different import")
    if stages["cleanup"]["removed_empties"] != 1:
        fail("the cleanup counted %s" % stages["cleanup"])


def main():
    run_replace()
    run_same_name()
    run_cleanup()
    print("bridge_replace_smoke: OK: a send removed its own import's "
          "collections and empties and the earlier sends of its assembly, "
          "and left the user's and other imports'")


main()
