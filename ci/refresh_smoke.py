# SPDX-License-Identifier: GPL-3.0-or-later
"""Refresh-from-disk: does it bring the file back and leave the user's work
alone?

    blender -b --factory-startup -P ci/refresh_smoke.py

Two halves.

  1. The arrangement. Import, put the family in a collection of your own,
     ctrl-drag one object into a second collection so it is linked in both,
     hang an object of your own off a part, hide another part. Refresh. All
     four have to survive.

  2. The file changing underneath. The fixture is written with three bodies,
     imported, then REWRITTEN with two, and refreshed. The refresh has to
     notice the file changed on disk, and report the component that has gone
     rather than quietly leaving a stale object behind.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON_DIR = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ADDON_DIR)

import bpy
bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import main as m, refresh as R

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path, n_bodies):
    """A STEP holding n disjoint bodies in ONE product. See
    ci/fixtures/make_multisolid.py for why AddShape(..., False) matters."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    from OCP.gp import gp_Pnt
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    for i in range(n_bodies):
        builder.Add(comp, BRepPrimAPI_MakeBox(
            gp_Pnt(i * 30.0, 0, 0), 10, 10, 10).Shape())

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    label = tool.AddShape(comp, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString("multibody part"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def clean_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for col in list(bpy.data.collections):
        bpy.data.collections.remove(col)
    for me in list(bpy.data.meshes):
        bpy.data.meshes.remove(me)
    vl = bpy.context.view_layer
    vl.active_layer_collection = vl.layer_collection
    bpy.context.scene[R.REGISTRY_PROP] = ""


def cols_of(obj):
    return sorted(c.name for c in obj.users_collection)


def under(col, obj, seen=None):
    seen = seen if seen is not None else set()
    if col.name in seen:
        return False
    seen.add(col.name)
    if obj.name in col.objects:
        return True
    return any(under(c, obj, seen) for c in col.children)


tmp = tempfile.mkdtemp(prefix="stepper_refresh_")
STEP = os.path.join(tmp, "refresh_fixture.step")

# ── 1. the arrangement survives ─────────────────────────────────────────
write_step(STEP, 3)
for htypes, grouped in (("EMPTIES", False), ("TREE", True)):
    clean_scene()
    scene = bpy.context.scene.collection
    m.load_step(bpy.context, STEP, htypes=htypes, up_as="Z",
                apply_scale=True, separate_solids=True,
                group_in_collection=grouped)
    print("\n== arrangement: %s grouped=%s" % (htypes, grouped))

    check(R.settings_for(bpy.context.scene, STEP) is not None,
          "the import was recorded")
    check(STEP in [r["path"] for r in R.imported_files()],
          "the file is listed as imported")

    mine = [o for o in bpy.data.objects
            if o.get("STEP_file") == STEP and o.type == "MESH"]
    check(len(mine) == 3, "three bodies imported (%d)" % len(mine))

    home = bpy.data.collections.new("STEP")
    scene.children.link(home)
    extra = bpy.data.collections.new("Collection A")
    scene.children.link(extra)

    roots = R.owned_roots(STEP)
    if roots:
        for col in roots:
            for sc in bpy.data.scenes:
                if col.name in [c.name for c in sc.collection.children]:
                    sc.collection.children.unlink(col)
            home.children.link(col)
    else:
        for obj in mine:
            for col in list(obj.users_collection):
                col.objects.unlink(obj)
            home.objects.link(obj)

    star = sorted(mine, key=lambda o: o.name)[0]
    star_name = star.name
    extra.objects.link(star)

    prop = bpy.data.objects.new("user light stand", None)
    scene.objects.link(prop)
    prop.parent = star

    hidden = sorted(mine, key=lambda o: o.name)[-1]
    hidden_name = hidden.name
    hidden.hide_viewport = True
    hidden.hide_render = True

    # A collection of someone ELSE'S nested inside the import's own, which is what
    # a rig collection is, since it is built where the assembly it drives
    # lives. Removing the import unlinks it from its only home, and a
    # collection linked nowhere is out of the scene for good.
    nested = None
    inner = R.owned_roots(STEP)
    if inner:
        nested = bpy.data.collections.new("Rig-like")
        inner[0].children.link(nested)
        nested_owner_role = inner[0].get("STEP_role") or inner[0].name

    n_before = len(mine)
    verts_before = sum(len(o.data.vertices) for o in mine)

    check(bpy.ops.stepper.refresh_file(filepath=STEP) == {"FINISHED"},
          "the refresh completed")

    after = [o for o in bpy.data.objects
             if o.get("STEP_file") == STEP and o.type == "MESH"]
    check(len(after) == n_before, "same object count (%d)" % len(after))
    check(sum(len(o.data.vertices) for o in after) == verts_before,
          "same geometry")

    new_star = bpy.data.objects.get(star_name)
    check(new_star is not None, "the ctrl-dragged object came back")
    if new_star is not None:
        check("Collection A" in cols_of(new_star),
              "still linked in Collection A (%s)" % cols_of(new_star))
        check(under(home, new_star), "still reachable under STEP")
        check(len(cols_of(new_star)) >= 2, "linked in two places, not moved")

    if roots:
        now = R.owned_roots(STEP)
        check(now and all(c.name in [x.name for x in home.children]
                          for c in now),
              "the import root is back inside STEP")
        check(not [c for c in scene.children if c.get("STEP_file") == STEP],
              "and not at the scene root as well")

    if nested is not None:
        try:
            alive = nested.name in bpy.data.collections
        except ReferenceError:
            alive = False
        check(alive, "the nested collection survived the refresh")
        if alive:
            owner = [c for c in R.file_collections(STEP)
                     if (c.get("STEP_role") or c.name) == nested_owner_role]
            check(owner and nested.name in [c.name for c in owner[0].children],
                  "and is back inside the import, not dumped at the root")

    new_prop = bpy.data.objects.get("user light stand")
    check(new_prop is not None and new_prop.parent is not None
          and new_prop.parent.name == star_name,
          "the user's own object is still parented to its part")

    new_hidden = bpy.data.objects.get(hidden_name)
    check(new_hidden is not None and new_hidden.hide_viewport
          and new_hidden.hide_render, "the hidden part is still hidden")
    check(not [o for o in after if not o.users_collection],
          "no object left unlinked")

# ── 2. the file changes underneath ──────────────────────────────────────
clean_scene()
write_step(STEP, 3)
m.load_step(bpy.context, STEP, htypes="EMPTIES", up_as="Z",
            apply_scale=True, separate_solids=True)
print("\n== the file changes on disk")
check(R.changed_on_disk(bpy.context.scene, STEP) is False,
      "not reported as changed straight after the import")

# A component is deleted in CAD and the file written again.
import time
time.sleep(1.1)                     # filesystem timestamps are coarse
write_step(STEP, 2)
check(R.changed_on_disk(bpy.context.scene, STEP) is True,
      "the panel would now say the file changed")

before = len([o for o in bpy.data.objects
              if o.get("STEP_file") == STEP and o.type == "MESH"])
check(bpy.ops.stepper.refresh_file(filepath=STEP) == {"FINISHED"},
      "the refresh completed")
after = len([o for o in bpy.data.objects
             if o.get("STEP_file") == STEP and o.type == "MESH"])
check(before == 3 and after == 2,
      "the deleted body is gone, not left stale (%d -> %d)" % (before, after))
check(R.changed_on_disk(bpy.context.scene, STEP) is False,
      "and the file reads as up to date again")

if FAILS:
    print("\nrefresh_smoke: FAILED\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nrefresh_smoke: OK - arrangement survives a refresh, and a file that "
      "changed on disk is noticed and applied")
