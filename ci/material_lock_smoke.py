# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Lock Materials.

    blender -b --factory-startup --python-exit-code 1 -P ci/material_lock_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

A locked part keeps the materials it has through everything that puts
other materials on a part: Apply of the material database, a refine and a
refresh from the CAD application (also one that changed the appearance),
a send that replaces every object, a collection instance whose prototype
is replaced, Regenerate, Rebuild Selected and a refresh of a STEP file.
Each unlocked part beside it takes the change, so the test shows that the
change did happen.
"""

import json
import os
import shutil
import struct
import sys
import tempfile

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from CADder import main, material_lock  # noqa: E402
from CADder.rig import manifest as man_mod, native_import, swmesh  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def manifest(parts):
    """parts: (component id, path, x)."""
    return man_mod.parse({
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "lock.step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            {"id": cid, "sw_path": path, "step_name": path,
             "step_occurrence_path": None, "sw_persistent_id": "p" + path,
             "transform": _t(x)}
            for cid, path, x in parts],
        "rigid_groups": [
            {"id": "g%03d" % i, "name": path, "components": [cid],
             "grounded": i == 0, "frame": None, "bbox_diag": 0.2}
            for i, (cid, path, x) in enumerate(parts)],
        "joints": [], "loops": [], "warnings": [],
    })


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


# One color for each appearance, whatever its place in the file: the color
# is part of what makes an appearance the same one on the next send.
COLORS = {"polished gold": (0.97, 0.88, 0.6),
          "brushed steel": (0.6, 0.6, 0.62),
          "satin chrome": (0.8, 0.8, 0.85)}


def write_mesh(path, materials, definitions, instances):
    """materials: names. definitions: (id, name, material of each
    triangle). instances: (definition id, component id, name, path, x)."""
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", len(materials), len(definitions),
                        len(instances), 0)
    for name in materials:
        body += _text(name) + struct.pack("<6f", *COLORS[name], 1.0, 0.4,
                                          1.0)
        body += _text("") + struct.pack("<I", 0)
    for did, name, per_triangle in definitions:
        triangles = len(per_triangle)
        n = triangles + 2
        verts = []
        for i in range(n):
            verts.extend([float(i) * 0.01, float(i * i % 3) * 0.01, 0.0])
        tris = []
        for i in range(triangles):
            tris.extend([0, i + 1, i + 2])
        body += struct.pack("<i", did) + _text(name)
        body += struct.pack("<II", n, triangles)
        body += struct.pack("<%df" % len(verts), *verts)
        body += struct.pack("<%di" % len(tris), *tris)
        body += struct.pack("<%di" % triangles, *per_triangle)
    for did, cid, name, where, x in instances:
        body += (struct.pack("<i", did) + _text(cid) + _text(name)
                 + _text(where)
                 + struct.pack("<16d", *[v for row in _t(x) for v in row])
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


PARTS = [("c001", "alpha-1", 0.0), ("c002", "beta-1", 0.2),
         ("c003", "gamma-1", 0.4)]
INSTANCES = [(1, "c001", "alpha", "alpha-1", 0.0),
             (2, "c002", "beta", "beta-1", 0.2),
             (3, "c003", "gamma", "gamma-1", 0.4)]


def sends(tmp):
    """Three exports of one assembly. The first: alpha and beta gold,
    gamma gold and steel. The second: every part finer. The third: alpha
    is satin chrome now, and the materials come in another order, so
    gamma's slots are steel, then gold."""
    first = write_mesh(
        os.path.join(tmp, "lock.swmesh"), ["polished gold", "brushed steel"],
        [(1, "alpha", [0, 0]), (2, "beta", [0, 0]), (3, "gamma", [0, 1])],
        INSTANCES)
    finer = write_mesh(
        os.path.join(tmp, "lock2.swmesh"), ["polished gold", "brushed steel"],
        [(1, "alpha", [0] * 4), (2, "beta", [0] * 4),
         (3, "gamma", [0, 1, 0, 1])],
        INSTANCES)
    changed = write_mesh(
        os.path.join(tmp, "lock3.swmesh"),
        ["brushed steel", "satin chrome", "polished gold"],
        [(1, "alpha", [1] * 6), (2, "beta", [2] * 4),
         (3, "gamma", [2, 0, 2, 0])],
        INSTANCES)
    return first, finer, changed


def by_path(path):
    for obj in bpy.data.objects:
        if obj.get("SWMESH_path") == path and obj.get("SWMESH_file"):
            return obj
    return None


def slots(obj):
    holders = material_lock.holders(obj)
    return [m.name if m else None for h in holders for m in h.data.materials]


def select(*objects):
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]


def enable(db_dir):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    prefs = bpy.context.preferences.addons["CADder"].preferences
    prefs.matdb_dir = db_dir
    return prefs


def write_db(db_dir, name, mappings):
    for material in set(mappings.values()):
        if bpy.data.materials.get(material) is None:
            bpy.data.materials.new(material)
    main._write_material_database(os.path.join(db_dir, name + ".blend"),
                                  mappings)


