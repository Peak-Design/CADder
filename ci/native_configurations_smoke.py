# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for configurations side by side (CADder 1.2).

    blender -b --factory-startup --python-exit-code 1 -P ci/native_configurations_smoke.py

A user exports five to ten configurations of each assembly. Up to 1.1 a
send replaced the send before it, so two configurations could not be in
Blender together. From 1.2 each configuration is an import of its own,
named after the document and the configuration, with its own collection
and its own rig (Oscar, 2026-09-24). The configurations hold the same
parts, with the same paths and ids, so this checks that nothing one of
them does reaches another:

  * three configurations of one assembly stand side by side, each in
    "<stem>" with "<stem>_Parts" and "<stem>_Rig", in all four hierarchy
    modes,
  * a part that two configurations hold the same way is one mesh, and a
    part with another shape or another appearance is a mesh of its own,
  * each part rides the rig of its own configuration, and posing one rig
    moves no part of another,
  * a send or a Refresh of one configuration leaves the others as they
    are, and a Refresh of a configuration that is not in the scene does
    not take the import of another,
  * the first send of a document from 1.2 takes over its import from
    1.1, with the collection the user put work in,
  * a pose push moves only the parts of its configuration, and Match
    Geometry with the manifest of one configuration claims only its parts,
  * the registry says which configurations the scene holds,
  * the Rig panel offers the rig of each configuration, and a pick loads
    its manifest, so Build Rig rebuilds that rig and no other,
  * Rebuild from CAD asks for each configuration apart, and puts its
    answer on the parts of that configuration only,
  * a send of one configuration keeps the defeature settings of the
    others as they are.

