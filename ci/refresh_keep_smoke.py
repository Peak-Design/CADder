# SPDX-License-Identifier: GPL-3.0-or-later
"""Refresh from disk: does it keep the user's work, and does it keep the size?

    blender -b --factory-startup --python-exit-code 1 -P ci/refresh_keep_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Three things are checked.

  1. The size. A refresh must never resize the assembly. It used to, by a
     factor of 1000, whenever the import settings could not be found: the
     fall-back defaults cannot repeat a custom scale, and a background
     import leaves nothing to find because it records the settings on a
     worker scene that is thrown away. The same happens when the importing
     scene and the refreshing scene have different unit lengths.

  2. The work built on top. Modifiers, constraints, animation, vertex
     groups, custom properties, object color and an assigned material all
     have to survive, because a refresh keeps the objects and moves only the
     CAD data onto them.

  3. Placement. A part the user moved stays where they put it. A part that
     moved in CAD moves. A part that moved in both does both.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy
from mathutils import Vector

bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import main as m, refresh as R

FAILS = []
MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path, n_bodies=3, offset=0.0):
    """n separate parts in one assembly. `offset` moves the last one, which
    is how a part that was edited in CAD is simulated."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for i in range(n_bodies):
        dy = offset if i == n_bodies - 1 else 0.0
        shape = BRepPrimAPI_MakeBox(
            gp_Pnt(i * 30.0, dy, 0), 10, 10, 10).Shape()
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString("body%d" % i))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
    m._cache_drop(STEP)


def parts():
    return sorted([o for o in bpy.data.objects
                   if o.get("STEP_file") == STEP and o.type == "MESH"
                   and len(o.data.vertices)],
                  key=lambda o: o.name)


def world_size(obj):
    """Measured off the mesh and the matrix, so no stale depsgraph value can
    make a resize look like no change."""
    bpy.context.view_layer.update()
    pts = [obj.matrix_world @ v.co for v in obj.data.vertices]
    return round(max(p.x for p in pts) - min(p.x for p in pts), 6)


def world_min(obj):
    bpy.context.view_layer.update()
    pts = [obj.matrix_world @ v.co for v in obj.data.vertices]
    return Vector((min(p.x for p in pts), min(p.y for p in pts),
                   min(p.z for p in pts)))


tmp = tempfile.mkdtemp(prefix="stepper_keep_")
STEP = os.path.join(tmp, "keep_fixture.step")
write_step(STEP)


# -- 1. the size never changes -----------------------------------------------

print("\n== the size survives a refresh")

# The import settings are gone, exactly as a background import leaves them.
# Without them the refresh falls back to the defaults, which do not know
# about the custom scale, and the whole assembly came back 1000x smaller.
clean()
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z", custom_scale=1.0)
was = world_size(parts()[0])
R.forget(bpy.context.scene, STEP)
check(R.settings_for(bpy.context.scene, STEP) is None, "the record is gone")
check(R.stamped_settings(STEP) is not None,
      "but the objects still carry it")
bpy.ops.stepper.refresh_file(filepath=STEP)
now = world_size(parts()[0])
check(was == now, "custom scale kept with no record (%s -> %s)" % (was, now))

# The import was made in a scene with a different unit length, which is what
# the background worker did before it was told the parent's.
clean()
bpy.context.scene.unit_settings.scale_length = 1.0
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z")
was = world_size(parts()[0])
bpy.context.scene.unit_settings.scale_length = 0.01
bpy.ops.stepper.refresh_file(filepath=STEP)
now = world_size(parts()[0])
check(was == now, "unit length change does not resize (%s -> %s)" % (was, now))

# Nothing unusual at all: the plain case must still be exact.
for mode in MODES:
    clean()
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    was = [world_size(o) for o in parts()]
    bpy.ops.stepper.refresh_file(filepath=STEP)
    now = [world_size(o) for o in parts()]
    check(was == now, "%s: same size (%s -> %s)" % (mode, was[:1], now[:1]))


# -- 2. the work built on top survives ---------------------------------------