def direct_link(tmp, db_dir):
    print("\n== the live link")
    prefs = enable(db_dir)
    write_db(db_dir, "shop", {"SW polished gold": "DB Gold",
                              "SW brushed steel": "DB Steel",
                              "SW satin chrome": "DB Chrome"})
    first, finer, changed = sends(tmp)
    m = manifest(PARTS)
    native_import.build(bpy.context, first, manifest=m)
    alpha, beta, gamma = (by_path(p) for p in ("alpha-1", "beta-1",
                                                "gamma-1"))
    check(slots(gamma) == ["SW polished gold", "SW brushed steel"],
          "the send gave gamma its two appearances (%s)" % slots(gamma))

    # The user's work: a material of their own on alpha, and gamma kept on
    # the CAD appearances although the database has entries for them.
    mine = bpy.data.materials.new("My Gold")
    alpha.data.materials[0] = mine
    select(alpha, gamma)
    result = bpy.ops.stepper.material_lock(lock=True)
    check(result == {"FINISHED"}, "Lock Materials finished (%s)" % result)
    check(material_lock.is_locked(alpha) and material_lock.is_locked(gamma)
          and not material_lock.is_locked(beta), "alpha and gamma are locked")

    print("\n== Apply of the database")
    prefs.active_matdb = "shop"
    result = bpy.ops.stepper.mat_db_apply()
    check(result == {"FINISHED"}, "Apply finished (%s)" % result)
    check(slots(alpha) == ["My Gold"], "alpha keeps its own material (%s)"
          % slots(alpha))
    check(slots(gamma) == ["SW polished gold", "SW brushed steel"],
          "gamma keeps the CAD appearances (%s)" % slots(gamma))
    check(slots(beta) == ["DB Gold"], "beta takes the database (%s)"
          % slots(beta))

    print("\n== a refine")
    native_import.refine(bpy.context, finer)
    # Six points: four triangles. Tris to Quads may pair them, so the
    # faces are not counted.
    check(len(alpha.data.vertices) == 6, "alpha has the finer mesh (%d)"
          % len(alpha.data.vertices))
    check(slots(alpha) == ["My Gold"], "alpha keeps its own material (%s)"
          % slots(alpha))
    check(slots(gamma) == ["SW polished gold", "SW brushed steel"],
          "gamma keeps the CAD appearances (%s)" % slots(gamma))
    check(slots(beta) == ["DB Gold"], "beta takes the database again (%s)"
          % slots(beta))

    print("\n== a refresh that changed the appearance")
    native_import.update(bpy.context, changed, manifest=m)
    alpha, beta, gamma = (by_path(p) for p in ("alpha-1", "beta-1",
                                                "gamma-1"))
    check(len(alpha.data.vertices) == 8, "alpha has the new geometry (%d)"
          % len(alpha.data.vertices))
    check(json.loads(alpha["STEP_materials"]) == ["SW satin chrome"],
          "alpha is satin chrome in the CAD application now")
    check(slots(alpha) == ["My Gold"], "alpha keeps its own material (%s)"
          % slots(alpha))
    check(json.loads(gamma["STEP_materials"])
          == ["SW brushed steel", "SW polished gold"],
          "gamma's slots come in another order now")
    check(slots(gamma) == ["SW brushed steel", "SW polished gold"],
          "each keeps its own appearance, by name and not by place (%s)"
          % slots(gamma))

    print("\n== a send that replaces every object")
    old = alpha
    native_import.build(bpy.context, changed, manifest=m)
    alpha, beta, gamma = (by_path(p) for p in ("alpha-1", "beta-1",
                                                "gamma-1"))
    check(material_lock.is_locked(alpha) and material_lock.is_locked(gamma)
          and not material_lock.is_locked(beta),
          "the new alpha and gamma are locked, beta is not")
    try:
        old.name
        check(False, "the send kept the old object")
    except ReferenceError:
        check(True, "the send made new objects")
    check(slots(alpha) == ["My Gold"], "alpha keeps its own material (%s)"
          % slots(alpha))
    check(slots(gamma) == ["SW brushed steel", "SW polished gold"],
          "gamma keeps the CAD appearances (%s)" % slots(gamma))
    check(slots(beta) == ["DB Gold"], "beta takes the database (%s)"
          % slots(beta))
    pinned = [m.name for m in bpy.data.materials if m.use_fake_user]
    check(not pinned, "no material keeps a fake user (%s)" % pinned)

    print("\n== Select Locked Parts, and Unlock")
    select(beta)
    result = bpy.ops.stepper.material_lock_select()
    check(sorted(o.name for o in bpy.context.selected_objects)
          == sorted([alpha.name, gamma.name]),
          "the locked parts are selected (%s)"
          % [o.name for o in bpy.context.selected_objects])
    select(alpha)
    bpy.ops.stepper.material_lock(lock=False)
    check(not material_lock.is_locked(alpha), "alpha is unlocked")
    bpy.ops.stepper.mat_db_apply()
    check(slots(alpha) == ["DB Chrome"],
          "unlocked, alpha takes the database (%s)" % slots(alpha))


