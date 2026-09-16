# SPDX-License-Identifier: GPL-3.0-or-later
"""The UV panel: can one part get a different UV map from its neighbor?

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_reapply_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

The point of the panel is a mix of techniques in one scene: a bent plate as
one flat pattern, the part beside it face by face. So every check here works
on two identical parts and changes only one of them.
"""
import math
import os
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bmesh
import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """Two identical bent plates, so one can be changed and one compared."""
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge,
                                    BRepBuilderAPI_MakeWire)
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    r = 5.0
    for i in range(2):
        y = i * 300.0
        p1 = gp_Pnt(0, y, 0)
        p2 = gp_Pnt(50, y, 0)
        mid = gp_Pnt(50 + r * math.cos(-math.pi / 4), y,
                     r + r * math.sin(-math.pi / 4))
        p3 = gp_Pnt(50 + r, y, r)
        p4 = gp_Pnt(50 + r, y, 60)
        arc = GC_MakeArcOfCircle(p2, mid, p3).Value()
        wire = BRepBuilderAPI_MakeWire(
            BRepBuilderAPI_MakeEdge(p1, p2).Edge(),
            BRepBuilderAPI_MakeEdge(arc).Edge(),
            BRepBuilderAPI_MakeEdge(p3, p4).Edge()).Wire()
        shell = BRepPrimAPI_MakePrism(wire, gp_Vec(0, 200, 0)).Shape()
        mk = BRepOffsetAPI_MakeThickSolid()
        mk.MakeThickSolidBySimple(shell, 2.0)
        mk.Build()
        lab = tool.AddShape(mk.Shape(), False)
        TDataStd_Name.Set_s(lab,
                            TCollection_ExtendedString("plate_%d" % i))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_reapply_")
STEP = os.path.join(tmp, "plates.step")
write_step(STEP)


def load(**kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)
    opts = dict(htypes="FLAT", up_as="Z", tris_to_quads=False,
                uv_mode="SURFACE", uv_normalize=False)
    opts.update(kw)
    m.load_step(bpy.context, STEP, **opts)
    bpy.context.view_layer.update()
    objs = [o for o in bpy.data.objects if o.type == "MESH" and o.data
            and len(o.data.polygons) and len(o.data.uv_layers)]
    return sorted(objs, key=lambda o: o.name)


def uv_of(o):
    a = np.empty(len(o.data.loops) * 2, dtype=np.float64)
    o.data.uv_layers.active.uv.foreach_get("vector", a)
    return a


def n_islands(o):
    bm = bmesh.new()
    bm.from_mesh(o.data)
    bm.faces.ensure_lookup_table()
    uvl = bm.loops.layers.uv.active
    seen = set()
    n = 0
    for face in bm.faces:
        if face.index in seen:
            continue
        n += 1
        stack = [face]
        seen.add(face.index)
        while stack:
            f = stack.pop()
            for loop in f.loops:
                a = loop[uvl].uv
                b = loop.link_loop_next[uvl].uv
                for other in loop.edge.link_faces:
                    if other.index in seen:
                        continue
                    for ol in other.loops:
                        oa, ob = ol[uvl].uv, ol.link_loop_next[uvl].uv
                        if (((oa - a).length < 1e-6
                             and (ob - b).length < 1e-6)
                                or ((oa - b).length < 1e-6
                                    and (ob - a).length < 1e-6)):
                            seen.add(other.index)
                            stack.append(other)
                            break
    bm.free()
    return n


def apply_to(obj, **settings):
    prg = bpy.context.scene.stepper
    for key, value in settings.items():
        setattr(prg, key, value)
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.stepper.reapply_uv()


# ---- one part changes, the other does not --------------------------------
print("\n== the panel works on the selection alone")
a, b = load()[:2]
before_a, before_b = n_islands(a), n_islands(b)
keep_b = uv_of(b)
check(before_a == before_b,
      "the two plates start alike (%d islands each)" % before_a)
apply_to(a, uv_mode="SMART", uv_pack="NONE")
after_a = n_islands(a)
check(after_a < before_a,
      "the selected plate is now one flat pattern (%d islands against %d)"
      % (after_a, before_a))
check(np.array_equal(uv_of(b), keep_b),
      "the plate that was not selected keeps its UVs exactly")

# ---- it matches what an import with the same settings makes --------------
print("\n== the result matches the import that would have made it")
direct = load(uv_mode="SMART")[0]
check(n_islands(direct) == after_a,
      "the panel and the import agree (%d islands against %d)"
      % (n_islands(direct), after_a))

# ---- box projection leaves the mesh alone --------------------------------
# Box projection reads the mesh and nothing else, so it has no reason to go
# back to the CAD file or to replace anything.
print("\n== Box Project keeps the mesh it is given")
a, b = load()[:2]
verts_before = np.empty(len(a.data.vertices) * 3, dtype=np.float64)
a.data.vertices.foreach_get("co", verts_before)
uv_before = uv_of(a)
apply_to(a, uv_mode="BOX", box_uv_scale=0.05, uv_pack="NONE")
verts_after = np.empty(len(a.data.vertices) * 3, dtype=np.float64)
a.data.vertices.foreach_get("co", verts_after)
check(np.array_equal(verts_before, verts_after),
      "not one vertex moved")
check(not np.array_equal(uv_before, uv_of(a)), "and the UVs did change")

# ---- the settings stay on the object -------------------------------------
# A Regenerate or a Refresh later has to make the UV map the user chose
# here, not the one the import made.
print("\n== the choice is recorded on the object")
a, b = load()[:2]
apply_to(a, uv_mode="ANGLE_BASED", uv_pack="NONE")
rec = str(a.get("STEP_import_settings", ""))
check('"uv_mode": "ANGLE_BASED"' in rec,
      "the record carries the mode")
rec_b = str(b.get("STEP_import_settings", ""))
check('"uv_mode": "SURFACE"' in rec_b,
      "and the record of the other plate is untouched")

# ---- Regenerate repeats the import ---------------------------------------
# Regenerate builds the mesh again from the CAD data, and then has to run
# the passes the import ran after it: the quads and the unwrap, with the
# method the part was imported with. It once left the triangles alone and
# always unwrapped with Conformal. A plate unwraps the same with every
# method, so the check reads the method Regenerate asks for.
print("\n== Regenerate makes the map the import made")
a = load(uv_mode="ANGLE_BASED", tris_to_quads=True)[0]
faces = len(a.data.polygons)
# The load above enabled the addon again, which loads the module again, so
# the wrapper goes on the module that is live now.
live = sys.modules["CADder.main"]
asked = []
real_unwrap = live._unwrap_uv_objects


def spy(objs, world_scale=None, method="CONFORMAL"):
    asked.append(method)
    return real_unwrap(objs, world_scale=world_scale, method=method)


live._unwrap_uv_objects = spy
for o in bpy.context.view_layer.objects:
    o.select_set(o == a)
bpy.context.view_layer.objects.active = a
bpy.ops.stepper.regenerate(use_scene_settings=False)
live._unwrap_uv_objects = real_unwrap
check(len(a.data.polygons) == faces,
      "the quads come back (%d faces against %d)"
      % (len(a.data.polygons), faces))
check(asked == ["ANGLE_BASED"],
      "and the unwrap uses the method of the import (%s)" % asked)

if FAILS:
    print("\nuv_reapply_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_reapply_smoke: OK - one part can get its own UV treatment")
