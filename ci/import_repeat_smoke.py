# SPDX-License-Identifier: GPL-3.0-or-later
"""The same file imported again: same place, same report, fresh content.

    blender -b --factory-startup --python-exit-code 1 -P ci/import_repeat_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

A refresh runs the import again with the settings it recorded. The import
put every part at the 3D cursor, and the cursor was not in the record, so
a refresh after a click in the viewport put the parts where the cursor was
NOW. A refresh reads a part it did not put back where it was as a move in
CAD.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
STEP = os.path.join(_HERE, "fixtures", "multisolid.step")

import bpy
from mathutils import Vector

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m
from CADder import refresh as refresh_mod

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def imported(**kwargs):
    """The mesh objects one import makes."""
    before = set(bpy.data.objects)
    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, **kwargs)
    return [o for o in bpy.data.objects
            if o not in before and o.type == "MESH"]


def low_corner(objs):
    pts = [o.matrix_world @ v.co for o in objs for v in o.data.vertices]
    return Vector([min(p[i] for p in pts) for i in range(3)])


# ---- a refresh puts the parts where the import did -----------------------
print("\n== the cursor of the import")
scene = bpy.context.scene
scene.cursor.location = (0.0, 0.0, 0.0)
first = imported(htypes="FLAT", up_as="Z")
at_first = low_corner(first)

scene.cursor.location = (5.0, 0.0, 0.0)
kwargs, _record, source = refresh_mod.import_settings(scene, STEP)
check(source is not None, "the import was recorded (%s)" % source)
again = imported(**kwargs)
shift = (low_corner(again) - at_first).length
check(shift < 1e-6, "a refresh puts the parts where the import did "
      "(moved %.4f)" % shift)

fresh = imported(htypes="FLAT", up_as="Z")
shift = (low_corner(fresh) - at_first - Vector((5.0, 0.0, 0.0))).length
check(shift < 1e-6, "a new import still lands at the cursor (off by %.4f)"
      % shift)

empty = bpy.data.objects.new("probe", None)
scene.collection.objects.link(empty)
m.transform_to_up("Z", [empty], 1.0, to_cursor=False)
check(empty.matrix_world.translation.length < 1e-9,
      "to_cursor=False leaves the cursor out (%s)"
      % (tuple(empty.matrix_world.translation),))

# ---- a second import reports the same problems, once ---------------------
# The lists of failed and recovered parts live on the reader, and the
# cache hands the same reader to the next import of the file. Nothing
# emptied them, so each import of an unchanged file listed every failed
# part once more.
print("\n== the report of a cached import")


def write_step(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.gp import gp_Pnt
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for name, shape in (
            ("block", BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape()),
            ("sketch", BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0),
                                               gp_Pnt(10, 0, 0)).Edge())):
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


import shutil
import tempfile

TMP = tempfile.mkdtemp(prefix="cadder_import_repeat_")
EMPTY = os.path.join(TMP, "with_sketch.step")
write_step(EMPTY)
m._cache_drop(EMPTY)
failed_1, recovered_1 = m.load_step(bpy.context, EMPTY, htypes="FLAT",
                                    up_as="Z")
failed_1, recovered_1 = list(failed_1), list(recovered_1)
problems_1 = dict(m._cache_get(EMPTY).import_problems)
check(len(failed_1) == 1, "the sketch part has no geometry (%s)" % failed_1)
failed_2, recovered_2 = m.load_step(bpy.context, EMPTY, htypes="FLAT",
                                    up_as="Z")
check(m._cache_get(EMPTY) is not None, "the second import used the cache")
check(list(failed_2) == failed_1,
      "the second import reports it once (%s)" % list(failed_2))
check(list(recovered_2) == recovered_1, "and the same recovered parts")
problems_2 = dict(m._cache_get(EMPTY).import_problems)
check(problems_2 == problems_1,
      "and the same problem counts (%s, then %s)" % (problems_1, problems_2))

if FAILS:
    print("\nimport_repeat_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nimport_repeat_smoke: OK")
