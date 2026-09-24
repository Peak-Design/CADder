# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for linked parts and copies of an import (CADder 1.2).

    blender -b --factory-startup --python-exit-code 1 -P ci/native_link_smoke.py

A user builds scenes from several assemblies that hold the same parts, and
wants the same part to be one mesh everywhere, and a send that adds an
assembly again as a new copy (Oscar, 2026-09-24). This checks:

  * a part of another assembly that is the same shape with the same
    appearance uses the mesh already in the scene, in all four hierarchy
    modes, and a part with another appearance gets a mesh of its own,
  * nothing is linked when the send asks for no links, or when the other
    mesh was made another way (triangles into quads), or when its geometry
    is locked,
  * a replace of one assembly leaves the mesh the other one uses,
  * Append as a New Copy puts "<name>.001", then ".002", beside the import,
    each with its own collections and rig, linked to the same meshes, in
    all four hierarchy modes,
  * a send without the option replaces the import and not its copies, and
    a lock on the rig of the import does not stop the build of a copy,
  * Refresh Model brings the import and every copy up to date, and a copy
    the user moved stays where it was put,
  * a Refresh with the import deleted builds the import again and does not
    take a copy for it,
  * the Rig list works on the rig of a copy, and Rebuild from CAD asks for
    a copy and builds it again as a copy,
  * Defeature asks only for parts of the documents in scope.

