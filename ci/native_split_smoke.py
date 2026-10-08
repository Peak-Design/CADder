# SPDX-License-Identifier: GPL-3.0-or-later
"""Split by Material for a direct send: one object for each material.

    blender -b --factory-startup --python-exit-code 1 -P ci/native_split_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

With Split by Material on (Mesh Quality panel), a part from SolidWorks
with faces in two or more appearances comes in as that many objects. The
unit tests (ci/rig/test_split_material.py) pin how the mesh file is cut.
This smoke runs the sends through bridge._run_job, the path SolidWorks
drives, and checks what the scene holds:

  1. In each hierarchy mode: an object for each material, of one material
     each, on the bone of the part. Two placements of a part share the
     mesh of each piece. With the option off nothing changes.
  2. A refresh with no change keeps each piece: the object, its mesh, and
     a material that the user put on it.
  3. A part that moved in SolidWorks: all its pieces move.
  4. One material of a part with new geometry: that piece gets a new mesh,
     and the other piece keeps its mesh.
  5. The option off, and a refresh: the parts are whole again. On again:
     the pieces come back.
  6. Rebuild from CAD puts the new geometry on each piece, and a part
     that the scene holds whole stays whole.
"""

import os
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.ci import native_top_level_smoke as tl  # noqa: E402
from CADder.rig import native_import, swmesh  # noqa: E402
from CADder.rig import ui as rig_ui  # noqa: E402

MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")
TMP = os.path.join(tempfile.gettempdir(), "native_split_smoke")
DOC = os.path.join(tempfile.gettempdir(), "smoke parts", "panel.SLDASM")
STEM = "panel_Default"
GRAY, RED = 0, 1

# name -> [(size of one triangle, material)]
SHAPES = {"base": [(0.05, GRAY)],
          "arm": [(0.10, GRAY), (0.20, RED)]}
# (component id, path, persistent id, x, mesh name)
PARTS = [("c001", "base-1", "pbase", 0.0, "base"),
         ("c002", "arm-1", "parm1", 0.2, "arm"),
         ("c003", "arm-2", "parm2", 0.4, "arm")]
JOINTS = [("j001", "c001", "c002", 0.2), ("j002", "c001", "c003", 0.4)]
PIECES = ["arm-1|gray", "arm-1|red", "arm-2|gray", "arm-2|red", "base-1"]


def fail(where, msg):
    raise SystemExit("native_split_smoke: FAIL: %s: %s" % (where, msg))


def check(cond, where, msg):
    if not cond:
        fail(where, msg)


def write_mesh(path, parts, shapes):
    names = sorted({p[4] for p in parts})
    body = struct.pack("<III", swmesh.MAGIC, 3, 0)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 2, len(names), len(parts), 0)
    for name, rgba in (("gray", (0.8, 0.8, 0.8, 1.0)),
                       ("red", (0.8, 0.1, 0.1, 1.0))):
        body += tl._text(name) + struct.pack("<6f", *rgba, 0.5, 0.0)
        body += tl._text("") + struct.pack("<I", 0)
    for i, name in enumerate(names):
        tris = shapes[name]
        body += struct.pack("<i", i + 1) + tl._text(name)
        body += struct.pack("<II", 3 * len(tris), len(tris))
        for k, (size, _material) in enumerate(tris):
            z = 0.01 * k
            body += struct.pack("<9f", 0, 0, z, size, 0, z, 0, size, z)
        for k in range(len(tris)):
            body += struct.pack("<3i", 3 * k, 3 * k + 1, 3 * k + 2)
        for _size, material in tris:
            body += struct.pack("<i", material)
    for cid, where, _pid, x, name in parts:
        rows = [v for row in tl._t(x) for v in row]
        body += (struct.pack("<i", names.index(name) + 1) + tl._text(cid)
                 + tl._text(where) + tl._text(where)
                 + struct.pack("<16d", *rows) + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def send(where, parts=PARTS, shapes=SHAPES, mode="FLAT", update=False):
    os.makedirs(TMP, exist_ok=True)
    payload = {
        "step": None,
        "mesh": write_mesh(os.path.join(TMP, STEM + ".swmesh"), parts, shapes),
        "manifest": tl.write_manifest(
            os.path.join(TMP, STEM + ".rig.json"), STEM, parts, JOINTS),
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": True, "sync_poses": True, "build_rig": True,
                  "relink": True, "cleanup": True},
        "import_options": {"hierarchy_types": mode, "up_as": "ZPOS",
                           "tris_to_quads": False},
        "source_document": DOC, "configuration": "Default",
        "rig_mode": "APPEND",
    }
    result = bridge._run_job(payload)
    check(result.get("ok"), where, "the send failed: %s" % result.get("error"))
    bpy.context.view_layer.update()
    return result