for mode in MODES:
    print("\n== the user's work survives: %s" % mode)
    clean()
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    obj = parts()[0]
    name = obj.name

    obj.modifiers.new("My Bevel", "BEVEL").width = 0.002
    obj.modifiers.new("My Subsurf", "SUBSURF").levels = 2
    obj.modifiers.new("My Weighted", "WEIGHTED_NORMAL")
    gn = obj.modifiers.new("My Nodes", "NODES")
    gn.node_group = bpy.data.node_groups.new("KeepMe", "GeometryNodeTree")

    target = bpy.data.objects.new("track target", None)
    bpy.context.scene.collection.objects.link(target)
    con = obj.constraints.new("COPY_LOCATION")
    con.target = target
    con.influence = 0.25

    obj.color = (1.0, 0.0, 0.0, 1.0)
    obj["my own property"] = 42
    obj.vertex_groups.new(name="My Group")
    obj.keyframe_insert(data_path="hide_viewport", frame=7)

    mine = bpy.data.materials.new("My Material")
    if obj.data.materials:
        obj.data.materials[0] = mine
    else:
        obj.data.materials.append(mine)

    verts_before = len(obj.data.vertices)
    bpy.ops.stepper.refresh_file(filepath=STEP)

    same = bpy.data.objects.get(name)
    check(same is not None and same is obj,
          "the object datablock is the same one, not a rebuild")
    if same is None:
        continue
    mods = [md.name for md in same.modifiers]
    check(mods == ["My Bevel", "My Subsurf", "My Weighted", "My Nodes"],
          "every modifier is still there, in order (%s)" % mods)
    check(any(md.type == "BEVEL" and abs(md.width - 0.002) < 1e-9
              for md in same.modifiers), "with its settings")
    check(any(md.type == "NODES" and md.node_group is not None
              and md.node_group.name == "KeepMe"
              for md in same.modifiers), "and its node group")
    check([c.type for c in same.constraints] == ["COPY_LOCATION"]
          and abs(same.constraints[0].influence - 0.25) < 1e-9,
          "the constraint survived")
    check(same.constraints and same.constraints[0].target is target,
          "still pointing at its target")
    check(same.animation_data is not None
          and same.animation_data.action is not None, "the animation survived")
    check(tuple(same.color) == (1.0, 0.0, 0.0, 1.0), "the object color survived")
    check(same.get("my own property") == 42, "the custom property survived")
    check("My Group" in [g.name for g in same.vertex_groups],
          "the vertex group survived")
    check([ms.name for ms in same.data.materials][:1] == ["My Material"],
          "the assigned material survived (%s)"
          % [ms.name for ms in same.data.materials])
    check(len(same.data.vertices) == verts_before,
          "and the geometry was re-imported")


# -- 3. placement -------------------------------------------------------------

print("\n== placement")

# A part the user moved stays moved.
clean()
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z")
obj = parts()[0]
name, start = obj.name, world_min(obj).copy()
obj.location = obj.location + Vector((0.5, 0.25, 0.125))
moved = world_min(obj).copy()
bpy.ops.stepper.refresh_file(filepath=STEP)
after = world_min(bpy.data.objects[name])
check((after - moved).length < 1e-5,
      "a part the user moved stays where they put it (off by %.6f)"
      % (after - moved).length)

# An untouched part does not drift, however many times it is refreshed.
other = parts()[1]
other_name, other_at = other.name, world_min(other).copy()
for _ in range(3):
    bpy.ops.stepper.refresh_file(filepath=STEP)
drift = (world_min(bpy.data.objects[other_name]) - other_at).length
check(drift < 1e-6, "an untouched part does not drift (%.9f)" % drift)

# A part that moved in CAD moves, and the user's own move rides on top.
clean()
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z")
last = parts()[-1]
last_name = last.name
last.location = last.location + Vector((0.0, 0.0, 1.0))
before = world_min(last).copy()
write_step(STEP, 3, offset=50.0)         # the part is moved 50mm in Y in CAD
bpy.ops.stepper.refresh_file(filepath=STEP)
after = world_min(bpy.data.objects[last_name])
check(abs((after - before).y - 0.05) < 1e-5,
      "a CAD move comes through (%.6f, expected 0.05)" % (after - before).y)