Every send runs through bridge._run_job, the path SolidWorks drives.
"""

import math
import os
import struct
import sys
import tempfile

import bpy
from mathutils import Matrix

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig, tools  # noqa: E402
from CADder.ci import native_top_level_smoke as tl  # noqa: E402
from CADder.rig import cad_link, native_import, rig_build, swmesh  # noqa: E402
from CADder.rig import ui as rig_ui  # noqa: E402

MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")
TMP = os.path.join(tempfile.gettempdir(), "native_link_smoke")
FOLDER = os.path.join(tempfile.gettempdir(), "smoke parts")
WRENCH = os.path.join(FOLDER, "wrench.SLDASM")
GIZMO = os.path.join(FOLDER, "gizmo.SLDASM")

GRAY = ("gray", (0.8, 0.8, 0.8, 1.0))
RED = ("red", (0.8, 0.1, 0.1, 1.0))

# (component id, path, persistent id, x, mesh name, size, material)
WRENCH_PARTS = [("c001", "base-1", "pbase", 0.0, "base", 0.05, 0),
                ("c002", "arm-1", "parm", 0.2, "arm", 0.10, 0),
                ("c003", "pin-1", "ppin", 0.4, "pin", 0.15, 0)]
# The same pin, the same arm in red, a lever of its own, and a dowel: a
# part of another name with the shape and the appearance of the gray arm.
GIZMO_PARTS = [("c001", "frame-1", "pframe", 0.0, "frame", 0.07, 0),
               ("c002", "arm-1", "parm2", 0.3, "arm", 0.10, 1),
               ("c003", "pin-1", "ppin2", 0.5, "pin", 0.15, 0),
               ("c004", "lever-1", "plever", 0.7, "lever", 0.12, 0),
               ("c006", "dowel-1", "pdowel", 1.1, "dowel", 0.10, 0)]
WRENCH_JOINTS = [("j001", "c001", "c002", 0.2), ("j002", "c002", "c003", 0.4)]
GIZMO_JOINTS = [("j001", "c001", "c002", 0.3), ("j002", "c002", "c003", 0.5),
                ("j003", "c001", "c004", 0.7), ("j004", "c001", "c006", 1.1)]


def fail(where, msg):
    raise SystemExit("native_link_smoke: FAIL: %s: %s" % (where, msg))


def check(cond, where, msg):
    if not cond:
        fail(where, msg)


def write_mesh(path, parts):
    """One triangle per part, of the part's size, in the part's material."""
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 2, len(parts), len(parts), 0)
    for name, rgba in (GRAY, RED):
        body += tl._text(name) + struct.pack("<6f", *rgba, 0.5, 0.0)
        body += tl._text("") + struct.pack("<I", 0)
    for i, (_cid, _where, _pid, _x, mesh, size, material) in enumerate(parts):
        body += struct.pack("<i", i + 1) + tl._text(mesh)
        body += struct.pack("<II", 3, 1)
        body += struct.pack("<9f", 0, 0, 0, size, 0, 0, 0, size, 0)
        body += struct.pack("<3i", 0, 1, 2)
        body += struct.pack("<i", material)
    for i, (cid, where, _pid, x, *_rest) in enumerate(parts):
        rows = [v for row in tl._t(x) for v in row]
        body += (struct.pack("<i", i + 1) + tl._text(cid)
                 + tl._text(where) + tl._text(where)
                 + struct.pack("<16d", *rows) + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def export(stem, parts, joints):
    os.makedirs(TMP, exist_ok=True)
    manifest = tl.write_manifest(os.path.join(TMP, stem + ".rig.json"), stem,
                                 [p[:5] for p in parts], joints)
    mesh = write_mesh(os.path.join(TMP, stem + ".swmesh"), parts)
    return mesh, manifest


def payload(which, mode="FLAT", update=False, rig_mode=None, append=False,
            link=True, quads=None, parts=None):
    stem, own, joints, document = {
        "wrench": ("wrench_Default", WRENCH_PARTS, WRENCH_JOINTS, WRENCH),
        "gizmo": ("gizmo_Default", GIZMO_PARTS, GIZMO_JOINTS, GIZMO),
    }[which]
    parts = parts or own
    mesh, manifest = export(stem, parts, joints)
    out = {
        "step": None, "mesh": mesh, "manifest": manifest,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": mode, "up_as": "ZPOS"},
        "source_document": document, "configuration": "Default",
    }
    if quads is not None:
        out["import_options"]["tris_to_quads"] = quads
    if rig_mode:
        out["rig_mode"] = rig_mode
    if append:
        out["append"] = True
    if not link:
        out["link_parts"] = False
    return out


def send(which, where, **kw):
    result = bridge._run_job(payload(which, **kw))
    check(result.get("ok"), where, "the send of %s failed: %s"
          % (which, result.get("error")))
    return result


def full_addon():
    try:
        rig.unregister()
    except Exception:                                   # noqa: BLE001
        pass
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    rig_ui._reset_state()


def parts_of(stem):
    return {o.get("SWMESH_path"): o for o in bpy.data.objects
            if o.get("SWMESH_file") == stem and o.get("RIG_component_id")
            and not o.get("SWMESH_prototype")}


def mesh_of(obj):
    if obj.type == "MESH":
        return obj.data
    col = obj.instance_collection
    return col.objects[0].data if col is not None and col.objects else None


def rig_of(stem):
    found = [o for o in bpy.data.objects if o.type == "ARMATURE"
             and o.get("RIG_rig") and o.get("RIG_import") == stem]
    return found[0] if len(found) == 1 else None


def rigs():
    return sorted(o.name for o in bpy.data.objects
                  if o.type == "ARMATURE" and o.get("RIG_rig"))


def pointers(stem):
    return {p: o.as_pointer() for p, o in parts_of(stem).items()}


def on_own_rig(where, stem):
    arm = rig_of(stem)
    check(arm is not None, where, "%s has no rig of its own: %s" % (stem, rigs()))
    for obj in parts_of(stem).values():
        check(tl.rides(obj, arm), where, "%s of %s is off its rig" % (obj.name, stem))


def top_holds(where, stem):
    top = tl.top_of(stem)
    check(top is not None, where, "no top collection %s: %s"
          % (stem, [c.name for c in tl.tops()]))
    check(top.name == stem and tl.names(top) == [stem + "_Parts", stem + "_Rig"],
          where, "%s is %s and holds %s" % (stem, top.name, tl.names(top)))


# ── Links between assemblies ───────────────────────────────────────────


def across_assemblies(mode):
    where = "%s two assemblies" % mode
    tl.fresh()
    send("wrench", where, mode=mode)
    send("gizmo", where, mode=mode)
    w, g = parts_of("wrench_Default"), parts_of("gizmo_Default")
    check(mesh_of(w["pin-1"]) is mesh_of(g["pin-1"]), where,
          "the same pin in two assemblies is two meshes")
    check(mesh_of(w["arm-1"]) is not mesh_of(g["arm-1"]), where,
          "the red arm shares the mesh of the gray one")
    check(mesh_of(w["arm-1"]) is mesh_of(g["dowel-1"]), where,
          "a part of another name with the same shape and appearance is "
          "two meshes")
    for stem in ("wrench_Default", "gizmo_Default"):
        top_holds(where, stem)
        on_own_rig(where, stem)
    if mode == "FLAT":
        # The flat collection of a linked part is named after the part of
        # this import, not after the mesh of the other one, and a Refresh
        # that adds a second pin finds it again under the number Blender
        # gave its name.
        dowel = g["dowel-1"].users_collection[0]
        check(dowel.get("SWMESH_group") == "dowel" and dowel.name == "dowel",
              where, "the dowel of gizmo, on the mesh of the arm of wrench, "
              "is in %s" % dowel.name)
        pin_col = g["pin-1"].users_collection[0]
        check(pin_col.get("SWMESH_group") == "pin" and pin_col.name == "pin.001",
              where, "the pin of gizmo is in %s" % pin_col.name)
        more = GIZMO_PARTS + [("c005", "pin-2", "ppin3", 0.9, "pin", 0.15, 0)]
        send("gizmo", where, mode=mode, update=True, rig_mode="APPEND",
             parts=more)
        pin2 = parts_of("gizmo_Default")["pin-2"]
        check(pin2.users_collection[0] == pin_col, where,
              "the new pin went into %s, not %s"
              % (pin2.users_collection[0].name, pin_col.name))
        check(mesh_of(pin2) is mesh_of(w["pin-1"]), where,
              "the new pin is not linked to the pin of wrench")
        g = parts_of("gizmo_Default")

    # A replace of wrench leaves gizmo its pin, and the pin its mesh.
    pin = mesh_of(g["pin-1"])
    send("wrench", where, mode=mode)
    check(mesh_of(parts_of("gizmo_Default")["pin-1"]) is pin, where,
          "the replace of wrench took the pin of gizmo")
    check(pin.users >= 2, where, "the pin mesh has %d user(s)" % pin.users)
    check(mesh_of(parts_of("wrench_Default")["pin-1"]) is pin, where,
          "the new wrench does not link to the pin again")
    # The mesh outlives the last import that made it.
    native_import.remove_previous("wrench_Default")
    check(mesh_of(parts_of("gizmo_Default")["pin-1"]).name == pin.name, where,
          "removing wrench removed the pin of gizmo")


def no_link():
    where = "link switch off"
    tl.fresh()
    send("wrench", where)
    send("gizmo", where, link=False)
    check(mesh_of(parts_of("wrench_Default")["pin-1"])
          is not mesh_of(parts_of("gizmo_Default")["pin-1"]), where,
          "a send with no links linked the pin")


def made_another_way():
    where = "made another way"
    full_addon()
    send("wrench", where, quads=True)
    send("gizmo", where, quads=False)
    check(mesh_of(parts_of("wrench_Default")["pin-1"])
          is not mesh_of(parts_of("gizmo_Default")["pin-1"]), where,
          "a send of triangles linked to a mesh of quads")
    send("gizmo", where, quads=True)
    check(mesh_of(parts_of("wrench_Default")["pin-1"])
          is mesh_of(parts_of("gizmo_Default")["pin-1"]), where,
          "two sends of quads did not link the pin")
    # The pin that gizmo took from wrench had its quads made there, so the
    # send of gizmo does not make them again on a mesh wrench holds too.
    finish = sorted(o.get("SWMESH_path") for o in native_import.to_finish(
        list(parts_of("gizmo_Default").values()), "gizmo_Default"))
    check(finish == ["arm-1", "frame-1", "lever-1"], where,
          "the send works again on %s" % finish)


def locked_geometry():
    where = "locked geometry"
    full_addon()
    from CADder import geometry_lock
    send("wrench", where)
    geometry_lock.set_locked(parts_of("wrench_Default")["pin-1"], True)
    send("gizmo", where)
    check(mesh_of(parts_of("wrench_Default")["pin-1"])
          is not mesh_of(parts_of("gizmo_Default")["pin-1"]), where,
          "a part linked to a mesh whose geometry is locked")


def defeature_scope():
    """Defeature takes the parts that share a mesh with the selection, but
    only of the documents asked about."""
    where = "defeature scope"
    tl.fresh()
    send("wrench", where)
    send("gizmo", where)
    pin = parts_of("wrench_Default")["pin-1"]
    got = sorted(o.name for o in tools.linked_parts(bpy.context, [pin]))
    check(got == [pin.name], where, "the scope is %s" % got)


# ── Copies ─────────────────────────────────────────────────────────────


def copies(mode):
    where = "%s copies" % mode
    tl.fresh()
    send("wrench", where, mode=mode)
    first = send("wrench", where, mode=mode, append=True)
    send("wrench", where, mode=mode, append=True)
    names = ["wrench_Default", "wrench_Default.001", "wrench_Default.002"]
    check(sorted(c.name for c in tl.tops()) == names, where,
          "top collections: %s" % [c.name for c in tl.tops()])
    check(any("a copy: wrench_Default.001" in line
              for line in first.get("log") or []), where,
          "the reply does not say it made a copy: %s" % first.get("log"))
    for stem in names:
        top_holds(where, stem)
        on_own_rig(where, stem)
    for stem in names[1:]:
        check(tl.top_of(stem).get("SWMESH_copy_of") == "wrench_Default", where,
              "%s does not say it is a copy" % stem)
        for path, obj in parts_of(stem).items():
            check(mesh_of(obj) is mesh_of(parts_of(names[0])[path]), where,
                  "%s of %s does not share the mesh of the import" % (path, stem))
    check(len(rigs()) == 3, where, "the rigs are %s" % rigs())
    check(not [n for n in tl.numbered() if n.endswith(("_Parts", "_Rig"))],
          where, "numbered copies: %s" % tl.numbered())

    # A send without the option replaces the import, and not its copies.
    kept = {s: pointers(s) for s in names[1:]}
    send("wrench", where, mode=mode)
    for stem in names[1:]:
        check(pointers(stem) == kept[stem], where,
              "the send of wrench replaced %s" % stem)
    check(len(rigs()) == 3, where, "the rigs are %s after a send" % rigs())

    # Posing the rig of the import moves no part of a copy.
    before = {p: o.matrix_world.copy() for p, o in parts_of(names[1]).items()}
    arm = rig_of(names[0])
    bone = arm.pose.bones[parts_of(names[0])["arm-1"].parent_bone]
    bone.rotation_mode = "XYZ"
    bone.rotation_euler = (0.0, 0.5, 0.0)
    bpy.context.view_layer.update()
    for path, obj in parts_of(names[1]).items():
        check(obj.matrix_world == before[path], where,
              "posing the import moved %s of the copy" % path)
    bone.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()


def copy_refresh():
    where = "copy refresh"
    tl.fresh()
    send("wrench", where)
    send("wrench", where, append=True)
    copy = "wrench_Default.001"
    # The user moves the copy away from the import, with its rig.
    arm = rig_of(copy)
    arm.matrix_world = Matrix.Translation((1.0, 0.0, 0.0)) @ arm.matrix_world
    bpy.context.view_layer.update()
    moved = {p: o.matrix_world.translation.x for p, o in parts_of(copy).items()}
    kept = {s: pointers(s) for s in ("wrench_Default", copy)}
    result = send("wrench", where, update=True, rig_mode="APPEND")
    rows = result.get("copies") or []
    check([r["import"] for r in rows] == [copy] and all(r["ok"] for r in rows),
          where, "the Refresh did not bring the copy up to date: %s" % rows)
    for stem in ("wrench_Default", copy):
        check(pointers(stem) == kept[stem], where,
              "the Refresh replaced parts of %s" % stem)
        on_own_rig(where, stem)
    for path, obj in parts_of(copy).items():
        check(abs(obj.matrix_world.translation.x - moved[path]) < 1e-6, where,
              "the Refresh moved %s of the copy back to the import" % path)

    # A lock on the rig of the import does not stop a build of a copy.
    rig_of("wrench_Default")[rig_build.LOCK_TAG] = True
    send("wrench", where, append=True)
    check(rig_of("wrench_Default.002") is not None, where,
          "no rig for the copy beside a locked rig: %s" % rigs())
    on_own_rig(where, "wrench_Default.002")
    rig_of("wrench_Default")[rig_build.LOCK_TAG] = False

    # The import deleted by the user: a Refresh builds it again, and a copy
    # stays a copy.
    native_import.remove_previous("wrench_Default")
    send("wrench", where, update=True, rig_mode="APPEND")
    check(tl.top_of("wrench_Default") is not None, where,
          "the Refresh did not build the import again")
    for stem in (copy, "wrench_Default.002"):
        top = tl.top_of(stem)
        check(top is not None and top.get("SWMESH_copy_of") == "wrench_Default",
              where, "%s is no longer a copy: %s" % (stem, top and top.name))
        on_own_rig(where, stem)
    on_own_rig(where, "wrench_Default")


def copy_panel_and_rebuild():
    """The Rig list works on the rig of a copy, and Rebuild from CAD asks
    for the parts of a copy and builds the copy again as a copy."""
    where = "copy panel"
    full_addon()
    send("wrench", where)
    send("wrench", where, append=True)
    copy = "wrench_Default.001"
    settings = bpy.context.scene.cad_link
    offered = [n for _i, n, *_r in rig_ui._rig_items(settings, bpy.context)]
    check(offered == ["wrench_Default.001_Rig", "wrench_Default_Rig"], where,
          "the Rig list offers %s" % offered)
    settings.rig = "0"
    check(rig_ui._find_rig(bpy.context) == rig_of(copy), where,
          "the panel works on %s" % rig_ui._find_rig(bpy.context))
    original = rig_of("wrench_Default").as_pointer()
    check("FINISHED" in bpy.ops.cadlink.build_rig(), where, "Build Rig failed")
    check("FINISHED" in bpy.ops.cadlink.relink_geometry(), where, "Relink failed")
    check(rig_of("wrench_Default").as_pointer() == original, where,
          "Build Rig for the copy replaced the rig of the import")
    on_own_rig(where, copy)
    on_own_rig(where, "wrench_Default")

    # Rebuild from CAD, Full Reimport, of the copy only.
    mesh, manifest = export("wrench_Default", WRENCH_PARTS, WRENCH_JOINTS)
    seen = []

    def request(op, timeout=None, instance=None, **fields):
        seen.append((op, fields.get("configuration")))
        if op != "export":
            raise cad_link.CadLinkError("no answer for " + op)
        return {"ok": True, "mesh": mesh, "manifest": manifest,
                "configuration": "Default"}
    real = cad_link.request
    cad_link.request = request
    try:
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for obj in parts_of(copy).values():
            obj.select_set(True)
        kept = pointers("wrench_Default")
        got = bpy.ops.cadlink.update_from_cad(what="EVERYTHING")
    finally:
        cad_link.request = real
    check("FINISHED" in got, where, "Full Reimport failed: %s" % got)
    check(seen == [("export", "Default")], where, "the requests were %s" % seen)
    check(pointers("wrench_Default") == kept, where,
          "the reimport of the copy replaced the import")
    top = tl.top_of(copy)
    check(top is not None and top.get("SWMESH_copy_of") == "wrench_Default",
          where, "the copy is no longer a copy")
    on_own_rig(where, copy)
    check(len(rigs()) == 2, where, "the rigs are %s" % rigs())


def main():
    for mode in MODES:
        across_assemblies(mode)
        copies(mode)
    no_link()
    defeature_scope()
    copy_refresh()
    made_another_way()
    locked_geometry()
    copy_panel_and_rebuild()
    print("native_link_smoke: OK: the same part of two assemblies is one "
          "mesh in all four hierarchy modes, and not with another "
          "appearance, with links off, made another way or locked; copies "
          "stand beside the import with rigs of their own, a send replaces "
          "only the import, a Refresh brings every copy up to date where "
          "the user put it, and the Rig list, Rebuild from CAD and "
          "Defeature keep to their import")


main()
