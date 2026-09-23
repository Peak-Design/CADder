# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the Material Database list.

    blender -b --factory-startup --python-exit-code 1 -P ci/matdb_panel_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

What the list does for the parts of the scene:

  * The eye hides the entries that no part of the scene has.
  * Select picks the parts that have the material of an entry, also a
    collection instance whose prototype has it, and not a hidden part.
  * The trash removes an entry, and Save removes it from the database
    with its material.
  * Load puts the materials of the database in the file, and they stay
    there when the file is saved. Blender does not save a material with
    no users, so they were gone when the file was opened again, and an
    entry for another material could not pick them.
  * A change in the list marks it unsaved until Save or Load.
"""

import os
import sys
import tempfile
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import main  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def enable():
    bpy.ops.preferences.addon_enable(module="CADder")
    return bpy.context.preferences.addons["CADder"].preferences


def part(name, originals, materials=None, collection=None):
    """A CAD part as an import leaves it: tagged, with the original name of
    each slot in STEP_materials."""
    import json
    me = bpy.data.meshes.new(name)
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    for mat_name in materials or originals:
        mat = (bpy.data.materials.get(mat_name)
               or bpy.data.materials.new(mat_name))
        me.materials.append(mat)
    obj = bpy.data.objects.new(name, me)
    obj["SWMESH_file"] = "rig"
    obj["STEP_materials"] = json.dumps(list(originals))
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def names(items):
    return sorted(i.original_name for i in items)


def db_materials(path):
    with bpy.data.libraries.load(path, link=False) as (data_from, _to):
        return sorted(data_from.materials)


def selected():
    return sorted(o.name for o in bpy.context.selected_objects)


def main_():
    tmp = tempfile.mkdtemp(prefix="cadder_matdb_panel_")
    db_dir = os.path.join(tmp, "MaterialDB")
    os.makedirs(db_dir)

    # ── A database, written from another project ────────────────────────
    bpy.ops.wm.read_factory_settings(use_empty=True)
    prefs = enable()
    prefs.matdb_dir = db_dir
    for name in ("Gold PBR", "Brushed Steel", "Rubber", "Chrome"):
        bpy.data.materials.new(name)
    db_path = os.path.join(db_dir, "workshop.blend")
    main._write_material_database(db_path, {
        "SW gold": "Gold PBR", "SW steel": "Brushed Steel",
        "SW rubber": "Rubber", "SW chrome": "Chrome"})

    # ── A new project: Load brings the materials, and they stay ─────────
    print("\n== Load keeps the materials of the database in the file")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    prefs = enable()
    prefs.matdb_dir = db_dir
    prefs.active_matdb = "workshop"
    check(not bpy.data.materials, "the new file has no materials")
    result = bpy.ops.stepper.mat_db_refresh()
    check(result == {"FINISHED"}, "Load finished (%s)" % result)
    stepper = bpy.context.scene.stepper
    check(names(stepper.mat_db_mappings)
          == ["SW chrome", "SW gold", "SW rubber", "SW steel"],
          "the list shows the entries (%s)" % names(stepper.mat_db_mappings))
    kept = sorted(m.name for m in bpy.data.materials if m.use_fake_user)
    check(kept == ["Brushed Steel", "Chrome", "Gold PBR", "Rubber"],
          "every material of the database has a fake user (%s)" % kept)
    check(not stepper.mat_db_dirty, "a list just loaded is not unsaved")
    blend = os.path.join(tmp, "project.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    after = sorted(m.name for m in bpy.data.materials)
    check(after == ["Brushed Steel", "Chrome", "Gold PBR", "Rubber"],
          "the materials are still in the file when it is opened again "
          "(%s)" % after)

    # ── The parts of the scene ──────────────────────────────────────────
    stepper = bpy.context.scene.stepper
    scene = bpy.context.scene
    alpha = part("alpha", ["SW gold"])
    beta = part("beta", ["SW gold", "SW steel"])
    hidden = part("hidden", ["SW steel"])
    hidden.hide_set(True)
    backdrop = bpy.data.objects.new("backdrop", bpy.data.meshes.new("bd"))
    backdrop.data.materials.append(bpy.data.materials.new("SW rubber"))
    scene.collection.objects.link(backdrop)
    # A collection instance: its prototype holds the chrome.
    protos = bpy.data.collections.new("protos")
    scene.collection.children.link(protos)
    part("proto", ["SW chrome"], collection=protos)
    for layer in bpy.context.view_layer.layer_collection.children:
        if layer.collection == protos:
            layer.exclude = True
    handle = bpy.data.objects.new("handle", None)
    handle.instance_type = "COLLECTION"
    handle.instance_collection = protos
    scene.collection.objects.link(handle)
    bpy.context.view_layer.update()

    print("\n== which entries the scene uses")
    used, locked = main._scene_usage(scene)
    check(sorted(used) == ["SW chrome", "SW gold", "SW steel"],
          "gold, steel and the instanced chrome. Not the rubber of the "
          "backdrop, which is not a CAD part (%s)" % sorted(used))
    check(locked == 0, "no part is locked (%d)" % locked)

    print("\n== the eye")
    items = [i.original_name for i in stepper.mat_db_mappings]
    bit = 1 << 30

    def shown(pattern_hits, has_pattern, invert, show_unused):
        flags = main._mapping_flags(items, pattern_hits, has_pattern, invert,
                                    show_unused, used, bit)
        # Blender inverts every flag when Invert is set.
        return [n for n, f in zip(items, flags) if bool(f & bit) != invert]

    every = [True] * len(items)
    check(shown(every, False, False, True) == items,
          "open, it shows every entry")
    check(shown(every, False, False, False)
          == ["SW chrome", "SW gold", "SW steel"],
          "closed, it shows only the entries the scene uses (%s)"
          % shown(every, False, False, False))
    check(shown(every, False, True, False)
          == ["SW chrome", "SW gold", "SW steel"],
          "Invert with no name filter changes nothing (%s)"
          % shown(every, False, True, False))
    gold_hits = [n == "SW gold" for n in items]
    check(shown(gold_hits, True, False, False) == ["SW gold"],
          "a name filter and the eye together (%s)"
          % shown(gold_hits, True, False, False))
    check(shown(gold_hits, True, True, False) == ["SW chrome", "SW steel"],
          "Invert turns the name filter around and the eye still hides "
          "the rubber (%s)" % shown(gold_hits, True, True, False))

    print("\n== Select")
    bpy.ops.object.select_all(action="DESELECT")
    backdrop.select_set(True)
    result = bpy.ops.stepper.mat_db_select_entry(original_name="SW gold")
    check(result == {"FINISHED"}, "Select finished (%s)" % result)
    check(selected() == ["alpha", "beta"],
          "the gold parts, and only them (%s)" % selected())
    check(bpy.context.view_layer.objects.active in (alpha, beta),
          "one of them is active")
    bpy.ops.stepper.mat_db_select_entry(original_name="SW steel")
    check(selected() == ["beta"],
          "the hidden steel part is left out (%s)" % selected())
    bpy.ops.stepper.mat_db_select_entry(original_name="SW chrome",
                                        extend=True)
    check(selected() == ["beta", "handle"],
          "Shift adds the instance of the chrome prototype (%s)" % selected())
    result = bpy.ops.stepper.mat_db_select_entry(original_name="SW rubber")
    check(result == {"CANCELLED"} and selected() == ["beta", "handle"],
          "an entry no part has selects nothing (%s)" % result)
    shown_parts, hidden_count = main._parts_with(bpy.context.view_layer,
                                                 "SW steel")
    check([o.name for o in shown_parts] == ["beta"] and hidden_count == 1,
          "the tooltip counts one part and one hidden part")

    print("\n== the list after a change in the scene")
    part("delta", ["SW rubber"])
    bpy.context.view_layer.update()
    used, _ = main._scene_usage(scene)
    check("SW rubber" in used, "a new part is seen without a reload (%s)"
          % sorted(used))
    alpha.location.x = 3.0
    bpy.context.view_layer.update()
    check(not main._usage["stale"], "a part that only moves costs no rescan")

    print("\n== the trash, and Save")
    check(not stepper.mat_db_dirty, "nothing changed yet")
    result = bpy.ops.stepper.mat_db_remove_entry(original_name="SW rubber")
    check(result == {"FINISHED"}, "the entry went (%s)" % result)
    check(names(stepper.mat_db_mappings)
          == ["SW chrome", "SW gold", "SW steel"],
          "the list has no rubber entry (%s)" % names(stepper.mat_db_mappings))
    check(stepper.mat_db_dirty, "the list is marked unsaved")
    check(main._read_matdb_mappings(db_path).get("SW rubber") == "Rubber",
          "the database keeps it until Save")
    result = bpy.ops.stepper.mat_db_save()
    check(result == {"FINISHED"} and not stepper.mat_db_dirty,
          "Save wrote the list and cleared the mark")
    check("SW rubber" not in main._read_matdb_mappings(db_path),
          "the entry is gone from the database")
    check(db_materials(db_path) == ["Brushed Steel", "Chrome", "Gold PBR"],
          "and its material with it (%s)" % db_materials(db_path))

    print("\n== an edit marks the list unsaved, and Load clears it")
    stepper.mat_db_mappings[0].replacement_name = "Gold PBR"
    check(stepper.mat_db_dirty, "picking another material marks it")
    bpy.ops.stepper.mat_db_refresh()
    check(not stepper.mat_db_dirty, "Load clears the mark")
    check(stepper.mat_db_mappings[0].replacement_name == "Chrome",
          "and shows the database again")

    print("\n== a large scene")
    many = bpy.data.collections.new("many")
    scene.collection.children.link(many)
    for i in range(10000):
        obj = bpy.data.objects.new("p%05d" % i, alpha.data)
        obj["SWMESH_file"] = "big"
        obj["STEP_materials"] = alpha["STEP_materials"]
        many.objects.link(obj)
    main._usage_stale()
    start = time.perf_counter()
    main._scene_usage(scene)
    first = time.perf_counter() - start
    start = time.perf_counter()
    for _ in range(100):
        main._scene_usage(scene)
    again = (time.perf_counter() - start) / 100
    print("   10000 parts: %.1f ms to scan, %.4f ms kept" % (first * 1000,
                                                             again * 1000))
    check(first < 1.0, "the scan of 10000 parts takes %.2f s" % first)
    check(again < 0.001, "a redraw with nothing changed does not scan")

    if FAILS:
        raise SystemExit("matdb_panel_smoke: FAIL: %d check(s): %s"
                         % (len(FAILS), "; ".join(FAILS)))
    print("\nmatdb_panel_smoke: OK: the eye, Select, the trash, Load that "
          "keeps the materials, and the unsaved mark")


main_()
