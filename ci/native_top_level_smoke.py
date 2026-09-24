# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the top collection of a direct send.

    blender -b --factory-startup --python-exit-code 1 -P ci/native_top_level_smoke.py

A send put the assembly collection at the scene root and the rig
collection inside it, or loose among the other collections of the scene.
Now a send makes a top collection with the assembly collection and the
rig collection side by side in it, so the rig is easy to find (Oscar,
2026-09-23). From 1.2 the top collection is "<name>", the name of the
send (the document and the configuration), and holds "<name>_Parts" and
"<name>_Rig" (Oscar, 2026-09-24). This checks, in all four hierarchy
modes:

  * the layout after a send,
  * a re-send uses the same top collection, also after the user moved it,
    and keeps what the user put in it and in the assembly,
  * an update of a scene built before the top collection gets the new
    layout and keeps the parts and the rig,
  * an update of a scene that has the top collection changes nothing
    about the layout,
  * a renamed assembly renames the top collection and the rig,
  * a different assembly stands beside the first, each with its own rig,
    a send of one leaves the other as it is, and a lock on one rig does
    not stop the build of the other.

Every send runs through bridge._run_job, the path SolidWorks drives.
"""

import json
import os
import re
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import native_import, rig_build, swmesh  # noqa: E402
from CADder.rig import ui as rig_ui  # noqa: E402

MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")
PARTS = "_Parts"

# (component id, path, persistent id, x, mesh). A subassembly has no mesh.
HINGE = [("c001", "base-1", "pbase", 0.0, "base"),
         ("c002", "sub-1", "psub", 0.2, None),
         ("c003", "sub-1/arm-1", "parm", 0.2, "arm"),
         ("c004", "sub-1/pin-1", "ppin", 0.4, "pin")]
HINGE_JOINTS = [("j001", "c001", "c003", 0.2), ("j002", "c003", "c004", 0.4)]
GIZMO = [("c001", "frame-1", "pframe", 0.0, "frame"),
         ("c002", "lever-1", "plever", 0.3, "lever")]
GIZMO_JOINTS = [("j001", "c001", "c002", 0.3)]


def fail(where, msg):
    raise SystemExit("native_top_level_smoke: FAIL: %s: %s" % (where, msg))


def check(cond, where, msg):
    if not cond:
        fail(where, msg)


def _t(x):
    return [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def write_manifest(path, stem, parts, joints):
    shaped = [p for p in parts if p[4]]
    group_of = {cid: "g%03d" % i for i, (cid, *_rest) in enumerate(shaped)}
    data = {
        "manifest_version": "1.0.0",
        "generator": {"name": "Peak.Cadder", "version": "smoke"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": stem + ".step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": [
            dict({"id": cid, "sw_path": where,
                  "step_name": where.rpartition("/")[2],
                  "step_occurrence_path": None, "sw_persistent_id": pid,
                  "transform": _t(x)},
                 **({} if mesh else {"subassembly_solving": "flexible"}))
            for cid, where, pid, x, mesh in parts],
        "rigid_groups": [
            {"id": group_of[cid], "name": mesh, "components": [cid],
             "grounded": i == 0, "frame": None, "bbox_diag": 0.1}
            for i, (cid, where, pid, x, mesh) in enumerate(shaped)],
        "joints": [
            {"id": jid, "type": "revolute", "parent_group": group_of[a],
             "child_group": group_of[b], "origin": [x, 0.0, 0.0],
             "axis": [0.0, 0.0, 1.0], "secondary_axis": [1.0, 0.0, 0.0],
             "limits": None}
            for jid, a, b, x in joints],
        "loops": [], "warnings": [],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


def write_mesh(path, parts):
    """One triangle per mesh, version 3, with the paths of the tree."""
    shaped = [p for p in parts if p[4]]
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, len(shaped), len(shaped), 0)
    body += _text("gray") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    for i, (_cid, _where, _pid, _x, mesh) in enumerate(shaped):
        s = 0.05 * (i + 1)
        body += struct.pack("<i", i + 1) + _text(mesh)
        body += struct.pack("<II", 3, 1)
        body += struct.pack("<9f", 0, 0, 0, s, 0, 0, 0, s, 0)
        body += struct.pack("<3i", 0, 1, 2)
        body += struct.pack("<i", 0)
    for i, (cid, where, _pid, x, _mesh) in enumerate(shaped):
        rows = [v for row in _t(x) for v in row]
        body += (struct.pack("<i", i + 1) + _text(cid)
                 + _text(where.rpartition("/")[2]) + _text(where)
                 + struct.pack("<16d", *rows) + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


TMP = os.path.join(tempfile.gettempdir(), "native_top_level_smoke")


def send(stem, mode, parts=HINGE, joints=HINGE_JOINTS, update=False,
         rig_mode=None):
    os.makedirs(TMP, exist_ok=True)
    manifest = write_manifest(os.path.join(TMP, stem + ".rig.json"), stem,
                              parts, joints)
    mesh = write_mesh(os.path.join(TMP, stem + ".swmesh"), parts)
    payload = {
        "step": None, "mesh": mesh, "manifest": manifest,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": mode, "up_as": "ZPOS"},
    }
    if rig_mode:
        payload["rig_mode"] = rig_mode
    result = bridge._run_job(payload)
    check(result.get("ok"), "%s %s" % (stem, mode),
          "the send failed: %s" % result.get("error"))
    return mesh


def fresh():
    try:
        rig.unregister()
    except Exception:                                   # noqa: BLE001
        pass
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    rig_ui._reset_state()


# ── What the scene holds ───────────────────────────────────────────────


def tops():
    return [c for c in bpy.data.collections
            if c.get("SWMESH_role") == "top"]


def top_of(stem):
    found = [c for c in tops() if c.get("SWMESH_file") == stem]
    return found[0] if len(found) == 1 else None


def names(collection):
    return sorted(c.name for c in collection.children)


def import_names(collection):
    """The children the import made, and not the user."""
    return sorted(c.name for c in collection.children
                  if c.get("SWMESH_file") is not None)


def root_names():
    return names(bpy.context.scene.collection)


def numbered():
    """Collections with a Blender number on the end: a copy made because
    the name was taken."""
    return sorted(c.name for c in bpy.data.collections
                  if re.search(r"\.\d{3}$", c.name))


def parents_of(collection):
    out = []
    if collection.name in bpy.context.scene.collection.children:
        out.append(bpy.context.scene.collection.name)
    out.extend(c.name for c in bpy.data.collections
               if collection.name in c.children)
    return sorted(out)


def layout():
    """Every collection of the scene with the collections that hold it."""
    out = {}
    for col in bpy.context.scene.collection.children_recursive:
        out[col.name] = parents_of(col)
    return out


def the_rig(stem=None):
    """The one rig of the scene, or with `stem`, the one rig of that
    import. None when there is not exactly one."""
    arms = [o for o in bpy.data.objects
            if o.type == "ARMATURE" and o.get("RIG_rig")
            and (stem is None or o.get("RIG_import") == stem)]
    return arms[0] if len(arms) == 1 else None


def parts_of(stem):
    return {o.get("SWMESH_path"): o for o in bpy.data.objects
            if o.get("SWMESH_file") == stem and o.get("RIG_component_id")}


def rides(obj, arm):
    holder = obj
    while holder is not None:
        if holder.parent == arm:
            return True
        holder = holder.parent
    return False


def excluded(collection_name):
    def find(layer):
        if layer.collection.name == collection_name:
            return layer
        for child in layer.children:
            got = find(child)
            if got is not None:
                return got
        return None
    layer = find(bpy.context.view_layer.layer_collection)
    return layer is not None and layer.exclude


def user_collection(name, parent, obj_name):
    col = bpy.data.collections.new(name)
    parent.children.link(col)
    obj = bpy.data.objects.new(obj_name, None)
    col.objects.link(obj)
    return col


def check_assembly(where, mode, stem="hinge"):
    """The assembly collection is laid out as a send made it before the
    top collection."""
    asm = bpy.data.collections.get(stem + PARTS)
    check(asm is not None, where, "no %s collection" % (stem + PARTS))
    parts = parts_of(stem)
    check(sorted(parts) == ["base-1", "sub-1/arm-1", "sub-1/pin-1"], where,
          "the parts are %s" % sorted(parts))
    if mode == "FLAT":
        check(asm.get("SWMESH_role") == "flat", where, "not a flat root")
        check(import_names(asm) == ["arm", "base", "pin"], where,
              "the flat groups are %s" % import_names(asm))
    else:
        check(asm.get("SWMESH_role") == "hierarchy", where,
              "not a hierarchy root")
    if mode == "TREE":
        check(import_names(asm) == ["sub-1"], where, "the branches are %s" % import_names(asm))
        check(sorted(o.name for o in bpy.data.collections["sub-1"].objects)
              == ["arm-1", "pin-1"], where, "the parts are not in sub-1")
        check(parts["base-1"].name in asm.objects, where,
              "the base is not in the assembly collection")
    if mode == "EMPTIES":
        # Every part in the assembly collection itself. The relink takes
        # off the sub-1 empty, because its two parts ride two bones.
        check(all(o.name in asm.objects for o in parts.values()), where,
              "a part is not in the assembly collection")
        check(import_names(asm) == [], where,
              "EMPTIES made collections %s" % import_names(asm))
    if mode == "COLLECTION_INSTANCES":
        comps = stem + ".components"
        check(import_names(asm) == [comps], where,
              "the assembly collection holds %s" % import_names(asm))
        check(excluded(comps), where, "the prototypes are not excluded")


def check_top(where, mode, stem="hinge", rig_name=None, at_root=True,
              extra=(), others=False):
    """The top collection holds the assembly collection and the rig
    collection, and whatever the test put in it, nothing else. With
    `others`, the scene holds other imports too."""
    top = top_of(stem)
    check(top is not None, where, "no single top collection of %s: %s"
          % (stem, [c.name for c in tops()]))
    check(top.name == stem, where, "the top collection is %s" % top.name)
    if at_root:
        check(top.name in root_names(), where,
              "the top collection is not at the scene root: %s" % root_names())
    check(stem + PARTS not in root_names(), where,
          "the assembly collection is still at the scene root")
    want = sorted([stem + PARTS, rig_name or stem + "_Rig"] + list(extra))
    check(names(top) == want, where,
          "the top collection holds %s, not %s" % (names(top), want))
    check_assembly(where, mode, stem)
    arm = the_rig(stem if others else None)
    check(arm is not None, where, "no single rig")
    check([c.name for c in arm.users_collection] == [rig_name or stem + "_Rig"],
          where, "the rig is in %s" % [c.name for c in arm.users_collection])
    for obj in parts_of(stem).values():
        check(rides(obj, arm), where, "%s does not ride the rig" % obj.name)
    check(not numbered(), where, "numbered copies: %s" % numbered())
    # The widgets of the bones are in the rig collection only, and hidden.
    # A second send left them at the scene root too, not excluded there.
    check("SW_widgets" not in root_names(), where,
          "the widget collection is at the scene root")
    shown = []

    def walk(layer):
        if layer.collection.name == "SW_widgets" and not layer.exclude:
            shown.append(layer)
        for child in layer.children:
            walk(child)
    walk(bpy.context.view_layer.layer_collection)
    check(not shown, where, "the widget collection shows in the view layer")
    return top


# ── The cases ──────────────────────────────────────────────────────────


def first_send(mode):
    where = "%s send" % mode
    fresh()
    mesh = send("hinge", mode)
    top = check_top(where, mode)
    check(not top.objects, where, "objects in the top collection: %s"
          % [o.name for o in top.objects])
    # Nothing else takes the top collection for the assembly: the update
    # finds the assembly collection, the Rebuild from CAD panel reads the
    # tree the send was built with, and the bridge sees no STEP import.
    scope = native_import._Scope(bpy.context.scene)
    found = native_import.standing("hinge", swmesh.load(mesh), scope)
    check(found is not None and found.name == "hinge" + PARTS, where,
          "the update finds %s" % (found.name if found else None))
    read = rig_ui._options_in_scene("hinge").get("hierarchy_types")
    check(read == mode, where, "the scene reads as %s" % read)
    check(bridge._step_import_of(mesh) == [], where,
          "the send reads as a STEP import")


def resend(mode):
    where = "%s re-send" % mode
    fresh()
    send("hinge", mode)
    top = top_of("hinge")
    top["smoke_mark"] = mode
    pointer = top.as_pointer()
    scene_root = bpy.context.scene.collection
    # The user moves the top collection into a collection of their own,
    # and puts an object and a collection of their own in it.
    mine = bpy.data.collections.new("My scenes")
    scene_root.children.link(mine)
    scene_root.children.unlink(top)
    mine.children.link(top)
    light = bpy.data.objects.new("my light", None)
    top.objects.link(light)
    user_collection("my notes", top, "note")
    # And a collection of their own inside the assembly: the rig of one
    # part, next to the part.
    holder = bpy.data.collections["sub-1"] if mode == "TREE" \
        else bpy.data.collections["hinge" + PARTS]
    user_collection("arm_Rig", holder, "arm handle")

    send("hinge", mode)
    top = top_of("hinge")
    check(top is not None and top.as_pointer() == pointer
          and top.get("smoke_mark") == mode, where,
          "the send made a new top collection")
    check(parents_of(top) == ["My scenes"], where,
          "the top collection moved to %s" % parents_of(top))
    check_top(where, mode, at_root=False, extra=("arm_Rig", "my notes"))
    check([o.name for o in top.objects] == ["my light"], where,
          "the top collection holds %s" % [o.name for o in top.objects])
    for name, obj in (("my notes", "note"), ("arm_Rig", "arm handle")):
        col = bpy.data.collections.get(name)
        check(col is not None and [o.name for o in col.objects] == [obj],
              where, "%s lost what it held" % name)
        check(parents_of(col) == [top.name], where,
              "%s is in %s" % (name, parents_of(col)))


def old_layout(stem="hinge", into=None):
    """Makes the scene a send made before the top collection: the
    assembly collection at the scene root, the rig collection inside it
    (or inside `into`, a collection of its tree)."""
    top = top_of(stem)
    scene_root = bpy.context.scene.collection
    asm = bpy.data.collections[stem + PARTS]
    rig_col = bpy.data.collections[stem + "_Rig"]
    top.children.unlink(asm)
    scene_root.children.link(asm)
    top.children.unlink(rig_col)
    (into or asm).children.link(rig_col)
    scene_root.children.unlink(top)
    bpy.data.collections.remove(top)
    check(not tops(), "old layout", "a top collection is left")


def old_update(mode, rig_mode, into_group=False):
    where = "%s update of an old scene, %s" % (mode, rig_mode)
    fresh()
    send("hinge", mode)
    old_layout(into=bpy.data.collections["base"] if into_group else None)
    arm = the_rig()
    arm_pointer = arm.as_pointer()
    bones = sorted(b.name for b in arm.data.bones)
    parts = {path: obj.as_pointer() for path, obj in parts_of("hinge").items()}
    # A collection of the user's own inside the assembly stays where it is.
    holder = "sub-1" if mode == "TREE" else "hinge" + PARTS
    user_collection("arm_Rig", bpy.data.collections[holder], "arm handle")

    send("hinge", mode, update=True, rig_mode=rig_mode)
    rig_name = "hinge_Rig"
    top = check_top(where, mode, rig_name=rig_name)
    check(parents_of(bpy.data.collections["arm_Rig"]) == [holder], where,
          "the user's collection moved to %s"
          % parents_of(bpy.data.collections["arm_Rig"]))
    now = {path: obj.as_pointer() for path, obj in parts_of("hinge").items()}
    check(now == parts, where, "the update replaced parts")
    if rig_mode in ("KEEP", "APPEND"):
        check(the_rig().as_pointer() == arm_pointer, where,
              "the rig was replaced")
        check(sorted(b.name for b in the_rig().data.bones) == bones, where,
              "the bones changed")
    check(bpy.data.collections["hinge" + PARTS].name in top.children, where,
          "the assembly collection is not in the top collection")


def old_resend(mode, locked):
    """A send, and not an update, over a scene from before the top
    collection. The rig collection comes out of the assembly collection
    with the old import, and the rig build or, for a locked rig, the
    bridge puts it next to the new assembly collection."""
    where = "%s re-send of an old scene%s" % (mode, ", locked" if locked else "")
    fresh()
    send("hinge", mode)
    old_layout()
    arm = the_rig()
    if locked:
        arm[rig_build.LOCK_TAG] = True
    pointer = arm.as_pointer()
    send("hinge", mode)
    check_top(where, mode)
    if locked:
        check(the_rig().as_pointer() == pointer, where,
              "the locked rig was replaced")


def new_update(mode):
    where = "%s update of a new scene" % mode
    fresh()
    send("hinge", mode)
    holder = "sub-1" if mode == "TREE" else "hinge" + PARTS
    user_collection("arm_Rig", bpy.data.collections[holder], "arm handle")
    before = layout()
    send("hinge", mode, update=True, rig_mode="APPEND")
    check(layout() == before, where, "the layout changed: %s, was %s"
          % (layout(), before))
    check_top(where, mode, extra=())


def renamed(mode):
    where = "%s renamed assembly" % mode
    fresh()
    send("hinge", mode)
    pointer = top_of("hinge").as_pointer()
    send("hinge2", mode, update=True, rig_mode="KEEP")
    check(top_of("hinge") is None, where, "a top collection keeps the old tag")
    top = top_of("hinge2")
    check(top is not None and top.as_pointer() == pointer, where,
          "the renamed assembly has another top collection")
    # KEEP keeps the rig itself, and it takes the new name with the rest.
    arm = the_rig()
    check(arm is not None and arm.get("RIG_import") == "hinge2", where,
          "the rig drives %s" % (arm.get("RIG_import") if arm else None))
    check_top(where, mode, stem="hinge2", rig_name="hinge2_Rig")


def other_assembly(mode):
    """A different assembly stands beside the first, each with its own
    rig (Oscar, 2026-09-24). Up to 1.1 a send removed every other import."""
    where = "%s different assembly" % mode
    fresh()
    send("hinge", mode)
    hinge_top = top_of("hinge").as_pointer()
    hinge_parts = {p: o.as_pointer() for p, o in parts_of("hinge").items()}
    send("gizmo", mode, parts=GIZMO, joints=GIZMO_JOINTS)
    check(sorted(c.name for c in tops()) == ["gizmo", "hinge"], where,
          "top collections: %s" % [c.name for c in tops()])
    top = top_of("gizmo")
    check(names(top) == ["gizmo" + PARTS, "gizmo_Rig"], where,
          "the top collection holds %s" % names(top))
    check(top.name in root_names(), where, "not at the scene root")
    check(not numbered(), where, "numbered copies: %s" % numbered())
    check(top_of("hinge").as_pointer() == hinge_top
          and {p: o.as_pointer() for p, o in parts_of("hinge").items()}
          == hinge_parts, where, "the send of gizmo changed hinge")
    check_top(where, mode, others=True)
    gizmo = the_rig("gizmo")
    check(gizmo is not None, where, "gizmo has no rig of its own")
    for obj in parts_of("gizmo").values():
        check(rides(obj, gizmo), where, "%s does not ride gizmo_Rig" % obj.name)

    # A send of hinge again replaces hinge only.
    gizmo_parts = {p: o.as_pointer() for p, o in parts_of("gizmo").items()}
    send("hinge", mode)
    check({p: o.as_pointer() for p, o in parts_of("gizmo").items()}
          == gizmo_parts, where, "the send of hinge changed gizmo")
    check(len([o for o in bpy.data.objects if o.type == "ARMATURE"]) == 2,
          where, "the rigs are %s" % [o.name for o in bpy.data.objects
                                      if o.type == "ARMATURE"])
    check_top(where, mode, others=True)

    # A lock on the rig of hinge does not stop a build of gizmo, and the
    # send of gizmo leaves the locked rig as it is.
    locked = the_rig("hinge")
    locked[rig_build.LOCK_TAG] = True
    pointer = locked.as_pointer()
    old_gizmo = the_rig("gizmo").as_pointer()
    send("gizmo", mode, parts=GIZMO, joints=GIZMO_JOINTS)
    check(the_rig("hinge").as_pointer() == pointer, where,
          "the locked rig was replaced")
    check(the_rig("gizmo") is not None
          and the_rig("gizmo").as_pointer() != old_gizmo, where,
          "gizmo was not built again beside a locked rig")
    check(names(top_of("gizmo")) == ["gizmo" + PARTS, "gizmo_Rig"], where,
          "the new top collection holds %s" % names(top_of("gizmo")))

    # With nothing left in them, the top collections go.
    locked[rig_build.LOCK_TAG] = False
    native_import.remove_previous()
    check(not tops(), where, "top collections left: %s"
          % [c.name for c in tops()])


def long_names():
    where = "long names"
    for stem in ("A" * 70, "B" * 50, "Ü" * 40, "short"):
        for name in (native_import._top_name(stem),
                     native_import._parts_name(stem)):
            check(len(name.encode("utf-8")) <= 63, where,
                  "%r is %d bytes" % (name, len(name.encode("utf-8"))))
        name = native_import._parts_name(stem)
        check(name.endswith(PARTS), where, "%r lost the suffix" % name)
    check(native_import._top_name("short") == "short", where,
          "the top collection of short is %r" % native_import._top_name("short"))
    fresh()
    stem = "Long-Assembly-Name-" + "x" * 60
    send(stem, "FLAT")
    top = top_of(stem)
    check(top is not None and top.name == native_import._top_name(stem),
          where, "the top collection of a long stem is %s"
          % (top.name if top is not None else None))
    parts = [c for c in top.children if c.get("SWMESH_role") == "flat"]
    check(len(parts) == 1 and parts[0].name == native_import._parts_name(stem),
          where, "the parts collection of a long stem is %s"
          % [c.name for c in top.children])


def main():
    for mode in MODES:
        first_send(mode)
        resend(mode)
        for rig_mode in ("KEEP", "APPEND", "REGENERATE"):
            old_update(mode, rig_mode)
        old_resend(mode, locked=False)
        old_resend(mode, locked=True)
        new_update(mode)
        renamed(mode)
        other_assembly(mode)
    # A build before the top collection could also park the rig deeper in
    # the tree, in the one collection that held every part.
    old_update("FLAT", "KEEP", into_group=True)
    long_names()
    print("native_top_level_smoke: OK: in FLAT, TREE, EMPTIES and "
          "COLLECTION_INSTANCES a send puts <name>_Parts and <name>_Rig "
          "side by side in <name>, a re-send uses it again where the user "
          "put it, an update wraps an older scene and keeps the parts and "
          "the rig, a rename renames it, and a different assembly stands "
          "beside the first with its own rig")


# Other smokes import the helpers above.
if __name__ == "__main__":
    main()