def fresh(split):
    try:
        rig.unregister()
    except Exception:                                   # noqa: BLE001
        pass
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    rig_ui._reset_state()
    bpy.context.scene.stepper.split_by_material = split


def parts():
    return {o.get("SWMESH_path"): o for o in bpy.data.objects
            if o.get("SWMESH_file") == STEM and o.get("RIG_component_id")
            and not o.get("SWMESH_prototype")}


def mesh_of(obj):
    if obj.type == "MESH":
        return obj.data
    col = obj.instance_collection
    return col.objects[0].data if col is not None and col.objects else None


def materials_of(obj):
    """The names of the materials that the faces of the object use."""
    me = mesh_of(obj)
    return sorted({me.materials[p.material_index].name for p in me.polygons})


def x_of(obj):
    return round(obj.matrix_world.translation.x, 4)


def in_each_mode():
    for mode in MODES:
        where = "split, %s" % mode
        fresh(True)
        send(where, mode=mode)
        got = parts()
        check(sorted(got) == PIECES, where, "the parts are %s" % sorted(got))
        for path, obj in got.items():
            check(len(materials_of(obj)) == 1, where,
                  "%s has the materials %s" % (path, materials_of(obj)))
        check(materials_of(got["arm-1|red"]) != materials_of(got["arm-1|gray"]),
              where, "the two pieces of the arm have the same material")
        check(len(mesh_of(got["arm-1|red"]).polygons) == 1
              and len(mesh_of(got["arm-1|gray"]).polygons) == 1, where,
              "a piece does not hold the one triangle of its material")
        for a, b in (("arm-1|gray", "arm-1|red"), ("arm-2|gray", "arm-2|red")):
            check(got[a].parent is not None and got[a].parent is got[b].parent
                  and got[a].parent_bone == got[b].parent_bone
                  and got[a].parent_type == "BONE", where,
                  "%s and %s are not on one bone" % (a, b))
            check(got[a].get("RIG_component_id")
                  == got[b].get("RIG_component_id"), where,
                  "%s and %s are not of one component" % (a, b))
        check(got["arm-1|red"].parent_bone != got["arm-2|red"].parent_bone,
              where, "the two arms are on one bone")
        check(mesh_of(got["arm-1|red"]) is mesh_of(got["arm-2|red"])
              and mesh_of(got["arm-1|gray"]) is mesh_of(got["arm-2|gray"]),
              where, "two placements of a part do not share the mesh of a "
              "piece")
        check(got["arm-1|red"].name.startswith("arm-1.red"), where,
              "the piece is named %s" % got["arm-1|red"].name)
        if mode == "FLAT":
            cols = {c.name for c in got["arm-1|red"].users_collection} \
                & {c.name for c in got["arm-1|gray"].users_collection}
            check(any(n.startswith("arm") for n in cols), where,
                  "the pieces of the arm are not in one collection (%s)"
                  % sorted(cols))
        print("   ok: %s" % where)

        where = "whole, %s" % mode
        fresh(False)
        send(where, mode=mode)
        got = parts()
        check(sorted(got) == ["arm-1", "arm-2", "base-1"], where,
              "with the option off the parts are %s" % sorted(got))
        check(len(materials_of(got["arm-1"])) == 2, where,
              "the whole arm has the materials %s" % materials_of(got["arm-1"]))
        print("   ok: %s" % where)