Every send runs through bridge._run_job, the path SolidWorks drives.
"""

import json
import math
import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.ci import native_top_level_smoke as tl  # noqa: E402
from CADder.rig import cad_link, native_import, swmesh  # noqa: E402
from CADder.rig import ui as rig_ui  # noqa: E402

MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")
DOCUMENT = os.path.join(tempfile.gettempdir(), "smoke parts", "wrench.SLDASM")
TMP = os.path.join(tempfile.gettempdir(), "native_configurations_smoke")

GRAY = ("gray", (0.8, 0.8, 0.8, 1.0))
RED = ("red", (0.8, 0.1, 0.1, 1.0))

# (component id, path, persistent id, x, mesh name, size, material)
DEFAULT = [("c001", "base-1", "pbase", 0.0, "base", 0.05, 0),
           ("c002", "arm-1", "parm", 0.2, "arm", 0.10, 0),
           ("c003", "pin-1", "ppin", 0.4, "pin", 0.15, 0)]
# The pin is longer.
LONG = [p if p[4] != "pin" else p[:5] + (0.30, 0) for p in DEFAULT]
# The arm has another appearance.
RED_ARM = [p if p[4] != "arm" else p[:6] + (1,) for p in DEFAULT]
JOINTS = [("j001", "c001", "c002", 0.2), ("j002", "c002", "c003", 0.4)]
CONFIGURATIONS = (("Default", DEFAULT), ("Long", LONG), ("Red", RED_ARM))


def fail(where, msg):
    raise SystemExit("native_configurations_smoke: FAIL: %s: %s" % (where, msg))


def check(cond, where, msg):
    if not cond:
        fail(where, msg)


def write_mesh(path, parts):
    """One triangle per part, of the part's size, in the part's material,
    version 3."""
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


def export(stem, parts):
    os.makedirs(TMP, exist_ok=True)
    manifest = tl.write_manifest(os.path.join(TMP, stem + ".rig.json"), stem,
                                 [p[:5] for p in parts], JOINTS)
    mesh = write_mesh(os.path.join(TMP, stem + ".swmesh"), parts)
    return mesh, manifest


def send(configuration, parts, mode="FLAT", update=False, rig_mode=None,
         document=DOCUMENT, base="wrench"):
    """A send of one configuration, as CADder Bridge 1.2 posts it. With
    `configuration` None, as an add-in before 1.2 posts it."""
    stem = base + "_" + configuration if configuration else base
    mesh, manifest = export(stem, parts)
    payload = {
        "step": None, "mesh": mesh, "manifest": manifest,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": mode, "up_as": "ZPOS"},
        "source_document": document,
    }
    if configuration:
        payload["configuration"] = configuration
    if rig_mode:
        payload["rig_mode"] = rig_mode
    result = bridge._run_job(payload)
    check(result.get("ok"), "send %s %s" % (stem, mode),
          "the send failed: %s" % result.get("error"))
    return stem


def numbered():
    """Numbered copies among the names this send gives: the top, the parts
    and the rig collections. The collections inside the parts collection
    are named after the parts, as the objects are, and two configurations
    hold the same parts, so Blender numbers those, as it numbers the
    objects."""
    return [n for n in tl.numbered()
            if n.startswith("wrench") or n.endswith(("_Parts", "_Rig"))]


def rigs():
    return [o for o in bpy.data.objects
            if o.type == "ARMATURE" and o.get("RIG_rig")]


def rig_of(stem):
    found = [o for o in rigs() if o.get("RIG_import") == stem]
    return found[0] if len(found) == 1 else None


def parts_of(stem):
    """path -> the part of that import."""
    return {o.get("SWMESH_path"): o for o in bpy.data.objects
            if o.get("SWMESH_file") == stem and o.get("RIG_component_id")}


def mesh_of(obj):
    if obj.type == "MESH":
        return obj.data
    col = obj.instance_collection
    return col.objects[0].data if col is not None and col.objects else None


def pointers(stem):
    return {p: o.as_pointer() for p, o in parts_of(stem).items()}


def world(obj):
    return [list(r) for r in obj.matrix_world]


def moved(a, b, tol=1e-6):
    return max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb)) > tol


def send_all(mode):
    for configuration, parts in CONFIGURATIONS:
        send(configuration, parts, mode)
    return ["wrench_" + c for c, _ in CONFIGURATIONS]


# ── The cases ──────────────────────────────────────────────────────────


def side_by_side(mode):
    where = "%s side by side" % mode
    tl.fresh()
    stems = send_all(mode)
    check(sorted(c.name for c in tl.tops()) == stems, where,
          "top collections: %s" % [c.name for c in tl.tops()])
    for (configuration, _parts), stem in zip(CONFIGURATIONS, stems):
        top = tl.top_of(stem)
        check(top.name in tl.root_names(), where, "%s is not at the root" % stem)
        check(tl.names(top) == [stem + "_Parts", stem + "_Rig"], where,
              "%s holds %s" % (stem, tl.names(top)))
        check(top.get("SWMESH_configuration") == configuration
              and top.get("SWMESH_document") == DOCUMENT, where,
              "%s is tagged %s of %s" % (stem, top.get("SWMESH_configuration"),
                                         top.get("SWMESH_document")))
        parts = parts_of(stem)
        check(sorted(parts) == ["arm-1", "base-1", "pin-1"], where,
              "%s holds %s" % (stem, sorted(parts)))
        arm = rig_of(stem)
        check(arm is not None, where, "%s has no rig of its own" % stem)
        check([c.name for c in arm.users_collection] == [stem + "_Rig"], where,
              "the rig of %s is in %s" % (stem, [c.name for c in arm.users_collection]))
        for obj in parts.values():
            check(obj.get("SWMESH_configuration") == configuration, where,
                  "%s is tagged %s" % (obj.name, obj.get("SWMESH_configuration")))
            check(tl.rides(obj, arm), where,
                  "%s does not ride the rig of %s" % (obj.name, stem))
    check(len(rigs()) == 3, where, "the rigs are %s" % [o.name for o in rigs()])
    check(not numbered(), where, "numbered copies: %s" % numbered())

    # One mesh for a part that two configurations hold the same way.
    d, g, r = (parts_of(s) for s in stems)
    shared = lambda a, b: mesh_of(a) is not None and mesh_of(a) == mesh_of(b)
    check(shared(d["base-1"], g["base-1"]) and shared(d["base-1"], r["base-1"]),
          where, "the base is not one mesh")
    check(shared(d["arm-1"], g["arm-1"]), where,
          "the arm of Default and Long is not one mesh")
    check(not shared(d["arm-1"], r["arm-1"]), where,
          "the red arm shares the mesh of the gray one")
    check(shared(d["pin-1"], r["pin-1"]), where,
          "the pin of Default and Red is not one mesh")
    check(not shared(d["pin-1"], g["pin-1"]), where,
          "the long pin shares the mesh of the short one")

    # Posing one rig moves no part of another.
    before = {s: {p: world(o) for p, o in parts_of(s).items()} for s in stems}
    arm = rig_of(stems[0])
    bone = next(pb for pb in arm.pose.bones
                if pb.get("RIG_group") and parts_of(stems[0])["arm-1"].parent_bone
                == pb.name)
    bone.rotation_mode = "XYZ"
    bone.rotation_euler = (0.0, 0.5, 0.0)
    bpy.context.view_layer.update()
    check(moved(world(parts_of(stems[0])["arm-1"]), before[stems[0]]["arm-1"]),
          where, "posing the rig of Default did not move its arm")
    for stem in stems[1:]:
        for path, obj in parts_of(stem).items():
            check(not moved(world(obj), before[stem][path]), where,
                  "posing the rig of Default moved %s of %s" % (path, stem))
    bone.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()


def one_at_a_time(mode):
    """A send, and a Refresh, of one configuration."""
    where = "%s one configuration" % mode
    tl.fresh()
    stems = send_all(mode)
    default, long_, red = stems
    kept = {s: pointers(s) for s in stems}

    send("Long", LONG, mode)
    check(pointers(default) == kept[default] and pointers(red) == kept[red],
          where, "the send of Long changed another configuration")
    check(set(pointers(long_)) == set(kept[long_]), where,
          "Long holds %s" % sorted(pointers(long_)))
    check(len(rigs()) == 3, where, "the rigs are %s" % [o.name for o in rigs()])
    check(mesh_of(parts_of(long_)["base-1"]) == mesh_of(parts_of(default)["base-1"]),
          where, "the new Long does not share the base")
    check(sorted(c.name for c in tl.tops()) == stems, where,
          "top collections: %s" % [c.name for c in tl.tops()])
    check(not numbered(), where, "numbered copies: %s" % numbered())
    for stem in stems:
        for obj in parts_of(stem).values():
            check(tl.rides(obj, rig_of(stem)), where,
                  "%s of %s is off its rig" % (obj.name, stem))

    for rig_mode in ("KEEP", "APPEND", "REGENERATE"):
        kept = {s: pointers(s) for s in stems}
        send("Red", RED_ARM, mode, update=True, rig_mode=rig_mode)
        for stem in stems:
            check(pointers(stem) == kept[stem], where,
                  "a Refresh of Red (%s) replaced parts of %s" % (rig_mode, stem))
            for obj in parts_of(stem).values():
                check(tl.rides(obj, rig_of(stem)), where,
                      "after a Refresh of Red (%s), %s of %s is off its rig"
                      % (rig_mode, obj.name, stem))
        check(len(rigs()) == 3, where, "the rigs are %s after %s"
              % ([o.name for o in rigs()], rig_mode))

    # A Refresh of a configuration that is not in the scene is a send of
    # it. It does not take the import of another configuration, which
    # holds the same paths.
    kept = {s: pointers(s) for s in stems}
    send("Short", DEFAULT, mode, update=True, rig_mode="APPEND")
    for stem in stems:
        check(pointers(stem) == kept[stem], where,
              "the Refresh of Short took parts of %s" % stem)
    check(tl.top_of("wrench_Short") is not None and rig_of("wrench_Short"),
          where, "Short has no import of its own")


def legacy(mode, update):
    """The first send of a document from 1.2 takes over its import from
    1.1: "wrench_Top_Level" holding "wrench" and "wrench_Rig"."""
    where = "%s import from 1.1%s" % (mode, ", Refresh" if update else "")
    tl.fresh()
    send(None, DEFAULT, mode)
    top = tl.top_of("wrench")
    top.name = "wrench_Top_Level"
    bpy.data.collections["wrench_Parts"].name = "wrench"
    arm = rig_of("wrench")
    del arm["RIG_import"]          # a 1.1 rig says only its manifest
    top_pointer = top.as_pointer()
    note = bpy.data.objects.new("my note", None)
    top.objects.link(note)
    parts = pointers("wrench")

    send("Default", DEFAULT, mode, update=update, rig_mode="KEEP" if update else None)
    check(tl.top_of("wrench") is None, where, "an import keeps the stem of 1.1")
    top = tl.top_of("wrench_Default")
    check(top is not None and top.as_pointer() == top_pointer, where,
          "the import from 1.1 was not taken over")
    check(top.name == "wrench_Default", where, "the top collection is %s" % top.name)
    check(tl.names(top) == ["wrench_Default_Parts", "wrench_Default_Rig"], where,
          "the top collection holds %s" % tl.names(top))
    check([o.name for o in top.objects] == ["my note"], where,
          "the top collection holds %s" % [o.name for o in top.objects])
    check(len(rigs()) == 1 and rig_of("wrench_Default") is not None, where,
          "the rigs are %s" % [(o.name, o.get("RIG_import")) for o in rigs()])
    if update:
        check(pointers("wrench_Default") == parts, where,
              "the Refresh replaced the parts")
    for obj in parts_of("wrench_Default").values():
        check(tl.rides(obj, rig_of("wrench_Default")), where,
              "%s is off the rig" % obj.name)
    check(not numbered(), where, "numbered copies: %s" % numbered())


def pose_push():
    where = "pose push"
    tl.fresh()
    stems = send_all("FLAT")
    before = {s: {p: world(o) for p, o in parts_of(s).items()} for s in stems}
    # SolidWorks turned the arm of Long 30 degrees about its hinge, and the
    # pin went with it.
    turn = [[math.cos(0.5236), -math.sin(0.5236), 0.0, 0.0],
            [math.sin(0.5236), math.cos(0.5236), 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]

    def about(x, rows):
        to, back = tl._t(x), tl._t(-x)
        m = [[sum(to[i][k] * rows[k][j] for k in range(4)) for j in range(4)]
             for i in range(4)]
        return [[sum(m[i][k] * back[k][j] for k in range(4)) for j in range(4)]
                for i in range(4)]

    def placed(x):
        pivot = about(0.2, turn)
        start = tl._t(x)
        return [[sum(pivot[i][k] * start[k][j] for k in range(4))
                 for j in range(4)] for i in range(4)]

    rows = {"c001": tl._t(0.0), "c002": placed(0.2), "c003": placed(0.4)}
    poses = {"components": [
        {"id": cid, "sw_path": path, "sw_persistent_id": pid,
         "transform": [v for r in rows[cid] for v in r]}
        for cid, path, pid, *_rest in LONG]}
    result = bridge._run_job({"poses": poses, "source_document": DOCUMENT,
                              "configuration": "Long"})
    check(result.get("ok"), where, "the push failed: %s" % result.get("error"))
    check(moved(world(parts_of("wrench_Long")["arm-1"]),
                before["wrench_Long"]["arm-1"]), where,
          "the push did not move the arm of Long")
    for stem in ("wrench_Default", "wrench_Red"):
        for path, obj in parts_of(stem).items():
            check(not moved(world(obj), before[stem][path]), where,
                  "the push for Long moved %s of %s" % (path, stem))


def match_geometry():
    """Match Geometry in the panel, with the manifest of Long loaded,
    claims the parts of Long, and no part of another configuration."""
    where = "match geometry"
    tl.fresh()
    send_all("FLAT")
    before = {o.name: o.get("RIG_group") for o in bpy.data.objects
              if o.get("RIG_component_id")}
    for obj in parts_of("wrench_Default").values():
        del obj["RIG_group"]
    bpy.context.scene.cad_link.manifest_path = os.path.join(
        TMP, "wrench_Long.rig.json")
    check("FINISHED" in bpy.ops.cadlink.load_manifest(), where,
          "the manifest of Long did not load")
    check("FINISHED" in bpy.ops.cadlink.match_geometry(), where,
          "Match Geometry did not run")
    report = rig_ui._STATE["match_report"]
    claimed = sorted(e.object_name for e in report.matched)
    want = sorted(o.name for o in parts_of("wrench_Long").values())
    check(claimed == want, where, "Match Geometry claimed %s, not %s"
          % (claimed, want))
    for obj in parts_of("wrench_Default").values():
        check(obj.get("RIG_group") is None, where,
              "Match Geometry for Long gave %s of Default a group" % obj.name)
    for obj in parts_of("wrench_Red").values():
        check(obj.get("RIG_group") == before[obj.name], where,
              "Match Geometry for Long changed %s of Red" % obj.name)


def panel_rig():
    where = "panel rig"
    tl.fresh()
    stems = send_all("FLAT")
    settings = bpy.context.scene.cad_link
    offered = [name for _id, name, *_rest in rig_ui._rig_items(settings, bpy.context)]
    check(offered == [s + "_Rig" for s in stems], where,
          "the panel offers %s" % offered)
    check(settings.rig == "2", where,
          "after the sends the panel shows rig %s, not the last" % settings.rig)
    settings.rig = "0"
    check(rig_ui._STATE["manifest"] is not None
          and os.path.basename(rig_ui._STATE["manifest"].source_path)
          == "wrench_Default.rig.json", where, "the pick loaded %s"
          % getattr(rig_ui._STATE["manifest"], "source_path", None))
    check(rig_ui._find_rig(bpy.context) == rig_of("wrench_Default"), where,
          "the panel works on %s" % rig_ui._find_rig(bpy.context))
    kept = {s: rig_of(s).as_pointer() for s in stems[1:]}
    check("FINISHED" in bpy.ops.cadlink.build_rig(), where, "Build Rig failed")
    check("FINISHED" in bpy.ops.cadlink.relink_geometry(), where,
          "Relink failed")
    for stem in stems[1:]:
        check(rig_of(stem) is not None and rig_of(stem).as_pointer() == kept[stem],
              where, "Build Rig for Default changed the rig of %s" % stem)
    for stem in stems:
        for obj in parts_of(stem).values():
            check(tl.rides(obj, rig_of(stem)), where,
                  "%s of %s is off its rig" % (obj.name, stem))


def registry():
    where = "registry"
    tl.fresh()
    send_all("FLAT")
    held = bridge._scene_configurations()
    check(held == {DOCUMENT: ["Default", "Long", "Red"]}, where,
          "the scene holds %s" % held)
    check(DOCUMENT in bridge._scene_documents(), where,
          "the documents are %s" % bridge._scene_documents())
    bridge._state["configurations"] = held
    info = bridge._instance_info()
    check(info.get("configurations") == held, where,
          "the ping says %s" % info.get("configurations"))
    # A send of another assembly after them: the scene still holds wrench,
    # and says so, although the last send was of another document.
    gizmo = os.path.join(os.path.dirname(DOCUMENT), "gizmo.SLDASM")
    send("Default", DEFAULT, document=gizmo, base="gizmo")
    documents = bridge._scene_documents()
    check(DOCUMENT in documents and gizmo in documents, where,
          "the documents are %s" % documents)
    held = bridge._scene_configurations()
    check(held == {DOCUMENT: ["Default", "Long", "Red"], gizmo: ["Default"]},
          where, "the scene holds %s" % held)


def full_addon():
    """A new scene with the whole add-on on, not the rig package alone:
    Rebuild from CAD reads Mesh Quality, and the defeature settings are
    properties of the add-on."""
    try:
        rig.unregister()
    except Exception:                                   # noqa: BLE001
        pass
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    rig_ui._reset_state()


def defeature_settings():
    """A send of one configuration writes down the defeature settings of
    its own parts, and puts them back on its own parts. The parts of the
    other configurations have the same component ids."""
    where = "defeature"
    full_addon()
    stems = send_all("FLAT")
    default, long_, _red = stems
    arm = parts_of(default)["arm-1"]
    check(hasattr(arm, "cad_defeature"), where, "no defeature settings")
    arm.cad_defeature.enabled = True
    arm.cad_defeature.size = 0.004
    send("Long", LONG)
    arm = parts_of(default)["arm-1"]
    check(arm.cad_defeature.enabled and abs(arm.cad_defeature.size - 0.004) < 1e-9,
          where, "the send of Long changed the settings of Default")
    check(not parts_of(long_)["arm-1"].cad_defeature.enabled, where,
          "the arm of Long took the settings of the arm of Default")
    send("Default", DEFAULT)
    arm = parts_of(default)["arm-1"]
    check(arm.cad_defeature.enabled and abs(arm.cad_defeature.size - 0.004) < 1e-9,
          where, "a send of Default lost its own settings")


def rebuild_from_cad():
    """Rebuild from CAD asks for each configuration apart, and the answer
    goes on the parts of that configuration only."""
    where = "rebuild from CAD"
    full_addon()
    stems = send_all("FLAT")
    default, long_, red = stems
    seen = []

    def poses_answer(fields):
        seen.append(("poses", fields.get("configuration"),
                     fields.get("document_path")))
        parts = dict(CONFIGURATIONS)[fields.get("configuration")]
        return {"ok": True, "components": [
            {"id": cid, "sw_path": path, "sw_persistent_id": pid,
             "transform": [v for r in tl._t(x) for v in r]}
            for cid, path, pid, x, *_rest in parts]}

    def retessellate_answer(fields):
        seen.append(("retessellate", fields.get("configuration"),
                     fields.get("document_path")))
        # A finer pin: a larger triangle stands in for it.
        path = os.path.join(TMP, "reply-%s.swmesh" % fields.get("configuration"))
        pin = [p for p in DEFAULT if p[4] == "pin"]
        write_mesh(path, [pin[0][:5] + (0.9, 0)])
        return {"ok": True, "mesh": path, "triangles": 1, "tolerance_m": 0.0001}

    class Cad(object):
        def __enter__(self):
            self.real = cad_link.request

            def request(op, timeout=None, instance=None, **fields):
                answer = {"poses": poses_answer,
                          "retessellate": retessellate_answer}.get(op)
                if answer is None:
                    raise cad_link.CadLinkError("no answer for " + op)
                return answer(fields)
            cad_link.request = request
            return self

        def __exit__(self, *exc):
            cad_link.request = self.real
            return False

    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    pins = {s: parts_of(s)["pin-1"] for s in stems}
    for stem in (default, long_):
        pins[stem].select_set(True)
    red_mesh = mesh_of(pins[red])
    long_mesh = mesh_of(pins[long_])
    with Cad():
        got = bpy.ops.cadlink.update_from_cad(what="POSES")
        check("FINISHED" in got, where, "Poses did not run: %s" % got)
        check(sorted(seen) == [("poses", "Default", DOCUMENT),
                               ("poses", "Long", DOCUMENT)], where,
              "the requests were %s" % seen)
        del seen[:]
        pins[long_].select_set(False)
        got = bpy.ops.cadlink.update_from_cad(what="GEOMETRY")
        check("FINISHED" in got, where, "Geometry did not run: %s" % got)
    check(seen == [("retessellate", "Default", DOCUMENT)], where,
          "the requests were %s" % seen)
    check(mesh_of(pins[default]) is not red_mesh, where,
          "the pin of Default kept its mesh")
    check(mesh_of(pins[red]) is red_mesh, where,
          "the new pin of Default went on the pin of Red too")
    check(mesh_of(pins[long_]) is long_mesh, where,
          "the new pin of Default went on the pin of Long")


def main():
    for mode in MODES:
        side_by_side(mode)
    for mode in ("FLAT", "TREE"):
        one_at_a_time(mode)
        legacy(mode, update=False)
        legacy(mode, update=True)
    pose_push()
    match_geometry()
    panel_rig()
    registry()
    rebuild_from_cad()
    defeature_settings()
    print("native_configurations_smoke: OK: three configurations of one "
          "assembly stand side by side in all four hierarchy modes, each "
          "with its own collection and rig, identical parts share one mesh "
          "and parts with another shape or appearance do not, a send or a "
          "Refresh of one leaves the others, an import from 1.1 is taken "
          "over, and a pose push, the registry, Rebuild from CAD and the "
          "defeature settings keep to their configuration")


main()
