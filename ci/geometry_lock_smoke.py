# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Lock Geometry.

    blender -b --factory-startup --python-exit-code 1 -P ci/geometry_lock_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

A locked part keeps the mesh it has, with the changes made to it in
Blender, through everything that rebuilds a part: a refine from the CAD
application, a refresh (also of a collection instance), a send that
replaces every object (also of a collection instance), Rebuild from CAD
with Geometry and with Geometry and Poses, the passes that run after a
send, Regenerate, Rebuild Selected, Apply UVs, Apply Defeature and a
refresh of a STEP file. Each unlocked part beside it takes the change, so
the test shows that the change did happen. A locked part still moves to a
new pose, and after the lock comes off the next refresh rebuilds it.

Then the button: it locks and unlocks the parts in scope, the panel reads
its state from a limited number of objects, and a large scene costs a
redraw no full scan.
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import time
from types import SimpleNamespace

import bmesh
import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from CADder import geometry_lock, main, material_lock  # noqa: E402
from CADder.ci import rebuild_fixture as fx  # noqa: E402
from CADder.rig import manifest as man_mod, native_import, swmesh  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


# ── the direct link ─────────────────────────────────────────────────────
#
# Two placements of alpha share one definition, so they share one mesh.
# beta is a part of its own. The third export moves alpha-1, changes the
# geometry of both parts and adds a third placement of alpha.

ALPHA1 = fx.Part("c001", "alpha-1", "palpha1", fx.translate(0.0), 0)
ALPHA2 = fx.Part("c002", "alpha-2", "palpha2", fx.translate(0.1), 1)
BETA = fx.Part("c003", "beta-1", "pbeta", fx.translate(0.2), 2)
MOVED = fx.Part("c001", "alpha-1", "palpha1", fx.translate(0.05), 0)
ALPHA3 = fx.Part("c004", "alpha-3", "palpha3", fx.translate(0.3), 3)
NAMES = ("alpha1", "alpha2", "beta", "alpha3")
# The largest X of a part of the first export is 0.03, and of the changed
# export 0.06.
BIG = 0.045
DEFINITION = {"alpha": 1, "beta": 2}


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def points(triangles, size):
    """The points of a fan of `triangles` triangles, `size` times as
    large: another size is another geometry."""
    out = []
    for i in range(triangles + 2):
        out.append((i * 0.01 * size, (i * i % 3) * 0.01 * size, 0.0))
    return out