def a_refresh_keeps_the_pieces():
    where = "a refresh with no change"
    fresh(True)
    send(where)
    was = dict(parts())
    meshes = {p: mesh_of(o) for p, o in was.items()}
    graphic = bpy.data.materials.new("Graphic")
    slot = mesh_of(was["arm-1|red"]).polygons[0].material_index
    mesh_of(was["arm-1|red"]).materials[slot] = graphic
    send(where, update=True)
    now = parts()
    check(sorted(now) == PIECES, where, "the parts are %s" % sorted(now))
    for path in PIECES:
        check(now[path] is was[path], where, "%s is a new object" % path)
        check(mesh_of(now[path]) is meshes[path], where,
              "%s has a new mesh" % path)
    check(materials_of(now["arm-1|red"]) == ["Graphic"], where,
          "the piece lost the material of the user (%s)"
          % materials_of(now["arm-1|red"]))
    print("   ok: %s" % where)

    where = "a part that moved"
    moved = [p if p[0] != "c002" else p[:3] + (0.3,) + p[4:] for p in PARTS]
    send(where, parts=moved, update=True)
    now = parts()
    check(x_of(now["arm-1|gray"]) == 0.3 and x_of(now["arm-1|red"]) == 0.3,
          where, "the pieces of the arm are at %s and %s"
          % (x_of(now["arm-1|gray"]), x_of(now["arm-1|red"])))
    check(x_of(now["arm-2|red"]) == 0.4 and x_of(now["base-1"]) == 0.0, where,
          "a part that did not move moved")
    print("   ok: %s" % where)

    where = "one material with new geometry"
    gray_mesh = mesh_of(now["arm-1|gray"])
    red_mesh = mesh_of(now["arm-1|red"])
    bigger = dict(SHAPES, arm=[(0.10, GRAY), (0.30, RED)])
    send(where, parts=moved, shapes=bigger, update=True)
    now = parts()
    check(sorted(now) == PIECES, where, "the parts are %s" % sorted(now))
    check(mesh_of(now["arm-1|gray"]) is gray_mesh, where,
          "the piece that did not change has a new mesh")
    check(mesh_of(now["arm-1|red"]) is not red_mesh, where,
          "the piece that changed kept its mesh")
    size = max(v.co.x for v in mesh_of(now["arm-1|red"]).vertices)
    check(abs(size - 0.30) < 1e-6, where,
          "the new mesh of the red piece is %s wide" % size)
    print("   ok: %s" % where)


def the_option_off_and_on():
    where = "the option off, and a refresh"
    fresh(True)
    send(where)
    bpy.context.scene.stepper.split_by_material = False
    send(where, update=True)
    now = parts()
    check(sorted(now) == ["arm-1", "arm-2", "base-1"], where,
          "the parts are %s" % sorted(now))
    stray = [o.name for o in bpy.data.objects
             if swmesh.PIECE in str(o.get("SWMESH_path") or "")]
    check(not stray, where, "pieces are left in the scene: %s" % stray)
    print("   ok: %s" % where)

    where = "the option on again"
    bpy.context.scene.stepper.split_by_material = True
    send(where, update=True)
    check(sorted(parts()) == PIECES, where,
          "the parts are %s" % sorted(parts()))
    print("   ok: %s" % where)


def rebuild_from_cad():
    for split in (True, False):
        where = "Rebuild from CAD, %s" % ("pieces" if split else "whole parts")
        fresh(split)
        send(where)
        was = dict(parts())
        # The scene decides, and not the option: turn it the other way.
        bpy.context.scene.stepper.split_by_material = not split
        finer = dict(SHAPES, arm=[(0.10, GRAY), (0.10, GRAY), (0.20, RED)])
        path = write_mesh(os.path.join(TMP, "finer.swmesh"), PARTS, finer)
        native_import.refine(bpy.context, path, stems=[STEM])
        now = parts()
        check(sorted(now) == sorted(was)
              and all(now[p] is was[p] for p in was), where,
              "the rebuild changed the parts: %s" % sorted(now))
        if split:
            check(len(mesh_of(now["arm-1|gray"]).polygons) == 2
                  and len(mesh_of(now["arm-1|red"]).polygons) == 1
                  and materials_of(now["arm-1|gray"]) != materials_of(
                      now["arm-1|red"]), where,
                  "the pieces did not get their own new geometry (%d and %d "
                  "faces)" % (len(mesh_of(now["arm-1|gray"]).polygons),
                              len(mesh_of(now["arm-1|red"]).polygons)))
        else:
            check(len(mesh_of(now["arm-1"]).polygons) == 3
                  and len(materials_of(now["arm-1"])) == 2, where,
                  "the whole part did not get all the new geometry")
        print("   ok: %s" % where)


in_each_mode()
a_refresh_keeps_the_pieces()
the_option_off_and_on()
rebuild_from_cad()
print("native_split_smoke: OK")
