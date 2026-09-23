# SPDX-License-Identifier: GPL-3.0-or-later
"""Refresh from disk keeps one collection per part name and per subassembly.

    blender -b --factory-startup --python-exit-code 1 -P ci/refresh_collections_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

FLAT makes one collection per part name and TREE one per subassembly. A
refresh matched such a collection to the one already in the scene only
when there was one of its kind. With two or more, none matched, so every
refresh left an empty copy behind ('alpha.001', 'sub0.001', ...), and a
part new in the file went into that copy instead of the real collection.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, refresh as R

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path, subs=(), loose=()):
    """An assembly 'top'. `subs` is [(name, [(leaf, x_mm), ...]), ...], one
    subassembly each, placed along Y. A name used twice is the same
    subassembly placed twice. `loose` is [(leaf, x_mm), ...] straight under
    top. A leaf name used twice is the same part placed twice."""
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
    labels = {}

    def named(label, name):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        return label

    def leaf(name):
        if name not in labels:
            size = 5.0 + len(labels)
            labels[name] = named(tool.AddShape(BRepPrimAPI_MakeBox(
                gp_Pnt(0, 0, 0), size, size, size).Shape(), False), name)
        return labels[name]

    def place(parent, child, x, y):
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(x, y, 0))
        tool.AddComponent(parent, child, TopLoc_Location(trsf))

    top = named(tool.NewShape(), "top")
    for i, (name, leaves) in enumerate(subs):
        if name not in labels:
            labels[name] = named(tool.NewShape(), name)
            for leaf_name, x in leaves:
                place(labels[name], leaf(leaf_name), x, 0.0)
        place(top, labels[name], 0.0, 100.0 * (i + 1))
    for leaf_name, x in loose:
        place(top, leaf(leaf_name), x, 0.0)
    tool.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)


def owned():
    return R.file_collections(STEP)


def empty_ones():
    return sorted(c.name for c in owned() if not c.all_objects)


def home(cad_name):
    """The names of the imported collections that hold the parts of this
    CAD name."""
    return sorted({c.name for o in R.file_objects(STEP)
                   if o.get("STEP_name") == cad_name
                   for c in o.users_collection})


tmp = tempfile.mkdtemp(prefix="stepper_refresh_cols_")
STEP = os.path.join(tmp, "collections_fixture.step")


print("\n== FLAT: one collection per part name")
clean()
write_step(STEP, loose=[("alpha", 0.0), ("beta", 30.0), ("gamma", 60.0)])
m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z")
start = sorted(c.name for c in owned())
for _ in range(3):
    bpy.ops.stepper.refresh_file(filepath=STEP)
now = sorted(c.name for c in owned())
check(now == start, "three refreshes make no new collection (%s -> %s)"
      % (start, now))
check(not empty_ones(), "and leave no empty one (%s)" % empty_ones())

# A second placement of alpha is added in CAD. It belongs in alpha's
# collection, not in a copy of it.
write_step(STEP, loose=[("alpha", 0.0), ("beta", 30.0), ("gamma", 60.0),
                        ("alpha", 90.0)])
bpy.ops.stepper.refresh_file(filepath=STEP)
check(len([o for o in R.file_objects(STEP)
           if o.get("STEP_name") == "alpha"]) == 2,
      "the new placement of alpha arrived")
check(home("alpha") == ["alpha"],
      "and sits in the existing collection (%s)" % home("alpha"))
check(not empty_ones(), "no empty collection left (%s)" % empty_ones())


print("\n== TREE: one collection per subassembly")
clean()
write_step(STEP, subs=[("sub0", [("leaf0", 0.0)]),
                       ("sub1", [("leaf1", 0.0), ("leaf2", 30.0)])])
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z")
start = sorted(c.name for c in owned())
for _ in range(3):
    bpy.ops.stepper.refresh_file(filepath=STEP)
now = sorted(c.name for c in owned())
check(now == start, "three refreshes make no new collection (%s -> %s)"
      % (start, now))
check(not empty_ones(), "and leave no empty one (%s)" % empty_ones())

# A part is added to sub0 in CAD. It belongs in sub0's collection.
write_step(STEP, subs=[("sub0", [("leaf0", 0.0), ("added", 30.0)]),
                       ("sub1", [("leaf1", 0.0), ("leaf2", 30.0)])])
bpy.ops.stepper.refresh_file(filepath=STEP)
check(home("added") == home("leaf0") == ["sub0"],
      "the new part sits with its subassembly (%s, leaf0 in %s)"
      % (home("added"), home("leaf0")))
check(not empty_ones(), "no empty collection left (%s)" % empty_ones())


print("\n== TREE: one subassembly placed twice, inside another")
clean()
write_step(STEP, subs=[("sub", [("leaf", 0.0), ("leaf", 40.0)]),
                       ("sub", []),
                       ("other", [("bolt", 0.0)])])
m.load_step(bpy.context, STEP, htypes="TREE", up_as="Z")
start = sorted(c.name for c in owned())
for _ in range(3):
    bpy.ops.stepper.refresh_file(filepath=STEP)
now = sorted(c.name for c in owned())
check(now == start, "three refreshes make no new collection (%s -> %s)"
      % (start, now))
check(not empty_ones(), "and leave no empty one (%s)" % empty_ones())


if FAILS:
    print("\nrefresh_collections_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nrefresh_collections_smoke: OK: a refresh keeps one collection per "
      "part name and per subassembly")
