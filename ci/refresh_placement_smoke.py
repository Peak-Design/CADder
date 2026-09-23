# SPDX-License-Identifier: GPL-3.0-or-later
"""Refresh from disk: does a component moved in CAD move in Blender?

    blender -b --factory-startup --python-exit-code 1 -P ci/refresh_placement_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

refresh_keep_smoke.py moves a part in CAD by moving its geometry. That
leaves the component placement as it was, so it cannot see a refresh that
drops placement changes. Here the geometry stays the same and the component
placement (its TopLoc_Location in the assembly) changes, which is what CAD
writes when a user drags a component.

The refresh used to measure the user's own move against the placement of
the NEW import, because the import stamps were copied over before the move
was read. Every CAD move then cancelled itself out and the part stayed where
it was.
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
    """One assembly 'top' with one component per entry of `placements`,
    {name: (x, y, z) in mm}. Every part has its own box, so the CAD names
    tell them apart. Only the component placement changes between two
    versions of the file, never the geometry."""
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
    for i, (name, at) in enumerate(sorted(placements.items())):
        size = 10.0 + i
        leaf = tool.AddShape(BRepPrimAPI_MakeBox(
            gp_Pnt(0, 0, 0), size, size, size).Shape(), False)
        TDataStd_Name.Set_s(leaf, TCollection_ExtendedString(name))
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(*at))
        tool.AddComponent(top, leaf, TopLoc_Location(trsf))
    tool.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)


def carrier(name):
    """The object that holds the placement of one component. In
    COLLECTION_INSTANCES mode that is the instancing empty, because the
    prototype mesh sits at the origin of a hidden collection."""
    objs = [o for o in R.file_objects(STEP) if o.get("STEP_name") == name]
    for obj in objs:
        if R._is_occurrence(obj):
            return obj
    for obj in objs:
        if obj.type == "MESH":
            return obj
    return None


def where(name):
    bpy.context.view_layer.update()
    obj = carrier(name)
    return None if obj is None else obj.matrix_world.translation.copy()


def near(a, b, tol=1e-6):
    return a is not None and b is not None and (a - b).length < tol


tmp = tempfile.mkdtemp(prefix="stepper_placement_")
STEP = os.path.join(tmp, "placement_fixture.step")

BEFORE = {"alpha": (0.0, 0.0, 0.0), "beta": (30.0, 0.0, 0.0),
          "gamma": (60.0, 0.0, 0.0)}
# alpha moves 50 mm in Y and beta 50 mm in X. gamma does not move.
AFTER = {"alpha": (0.0, 50.0, 0.0), "beta": (80.0, 0.0, 0.0),
         "gamma": (60.0, 0.0, 0.0)}
USER_MOVE = Vector((0.0, 0.0, 1.0))
fresh_after = {}

# What says a stamp is where the import put the object. Stamps from older
# versions do not have it.
LEGACY_MARK = getattr(R, "BASIS_VERSION_PROP", "STEP_import_basis_version")


for mode in MODES:
    print("\n== a CAD placement change comes through: %s" % mode)

    # Where a fresh import of the changed file puts each part. The refresh
    # has to agree with it, apart from the user's own move.
    clean()
    write_step(STEP, AFTER)
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    fresh = {name: where(name) for name in AFTER}
    fresh_after[mode] = fresh
    check(all(v is not None for v in fresh.values()),
          "%s: every part found in a fresh import" % mode)
    check(near(fresh["beta"], Vector((0.08, 0.0, 0.0))),
          "%s: the fixture puts beta at 80 mm (%s)" % (mode, fresh["beta"]))

    clean()
    write_step(STEP, BEFORE)
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    user = carrier("alpha")
    user.location = user.location + USER_MOVE

    write_step(STEP, AFTER)
    bpy.ops.stepper.refresh_file(filepath=STEP)

    got = {name: where(name) for name in AFTER}
    check(near(got["beta"], fresh["beta"]),
          "%s: a part moved in CAD moves (%s, expected %s)"
          % (mode, got["beta"], fresh["beta"]))
    check(near(got["gamma"], fresh["gamma"]),
          "%s: a part CAD did not move stays (%s, expected %s)"
          % (mode, got["gamma"], fresh["gamma"]))
    check(near(got["alpha"], fresh["alpha"] + USER_MOVE),
          "%s: a part moved in CAD and by the user gets both (%s, "
          "expected %s)" % (mode, got["alpha"], fresh["alpha"] + USER_MOVE))

    # The next refresh of the same file changes nothing, so neither move
    # is applied twice.
    for _ in range(2):
        bpy.ops.stepper.refresh_file(filepath=STEP)
    again = {name: where(name) for name in AFTER}
    check(all(near(again[n], got[n]) for n in AFTER),
          "%s: a second refresh moves nothing (%s)" % (mode, again))


for mode in MODES:
    print("\n== a stamp an older version left behind: %s" % mode)

    # Older versions wrote the stamp again at the end of every refresh, from
    # where the object ended up. After such a refresh the stamp holds the
    # user's move as well, so it cannot tell the two apart. The first
    # refresh with such a stamp has to keep the part where it is, not snap
    # it back to the CAD placement.
    clean()
    write_step(STEP, BEFORE)
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    user = carrier("alpha")
    user.location = user.location + USER_MOVE
    bpy.context.view_layer.update()
    for obj in R.file_objects(STEP):
        obj[R.BASIS_PROP] = [v for row in obj.matrix_basis for v in row]
        if LEGACY_MARK in obj:
            del obj[LEGACY_MARK]
    at = where("alpha")

    bpy.ops.stepper.refresh_file(filepath=STEP)
    check(near(where("alpha"), at),
          "%s: the part stays where the user put it (%s, was %s)"
          % (mode, where("alpha"), at))

    # From then on the stamp is right: the next CAD move comes through and
    # the user's move rides on top.
    write_step(STEP, AFTER)
    bpy.ops.stepper.refresh_file(filepath=STEP)
    check(near(where("beta"), fresh_after[mode]["beta"]),
          "%s: the next CAD move comes through (%s)" % (mode, where("beta")))
    check(near(where("alpha"), fresh_after[mode]["alpha"] + USER_MOVE),
          "%s: with the user's move on top (%s)" % (mode, where("alpha")))


if FAILS:
    print("\nrefresh_placement_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nrefresh_placement_smoke: OK - a CAD placement change comes through "
      "a refresh, with the user's own move on top")
