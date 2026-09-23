# SPDX-License-Identifier: GPL-3.0-or-later
"""Refresh from disk keeps a copy the user made of an imported part.

    blender -b --factory-startup --python-exit-code 1 -P ci/refresh_copy_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Shift+D copies the custom properties with the object, so a copy carries
the same CAD identity as the part it came from. The refresh paired the
fresh part with one of the two and found no partner for the other. It then
deleted that one as "gone from the file", with its modifiers and placement,
although the part is still in the file.

A part that really went from the file must still go, and so must one
occurrence of a part that CAD placed twice and now places once.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy
from mathutils import Vector

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, refresh as R

FAILS = []
MODES = ("FLAT", "TREE", "EMPTIES", "COLLECTION_INSTANCES")


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path, placements):
    """One assembly 'top'. `placements` is {name: [x_mm, ...]}, one
    component for each x. The same name is the same part."""
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
    for i, (name, xs) in enumerate(sorted(placements.items())):
        size = 10.0 + i
        leaf = tool.AddShape(BRepPrimAPI_MakeBox(
            gp_Pnt(0, 0, 0), size, size, size).Shape(), False)
        TDataStd_Name.Set_s(leaf, TCollection_ExtendedString(name))
        for x in xs:
            trsf = gp_Trsf()
            trsf.SetTranslation(gp_Vec(x, 0, 0))
            tool.AddComponent(top, leaf, TopLoc_Location(trsf))
    tool.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)


def carriers(name):
    """The objects that place a component of this CAD name: the instancing
    empties in COLLECTION_INSTANCES mode, the meshes otherwise."""
    objs = [o for o in R.file_objects(STEP) if o.get("STEP_name") == name]
    empties = [o for o in objs if R._is_occurrence(o)]
    return empties or [o for o in objs if o.type == "MESH"]


def duplicate(obj):
    """Shift+D on one object. Returns the copy."""
    for other in bpy.context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.duplicate()
    return bpy.context.view_layer.objects.active


tmp = tempfile.mkdtemp(prefix="stepper_refresh_copy_")
STEP = os.path.join(tmp, "copy_fixture.step")


for mode in MODES:
    print("\n== a user's copy survives a refresh: %s" % mode)
    clean()
    write_step(STEP, {"alpha": [0.0], "beta": [30.0]})
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    original = carriers("alpha")[0]
    copy = duplicate(original)
    check(copy is not original and copy.get("STEP_name") == "alpha",
          "%s: the copy carries the CAD identity of the part" % mode)
    copy.location = copy.location + Vector((0.0, 0.5, 0.0))
    copy["mine"] = 1
    if copy.type == "MESH":
        copy.modifiers.new("Mine", "BEVEL")
    copy_name, original_name = copy.name, original.name
    bpy.context.view_layer.update()
    copy_at = copy.matrix_world.translation.copy()

    bpy.ops.stepper.refresh_file(filepath=STEP)
    kept = bpy.data.objects.get(copy_name)
    check(kept is not None, "%s: the copy is still there" % mode)
    check(bpy.data.objects.get(original_name) is not None,
          "%s: and so is the part it came from" % mode)
    if kept is not None:
        check(kept.get("mine") == 1 and (kept.type != "MESH" or "Mine" in [
            md.name for md in kept.modifiers]),
              "%s: the copy keeps the user's work on it" % mode)
        bpy.context.view_layer.update()
        check((kept.matrix_world.translation - copy_at).length < 1e-6,
              "%s: and its placement" % mode)
    check(len(carriers("alpha")) == 2, "%s: two alpha objects, not three (%d)"
          % (mode, len(carriers("alpha"))))

    # A second refresh finds the copy the same way.
    bpy.ops.stepper.refresh_file(filepath=STEP)
    check(bpy.data.objects.get(copy_name) is not None,
          "%s: the copy survives a second refresh" % mode)

    # What CAD removes still goes.
    write_step(STEP, {"alpha": [0.0]})
    bpy.ops.stepper.refresh_file(filepath=STEP)
    check(not carriers("beta"), "%s: a part gone from the file goes" % mode)
    check(bpy.data.objects.get(copy_name) is not None,
          "%s: and the copy of alpha stays" % mode)


for mode in MODES:
    print("\n== one occurrence removed in CAD still goes: %s" % mode)
    clean()
    write_step(STEP, {"alpha": [0.0, 40.0, 80.0]})
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    check(len(carriers("alpha")) == 3, "%s: three occurrences (%d)"
          % (mode, len(carriers("alpha"))))
    write_step(STEP, {"alpha": [0.0, 40.0]})
    bpy.ops.stepper.refresh_file(filepath=STEP)
    check(len(carriers("alpha")) == 2,
          "%s: two occurrences after the refresh (%d)"
          % (mode, len(carriers("alpha"))))


if FAILS:
    print("\nrefresh_copy_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nrefresh_copy_smoke: OK: a refresh keeps the user's copies and "
      "removes what CAD removed")