def instances(tmp, db_dir):
    print("\n== a collection instance")
    enable(db_dir)
    first, _finer, changed = sends(tmp)
    m = manifest(PARTS)
    native_import.build(bpy.context, first, manifest=m,
                        hierarchy="COLLECTION_INSTANCES")
    alpha, beta = by_path("alpha-1"), by_path("beta-1")
    check(alpha.type == "EMPTY" and alpha.instance_collection is not None,
          "alpha is a collection instance")
    mine = bpy.data.materials.new("My Gold")
    material_lock.holders(alpha)[0].data.materials[0] = mine
    select(alpha)
    bpy.ops.stepper.material_lock(lock=True)
    check(material_lock.is_locked(alpha), "the instance is locked")
    before = alpha.instance_collection
    native_import.update(bpy.context, changed, manifest=m,
                         hierarchy="COLLECTION_INSTANCES")
    alpha, beta = by_path("alpha-1"), by_path("beta-1")
    check(alpha.instance_collection is not before,
          "the instance shows the new prototype")
    check(slots(alpha) == ["My Gold"],
          "the new prototype has the locked material (%s)" % slots(alpha))
    check(slots(beta) == ["SW polished gold"],
          "beta has the CAD appearance (%s)" % slots(beta))
    bpy.context.preferences.addons["CADder"].preferences.active_matdb = "shop"
    select(alpha, beta)
    bpy.context.scene.stepper.mat_db_apply_selection_only = True
    bpy.ops.stepper.mat_db_apply()
    check(slots(alpha) == ["My Gold"] and slots(beta) == ["DB Gold"],
          "Apply to the selection reaches the prototypes and leaves the "
          "locked one (%s, %s)" % (slots(alpha), slots(beta)))


def step_file(tmp, db_dir):
    step = os.path.join(tmp, "holes.step")
    shutil.copyfile(os.path.join(_HERE, "fixtures", "holes.step"), step)
    from CADder import refresh as R

    def load(database="NONE"):
        prefs = enable(db_dir)
        prefs.active_matdb = database
        main._cache_drop(step)
        main.load_step(bpy.context, step, htypes="FLAT", up_as="Z",
                       material_database=database)
        return [o for o in R.file_objects(step)
                if o.type == "MESH" and "STEP_tag" in o][0]

    plate = load()
    original = json.loads(plate["STEP_materials"])[0]

    for tool, call in (("Regenerate", lambda: bpy.ops.stepper.regenerate()),
                       ("Rebuild Selected",
                        lambda: bpy.ops.object.occ_rebuild_selected())):
        print("\n== " + tool)
        for locked in (False, True):
            plate = load()
            mine = bpy.data.materials.new("Mine")
            plate.data.materials[0] = mine
            material_lock.set_locked(plate, locked)
            select(plate)
            result = call()
            check(result == {"FINISHED"}, "%s finished (%s)" % (tool, result))
            if locked:
                check(slots(plate)[0] == "Mine",
                      "a locked part keeps its material (%s)" % slots(plate))
            else:
                check(slots(plate)[0] != "Mine",
                      "an unlocked part takes the file's (%s)" % slots(plate))

    # A refresh imports again with the database of the first import. A
    # part that still has the import's material takes an entry that was
    # added to that database since. A locked one does not.
    print("\n== a refresh of the STEP file")
    for locked in (False, True):
        write_db(db_dir, "plates", {"SW other": "Other"})
        plate = load("plates")
        check(slots(plate)[0] == original,
              "the database had no entry for the plate at import (%s)"
              % slots(plate))
        material_lock.set_locked(plate, locked)
        write_db(db_dir, "plates", {original: "DB Plate"})
        result = bpy.ops.stepper.refresh_file(filepath=step)
        check(result == {"FINISHED"}, "the refresh finished (%s)" % result)
        if locked:
            check(slots(plate)[0] == original,
                  "a locked part keeps the import's material (%s)"
                  % slots(plate))
        else:
            check(slots(plate)[0] == "DB Plate",
                  "an unlocked part takes the database (%s)" % slots(plate))


def main_():
    tmp = tempfile.mkdtemp(prefix="cadder_material_lock_")
    db_dir = os.path.join(tmp, "MaterialDB")
    os.makedirs(db_dir)
    direct_link(tmp, db_dir)
    instances(tmp, db_dir)
    step_file(tmp, db_dir)
    if FAILS:
        raise SystemExit("material_lock_smoke: FAIL: %d check(s): %s"
                         % (len(FAILS), "; ".join(FAILS)))
    print("\nmaterial_lock_smoke: OK: a locked part keeps its materials "
          "through Apply, refine, refresh, a replacing send, a new "
          "prototype, Regenerate, Rebuild Selected and a STEP refresh")


main_()