def write_mesh(path, shapes, parts):
    """shapes: definition name -> (triangles, size). parts: fx.Part."""
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, len(shapes), len(parts), 0)
    body += _text("polished gold") + struct.pack(
        "<6f", 0.97, 0.88, 0.6, 1.0, 0.4, 1.0)
    body += _text("") + struct.pack("<I", 0)
    for name, (triangles, size) in shapes.items():
        verts = [v for p in points(triangles, size) for v in p]
        tris = []
        for i in range(triangles):
            tris.extend([0, i + 1, i + 2])
        body += struct.pack("<i", DEFINITION[name]) + _text(name)
        body += struct.pack("<II", triangles + 2, triangles)
        body += struct.pack("<%df" % len(verts), *verts)
        body += struct.pack("<%di" % len(tris), *tris)
        body += struct.pack("<%di" % triangles, *([0] * triangles))
    for p in parts:
        did = DEFINITION["beta" if p.path.startswith("beta") else "alpha"]
        body += (struct.pack("<i", did) + _text(p.cid) + _text(p.name)
                 + _text(p.path) + struct.pack("<16d", *fx.flat(p.rows))
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def export(tmp, folder, shapes, parts):
    """(mesh path, manifest path, manifest). Every export has the same
    file name in a folder of its own, as the CAD add-in writes the sends
    of one document: the import is known by the name."""
    where = os.path.join(tmp, folder)
    os.makedirs(where, exist_ok=True)
    data = fx.manifest(parts, NAMES, [], name="lock")
    man = os.path.join(where, "lock.rig.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    mesh = write_mesh(os.path.join(where, "lock.swmesh"), shapes, parts)
    return mesh, man, man_mod.parse(data)


def exports(tmp):
    first = export(tmp, "first", {"alpha": (2, 1.0), "beta": (2, 1.0)},
                   [ALPHA1, ALPHA2, BETA])
    finer = export(tmp, "finer", {"alpha": (4, 1.0), "beta": (4, 1.0)},
                   [ALPHA1, ALPHA2, BETA])
    changed = export(tmp, "changed", {"alpha": (2, 2.0), "beta": (2, 2.0)},
                     [MOVED, ALPHA2, BETA, ALPHA3])
    return first, finer, changed


def by_path(path):
    for obj in bpy.data.objects:
        if obj.get("SWMESH_path") == path and obj.get("SWMESH_file") \
                and not obj.get("SWMESH_prototype"):
            return obj
    return None


def parts():
    return [by_path(p) for p in ("alpha-1", "alpha-2", "beta-1")]


def mesh_of(obj):
    return geometry_lock.holders(obj)[0].data


def coords(mesh):
    return [tuple(round(c, 6) for c in v.co) for v in mesh.vertices]


# An amount no fixture has as a size, so a point moved by it is the change
# and not a corner of the part.
NUDGE = 0.0123


def edit(obj):
    """A change made in Blender: the first point of the mesh goes up."""
    mesh = mesh_of(obj)
    mesh.vertices[0].co.z += NUDGE
    mesh.update()
    return coords(mesh)


def orphans():
    return [m.name for m in bpy.data.meshes if m.users == 0]


def pinned():
    return [m.name for m in bpy.data.meshes if m.use_fake_user]


def select(*objects):
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


def enable():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")


def refine(tmp):
    print("\n== a finer mesh from the CAD application")
    enable()
    first, finer, _changed = exports(tmp)
    native_import.build(bpy.context, first[0], manifest=first[2])
    alpha1, alpha2, beta = parts()
    check(mesh_of(alpha1) == mesh_of(alpha2),
          "the two placements of alpha share one mesh")
    mesh = mesh_of(alpha1)
    mine = edit(alpha1)
    geometry_lock.set_locked(alpha1, True)
    replaced = native_import.refine(bpy.context, finer[0])
    check(mesh_of(alpha1) == mesh and coords(mesh) == mine,
          "the locked part keeps its mesh and the change made to it")
    check(mesh_of(alpha2) == mesh,
          "the placement that shares the locked mesh keeps it too")
    check(len(mesh_of(beta).vertices) == 6,
          "the unlocked part has the finer mesh (%d points)"
          % len(mesh_of(beta).vertices))
    check(sorted(o.name for o in replaced) == [beta.name],
          "refine says it changed only beta (%s)"
          % sorted(o.name for o in replaced))
    check(not orphans(), "no mesh is left with no users (%s)" % orphans())


def update(tmp, hierarchy):
    print("\n== a refresh, %s" % hierarchy)
    enable()
    first, _finer, changed = exports(tmp)
    native_import.build(bpy.context, first[0], manifest=first[2],
                        hierarchy=hierarchy)
    alpha1, alpha2, beta = parts()
    mesh, beta_mesh = mesh_of(alpha1), mesh_of(beta)
    held_col = alpha1.instance_collection
    beta_col = beta.instance_collection
    mine = edit(alpha1)
    tag = alpha1["SWMESH_geometry"]
    geometry_lock.set_locked(alpha1, True)

    _objects, _report, out = native_import.update(
        bpy.context, changed[0], manifest=changed[2], hierarchy=hierarchy)
    check(by_path("alpha-1") == alpha1, "the locked part is the same object")
    check(mesh_of(alpha1) == mesh and coords(mesh) == mine,
          "the locked part keeps its mesh and the change made to it")
    check(alpha1.instance_collection == held_col,
          "a locked instance keeps the collection it instances")
    check(alpha1["SWMESH_geometry"] == tag,
          "the geometry tag still says what the mesh is")
    check(abs(alpha1.matrix_world.translation.x - 0.05) < 1e-6,
          "the locked part moved to its new pose (x=%.4f)"
          % alpha1.matrix_world.translation.x)
    check(geometry_lock.is_locked(alpha1), "the part is still locked")
    check(sorted(out.locked) == sorted([alpha1.name, alpha2.name]),
          "the report names the parts that kept their geometry (%s)"
          % out.locked)
    check("kept their locked geometry" in out.describe(),
          "the summary says so (%s)" % out.describe())
    check(mesh_of(beta) != beta_mesh and beta.name in out.reshaped,
          "the unlocked part has the new geometry")
    check(max(v[0] for v in coords(mesh_of(beta))) > BIG,
          "at the new size")
    if hierarchy == "COLLECTION_INSTANCES":
        check(beta.instance_collection != beta_col,
              "the unlocked instance shows the new prototype")
    alpha3 = by_path("alpha-3")
    check(alpha3 is not None and mesh_of(alpha3) != mesh,
          "a new placement of alpha gets the new geometry")

    print("   -- unlocked, the next refresh rebuilds it")
    geometry_lock.set_locked(alpha1, False)
    _objects, _report, out = native_import.update(
        bpy.context, changed[0], manifest=changed[2], hierarchy=hierarchy)
    check(alpha1.name in out.reshaped and not out.locked,
          "the refresh rebuilt the part (%s, %s)"
          % (out.reshaped, out.locked))
    check(max(v[0] for v in coords(mesh_of(alpha1))) > BIG
          and coords(mesh_of(alpha1))[0][2] == 0.0,
          "the part has the new geometry and not the change")
    check(alpha1["SWMESH_geometry"] != tag, "the tag says so too")
    check(not orphans(), "no mesh is left with no users (%s)" % orphans())


def flat_names(tmp):
    print("\n== a send that replaces every object: the names")
    enable()
    first, _finer, _changed = exports(tmp)
    native_import.build(bpy.context, first[0], manifest=first[2])
    alpha1 = by_path("alpha-1")
    mesh = mesh_of(alpha1)
    geometry_lock.set_locked(alpha1, True)
    native_import.build(bpy.context, first[0], manifest=first[2])
    alpha1 = by_path("alpha-1")
    group = [c.name for c in alpha1.users_collection
             if c.get("SWMESH_role") == "group"]
    check(mesh_of(alpha1) == mesh and group == [mesh.name],
          "the flat collection has the name of the kept mesh (%s, %s)"
          % (group, mesh.name))
    check(not orphans(), "no mesh is left with no users (%s)" % orphans())


def replace(tmp, hierarchy):
    print("\n== a send that replaces every object, %s" % hierarchy)
    enable()
    first, _finer, changed = exports(tmp)
    native_import.build(bpy.context, first[0], manifest=first[2],
                        hierarchy=hierarchy)
    alpha1, alpha2, beta = parts()
    mesh = mesh_of(alpha1)
    name = mesh.name
    mine = edit(alpha1)
    tag = alpha1["SWMESH_geometry"]
    geometry_lock.set_locked(alpha1, True)
    # Its materials too: the old mesh carries them, and the material lock
    # puts them back on the mesh the part has after the send.
    gold = bpy.data.materials.new("My Gold")
    mesh.materials[0] = gold
    material_lock.set_locked(alpha1, True)
    old = alpha1

    native_import.build(bpy.context, changed[0], manifest=changed[2],
                        hierarchy=hierarchy)
    alpha1, alpha2, beta = parts()
    try:
        old.name
        check(False, "the send kept the old object")
    except ReferenceError:
        check(True, "the send made new objects")
    check(geometry_lock.is_locked(alpha1)
          and not geometry_lock.is_locked(alpha2)
          and not geometry_lock.is_locked(beta),
          "the new alpha-1 is locked, the others are not")
    check(mesh_of(alpha1) == mesh and coords(mesh) == mine,
          "the new part has the old mesh and the change made to it")
    check(mesh.name == name, "the mesh keeps its name (%s)" % mesh.name)
    check(mesh_of(alpha2) == mesh,
          "the placement that shared the locked mesh has it too")
    check(alpha1["SWMESH_geometry"] == tag
          and alpha2["SWMESH_geometry"] == tag,
          "their geometry tags say what the mesh is")
    check([m.name if m else None for m in mesh.materials] == ["My Gold"]
          and material_lock.is_locked(alpha1),
          "the locked materials are on it")
    check(abs(alpha1.matrix_world.translation.x - 0.05) < 1e-6,
          "the part stands at its new pose")
    check(max(v[0] for v in coords(mesh_of(beta))) > BIG,
          "the unlocked part has the new geometry")
    alpha3 = by_path("alpha-3")
    if hierarchy == "COLLECTION_INSTANCES":
        check(mesh_of(alpha3) == mesh and alpha3["SWMESH_geometry"] == tag,
              "a new placement that shares the prototype draws the kept "
              "mesh, and its tag says so")
    else:
        check(mesh_of(alpha3) != mesh,
              "a new placement has the new geometry of its own")
    check(not pinned(), "no mesh keeps a fake user (%s)" % pinned())
    check(not orphans(), "no mesh is left with no users (%s)" % orphans())

    print("   -- the next refresh still keeps it, and after the unlock "
          "rebuilds it")
    _objects, _report, out = native_import.update(
        bpy.context, changed[0], manifest=changed[2], hierarchy=hierarchy)
    check(alpha1.name in out.locked and mesh_of(alpha1) == mesh,
          "the refresh keeps the locked part (%s)" % out.locked)
    geometry_lock.set_locked(alpha1, False)
    _objects, _report, out = native_import.update(
        bpy.context, changed[0], manifest=changed[2], hierarchy=hierarchy)
    check(alpha1.name in out.reshaped
          and max(v[0] for v in coords(mesh_of(alpha1))) > BIG,
          "unlocked, the refresh rebuilds it (%s)" % out.reshaped)


# ── Rebuild from CAD, through the send the CAD add-in makes ─────────────

class Cad:
    """The CAD application's answers. Geometry comes back for every part
    of the assembly, whatever the request names, so the refine has to
    keep the lock as well."""

    def __init__(self, tmp, poses):
        self.tmp = tmp
        self.poses_of = poses
        self.count = 0

    def retessellate(self, fields):
        self.count += 1
        path = os.path.join(self.tmp, "reply%d.swmesh" % self.count)
        write_mesh(path, {"alpha": (4, 1.0), "beta": (4, 1.0)},
                   [ALPHA1, ALPHA2, BETA])
        return {"ok": True, "mesh": path, "triangles": 16,
                "tolerance_m": 0.0005}

    def poses(self, fields):
        return fx.poses_reply(self.poses_of)


def asked(cad, op):
    return [dict(f) for f in cad.seen if f["op"] == op]


def rebuild_from_cad(tmp):
    print("\n== Rebuild from CAD")
    enable()
    scene = bpy.context.scene
    scene.stepper.tris_to_quads = True
    first, _finer, _changed = exports(tmp)
    result = fx.send(first[0], first[1], "FLAT", "ZPOS",
                     steps={"build_rig": False, "relink": False})
    check(result.get("ok"), "the send worked (%s)" % result.get("error"))
    alpha1, alpha2, beta = parts()
    mesh = mesh_of(alpha1)
    mine = edit(alpha1)
    geometry_lock.set_locked(alpha1, True)
    fx.select_none()

    for what, where in (("GEOMETRY", None), ("GEOMETRY_POSES", 0.08)):
        print("   -- " + what)
        before = mesh_of(beta)
        moved = [fx.Part("c001", "alpha-1", "palpha1",
                         fx.translate(where or 0.0), 0), ALPHA2, BETA]
        answers = Cad(tmp, moved)
        with fx.FakeCad(retessellate=answers.retessellate,
                        poses=answers.poses) as cad:
            result = bpy.ops.cadlink.update_from_cad(what=what)
        check(result == {"FINISHED"}, "it finished (%s)" % result)
        request = asked(cad, "retessellate")
        check(len(request) == 1, "one request for geometry (%d)"
              % len(request))
        if request:
            check(request[0]["components"] == ["c003"]
                  and request[0]["persistent_ids"] == ["pbeta"]
                  and request[0]["paths"] == ["beta-1"],
                  "it names beta alone: the locked mesh is not asked for "
                  "(%s, %s, %s)" % (request[0]["components"],
                                    request[0]["persistent_ids"],
                                    request[0].get("paths")))
        check(mesh_of(alpha1) == mesh and coords(mesh) == mine
              and mesh_of(alpha2) == mesh,
              "the locked mesh and the change made to it stay")
        check(mesh_of(beta) != before and len(mesh_of(beta).vertices) == 6,
              "the unlocked part has the finer mesh")
        if where is not None:
            check(asked(cad, "poses")
                  and sorted(asked(cad, "poses")[0]["components"])
                  == ["c001", "c002", "c003"],
                  "the poses of every part are asked for")
            check(abs(alpha1.matrix_world.translation.x - where) < 1e-5,
                  "the locked part moved to its CAD pose (x=%.4f)"
                  % alpha1.matrix_world.translation.x)

    print("   -- every part in scope locked")
    for obj in (alpha2, beta):
        geometry_lock.set_locked(obj, True)
    answers = Cad(tmp, [fx.Part("c001", "alpha-1", "palpha1",
                                fx.translate(0.12), 0), ALPHA2, BETA])
    with fx.FakeCad(retessellate=answers.retessellate,
                    poses=answers.poses) as cad:
        result = bpy.ops.cadlink.update_from_cad(what="GEOMETRY")
    check(result == {"CANCELLED"} and not cad.seen,
          "Geometry does nothing and asks for nothing (%s, %s)"
          % (result, [f["op"] for f in cad.seen]))
    with fx.FakeCad(retessellate=answers.retessellate,
                    poses=answers.poses) as cad:
        result = bpy.ops.cadlink.update_from_cad(what="GEOMETRY_POSES")
    check(result == {"FINISHED"}
          and [f["op"] for f in cad.seen] == ["poses"],
          "Geometry and Poses asks for the poses only (%s, %s)"
          % (result, [f["op"] for f in cad.seen]))
    check(abs(alpha1.matrix_world.translation.x - 0.12) < 1e-5,
          "and moves the parts (x=%.4f)" % alpha1.matrix_world.translation.x)
    check(mesh_of(alpha1) == mesh and coords(mesh) == mine,
          "the locked mesh stays")

    print("   -- the passes after a send leave a locked mesh alone")
    geometry_lock.set_locked(alpha2, False)
    geometry_lock.set_locked(beta, False)
    bm = bmesh.new()
    bm.from_mesh(alpha1.data)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(alpha1.data)
    bm.free()
    faces = len(alpha1.data.polygons)
    _first, _finer, changed = exports(tmp)
    result = fx.send(changed[0], changed[1], "FLAT", "ZPOS", update=True,
                     steps={"build_rig": False, "relink": False})
    check(result.get("ok"), "the refresh worked (%s)" % result.get("error"))
    check(mesh_of(alpha1) == mesh and len(alpha1.data.polygons) == faces,
          "Tris to Quads left the locked mesh as it was (%d faces)"
          % len(alpha1.data.polygons))
    check(len(beta.data.polygons) == 1,
          "and joined the two new triangles of the unlocked one into a "
          "quad (%d faces)" % len(beta.data.polygons))
    stage = (result.get("stages") or {}).get("update") or {}
    check(alpha1.name in (stage.get("locked") or []),
          "the refresh tells the CAD add-in which parts kept their "
          "geometry (%s)" % stage.get("locked"))


# ── a STEP file ─────────────────────────────────────────────────────────

def step_file(tmp):
    step = os.path.join(tmp, "assembly.step")
    shutil.copyfile(os.path.join(_HERE, "fixtures", "assembly.step"), step)
    from CADder import refresh as R

    def load(htypes="FLAT"):
        enable()
        main._cache_drop(step)
        main.load_step(bpy.context, step, htypes=htypes, up_as="Z")
        found = {}
        for obj in R.file_objects(step):
            if geometry_lock.is_part(obj):
                found.setdefault(obj.get("STEP_name"), []).append(obj)
        return found

    def point(obj):
        return tuple(round(c, 6) for c in mesh_of(obj).vertices[0].co)

    def nudged(obj, was):
        """True when the mesh has the point that edit() moved."""
        want = (was[0], was[1], was[2] + NUDGE)
        return any(all(abs(a - b) < 1e-5 for a, b in zip(v.co, want))
                   for v in mesh_of(obj).vertices)

    def prepare():
        found = load()
        base, arm = found["base"][0], found["arm"][0]
        pads = found["pad"]
        was = {o.name: point(o) for o in (base, arm, pads[0])}
        for obj in (base, arm, pads[0]):
            edit(obj)
        geometry_lock.set_locked(base, True)
        geometry_lock.set_locked(pads[0], True)
        return base, arm, pads, was

    def tool(title, call, shared=True):
        print("\n== " + title)
        base, arm, pads, was = prepare()
        meshes = {o.name: o.data for o in (base, arm, pads[1])}
        select(base, arm, pads[1])
        result = call()
        check(result == {"FINISHED"}, "%s finished (%s)" % (title, result))
        check(nudged(base, was[base.name]) and base.data == meshes[base.name],
              "the locked part keeps its mesh and the change made to it")
        check(not nudged(arm, was[arm.name]),
              "the unlocked part is rebuilt: the change is gone")
        if shared:
            check(nudged(pads[1], was[pads[0].name]),
                  "the part that shares the locked mesh keeps it too")
        check(geometry_lock.is_locked(base), "the part is still locked")
        return base, arm, pads

    tool("Rebuild from STEP (Regenerate)",
         lambda: bpy.ops.stepper.regenerate())
    _base, _arm, pads = tool("Rebuild Selected",
                             lambda: bpy.ops.object.occ_rebuild_selected())
    select(pads[1])
    result = bpy.ops.object.occ_rebuild_selected()
    check(result == {"CANCELLED"},
          "Rebuild Selected of a locked mesh alone does nothing (%s)"
          % result)
    base, arm, pads, _was = prepare()
    select(pads[1])
    result = bpy.ops.stepper.regenerate()
    check(result == {"CANCELLED"},
          "Regenerate of a locked mesh alone does nothing (%s)" % result)
    tool("Apply UVs", lambda: bpy.ops.stepper.reapply_uv())
    tool("Apply Defeature", lambda: bpy.ops.stepper.apply_defeature())

    print("\n== Refresh from Disk")
    base, arm, pads, was = prepare()
    materials = base["STEP_materials"]
    record = base["STEP_import_settings"]
    meshes = {o.name: o.data for o in (base, arm, pads[1])}
    select(base)
    result = bpy.ops.stepper.refresh_file(filepath=step)
    check(result == {"FINISHED"}, "the refresh finished (%s)" % result)
    check(R.file_objects(step) and base.name in bpy.data.objects,
          "the objects stay")
    check(base.data == meshes[base.name] and nudged(base, was[base.name]),
          "the locked part keeps its mesh and the change made to it")
    check(pads[1].data == meshes[pads[1].name]
          and nudged(pads[1], was[pads[0].name]),
          "the part that shares the locked mesh keeps it too")
    check(arm.data != meshes[arm.name] and not nudged(arm, was[arm.name]),
          "the unlocked part has the fresh mesh")
    check(base["STEP_materials"] == materials
          and base["STEP_import_settings"] == record,
          "the stamps that describe the kept mesh stay")
    check(base.get("STEP_import_basis") is not None,
          "the other stamps are the new import's")
    check(geometry_lock.is_locked(base), "the part is still locked")
    check(not orphans(), "no mesh is left with no users (%s)" % orphans())

    print("\n== Refresh from Disk, collection instances")
    found = load("COLLECTION_INSTANCES")
    empties = [o for o in found.get("pad", []) if o.type == "EMPTY"]
    check(len(empties) == 2, "two instances of pad (%d)" % len(empties))
    others = [o for o in found.get("base", []) if o.type == "EMPTY"]
    if len(empties) == 2 and others:
        pad_mesh, base_mesh = mesh_of(empties[0]), mesh_of(others[0])
        pad_was = point(empties[0])
        base_was = point(others[0])
        edit(empties[0])
        edit(others[0])
        geometry_lock.set_locked(empties[0], True)
        result = bpy.ops.stepper.refresh_file(filepath=step)
        check(result == {"FINISHED"}, "the refresh finished (%s)" % result)
        check(mesh_of(empties[0]) == pad_mesh
              and nudged(empties[0], pad_was)
              and mesh_of(empties[1]) == pad_mesh,
              "the prototype of the locked instance keeps its mesh")
        check(mesh_of(others[0]) != base_mesh
              and not nudged(others[0], base_was),
              "the prototype of an unlocked instance has the fresh mesh")
        check(not orphans(), "no mesh is left with no users (%s)"
              % orphans())


# ── the button ──────────────────────────────────────────────────────────

def part(name, collection):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new(name, me)
    obj["SWMESH_file"] = "rig"
    obj["SWMESH_path"] = name
    collection.objects.link(obj)
    return obj


def activate(collection):
    def find(layer):
        if layer.collection == collection:
            return layer
        for child in layer.children:
            found = find(child)
            if found is not None:
                return found
        return None
    bpy.context.view_layer.active_layer_collection = find(
        bpy.context.view_layer.layer_collection)


def button():
    print("\n== the button")
    enable()
    scene = bpy.context.scene
    check(not bpy.ops.stepper.geometry_lock.poll(),
          "with no part in scope the button is off")
    group = bpy.data.collections.new("group")
    scene.collection.children.link(group)
    a, b = part("a", group), part("b", group)
    c = part("c", scene.collection)
    camera = bpy.data.objects.new("camera", bpy.data.cameras.new("camera"))
    scene.collection.objects.link(camera)
    check(not geometry_lock.is_part(camera), "a camera is not a part")

    select(a, b)
    check(main._lock_state(bpy.context) == (True, False),
          "two unlocked parts: the button shows Unlocked")
    props = SimpleNamespace(is_property_set=lambda name: False, lock=True)
    text = main.STEP_OT_GeometryLock.description(bpy.context, props)
    check(text.startswith("Keep the geometry"),
          "the tooltip says what a click does (%s)" % text)
    result = bpy.ops.stepper.geometry_lock()
    check(result == {"FINISHED"} and geometry_lock.is_locked(a)
          and geometry_lock.is_locked(b) and not geometry_lock.is_locked(c),
          "a click locks the selected parts and no other")
    check(main._lock_state(bpy.context) == (True, True),
          "the button shows Locked")
    text = main.STEP_OT_GeometryLock.description(bpy.context, props)
    check(text.startswith("Let Rebuild"), "the tooltip says Unlock (%s)"
          % text)
    check(main._geometry_locked(scene) == 2,
          "the scene count sees two (%d)" % main._geometry_locked(scene))
    bpy.ops.stepper.geometry_lock()
    check(not geometry_lock.is_locked(a) and not geometry_lock.is_locked(b),
          "a second click unlocks them")
    geometry_lock.set_locked(a, True)
    check(main._lock_state(bpy.context) == (True, False),
          "one of two locked: the button shows Unlocked")
    bpy.ops.stepper.geometry_lock()
    check(geometry_lock.is_locked(a) and geometry_lock.is_locked(b),
          "and a click locks both")
    bpy.ops.stepper.geometry_lock(lock=False)
    check(not geometry_lock.is_locked(a) and not geometry_lock.is_locked(b),
          "Lock off in the redo panel unlocks")

    print("   -- no part selected: the active collection")
    select(camera)
    activate(group)
    check(main._lock_state(bpy.context) == (True, False),
          "a camera alone is not a scope: the collection is")
    bpy.ops.stepper.geometry_lock()
    check(geometry_lock.is_locked(a) and geometry_lock.is_locked(b)
          and not geometry_lock.is_locked(c),
          "the parts of the active collection are locked, and no other")

    print("   -- Select Parts with Locked Geometry")
    geometry_lock.set_locked(c, True)
    main._usage_stale()
    select(camera)
    result = bpy.ops.stepper.geometry_lock_select()
    check(result == {"FINISHED"}
          and sorted(o.name for o in bpy.context.selected_objects)
          == ["a", "b", "c"],
          "the locked parts are selected (%s)"
          % sorted(o.name for o in bpy.context.selected_objects))
    for obj in (a, b, c):
        geometry_lock.set_locked(obj, False)
    main._usage_stale()
    check(not bpy.ops.stepper.geometry_lock_select.poll(),
          "with no locked part the arrow is off")

    print("\n== a large scene")
    many = bpy.data.collections.new("many")
    scene.collection.children.link(many)
    mesh = a.data
    made = []
    for i in range(10000):
        obj = bpy.data.objects.new("p%05d" % i, mesh)
        obj["SWMESH_file"] = "big"
        many.objects.link(obj)
        made.append(obj)
    select()
    activate(many)

    def timed(fn, times=20):
        start = time.perf_counter()
        for _ in range(times):
            fn()
        return (time.perf_counter() - start) / times

    free = timed(lambda: main._lock_state(bpy.context))
    poll = timed(lambda: bpy.ops.stepper.geometry_lock.poll())
    for obj in made:
        geometry_lock.set_locked(obj, True)
    main._usage_stale()
    locked = timed(lambda: main._lock_state(bpy.context))
    state = main._lock_state(bpy.context)
    first = timed(lambda: (main._usage_stale(),
                           main._geometry_locked(scene)), 1)
    again = timed(lambda: main._geometry_locked(scene), 100)
    print("   10000 parts: state %.2f ms unlocked, %.2f ms locked, poll "
          "%.2f ms, count %.1f ms to scan and %.4f ms kept"
          % (free * 1000, locked * 1000, poll * 1000, first * 1000,
             again * 1000))
    check(state == (True, True), "every part locked: the button shows it")
    check(free < 0.005 and locked < 0.02 and poll < 0.02,
          "a redraw looks at a limited number of objects")
    check(again < 0.001, "the arrow's poll does not scan on every redraw")
    start = time.perf_counter()
    bpy.ops.stepper.geometry_lock()
    spent = time.perf_counter() - start
    check(not any(geometry_lock.is_locked(o) for o in made),
          "a click unlocks all 10000, decided from the whole scope")
    check(spent < 2.0, "in %.2f s" % spent)


def main_():
    tmp = tempfile.mkdtemp(prefix="cadder_geometry_lock_")
    refine(tmp)
    for hierarchy in ("FLAT", "COLLECTION_INSTANCES"):
        update(tmp, hierarchy)
        replace(tmp, hierarchy)
    flat_names(tmp)
    rebuild_from_cad(tmp)
    step_file(tmp)
    button()
    if FAILS:
        raise SystemExit("geometry_lock_smoke: FAIL: %d check(s): %s"
                         % (len(FAILS), "; ".join(FAILS)))
    print("\ngeometry_lock_smoke: OK: a locked part keeps its mesh through "
          "a refine, a refresh, a replacing send, Rebuild from CAD, "
          "Regenerate, Rebuild Selected, Apply UVs, Apply Defeature and a "
          "STEP refresh, and still moves to a new pose")


main_()