check(abs((after - before).z) < 1e-6,
      "and the user's own move is still applied on top")


# -- 4. the assembly changes --------------------------------------------------

print("\n== the assembly changes")
for mode in MODES:
    clean()
    write_step(STEP, 3)
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    kept = parts()[0]
    kept.modifiers.new("Keep Me", "BEVEL")
    kept_name = kept.name

    write_step(STEP, 4)                  # a component is added in CAD
    bpy.ops.stepper.refresh_file(filepath=STEP)
    check(len(parts()) == 4, "%s: the new component arrived (%d)"
          % (mode, len(parts())))
    check(all(o.users_collection for o in parts()),
          "%s: nothing was left unlinked" % mode)
    survivor = bpy.data.objects.get(kept_name)
    check(survivor is not None and "Keep Me" in [md.name for md in survivor.modifiers],
          "%s: the untouched parts kept their modifiers" % mode)

    write_step(STEP, 2)                  # two components are deleted in CAD
    bpy.ops.stepper.refresh_file(filepath=STEP)
    check(len(parts()) == 2, "%s: the deleted components are gone (%d)"
          % (mode, len(parts())))
    check(not [o for o in bpy.data.objects
               if o.get("STEP_file") == STEP and not o.users_collection],
          "%s: no orphan left behind" % mode)



# -- 5. repeated parts, when the CAD tree is renumbered -----------------------

print("\n== repeated occurrences of one part")


def write_assembly(path, extras=0):
    """One leaf part used twice, under a sub-assembly used twice. `extras`
    adds components BEFORE it, which renumbers everything after them and is
    what takes the per-occurrence ids away from a refresh."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    top = tool.NewShape()
    TDataStd_Name.Set_s(top, TCollection_ExtendedString("top"))

    for i in range(extras):
        spacer = tool.AddShape(
            BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 5, 5, 5).Shape(), False)
        TDataStd_Name.Set_s(spacer, TCollection_ExtendedString("spacer%d" % i))
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(-40.0 * (i + 1), 0, 0))
        tool.AddComponent(top, spacer, TopLoc_Location(trsf))

    leaf = tool.AddShape(
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 20, 30).Shape(), False)
    TDataStd_Name.Set_s(leaf, TCollection_ExtendedString("leaf"))
    sub = tool.NewShape()
    TDataStd_Name.Set_s(sub, TCollection_ExtendedString("sub"))
    for i in range(2):
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(i * 40.0, 0, 0))
        tool.AddComponent(sub, leaf, TopLoc_Location(trsf))
    for i in range(2):
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(0, i * 80.0, 0))
        tool.AddComponent(top, sub, TopLoc_Location(trsf))
    tool.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


for mode in ("TREE", "EMPTIES"):
    clean()
    write_assembly(STEP, extras=0)
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    leaves = [o for o in parts() if "leaf" in (o.get("STEP_name") or "")]
    check(len(leaves) == 4, "%s: four occurrences of the one part (%d)"
          % (mode, len(leaves)))
    for i, obj in enumerate(leaves):
        obj.modifiers.new("Mine %d" % i, "BEVEL")
    marked = {o.name: "Mine %d" % i for i, o in enumerate(leaves)}

    write_assembly(STEP, extras=2)      # two components added ahead of them
    bpy.ops.stepper.refresh_file(filepath=STEP)

    kept = 0
    for name, mod in marked.items():
        obj = bpy.data.objects.get(name)
        if obj is not None and mod in [md.name for md in obj.modifiers]:
            kept += 1
    check(kept == 4,
          "%s: every occurrence kept its own modifier through the renumber "
          "(%d of 4)" % (mode, kept))
    check(len(parts()) == 6, "%s: and the added components arrived (%d)"
          % (mode, len(parts())))


if FAILS:
    print("\nrefresh_keep_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nrefresh_keep_smoke: OK - a refresh keeps the size, the user's work "
      "and their placement")
