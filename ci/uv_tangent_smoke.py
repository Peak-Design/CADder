# SPDX-License-Identifier: GPL-3.0-or-later
"""Merge tangent faces: does a bent plate come out as its flat pattern?

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_tangent_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

The fixture is a sheet metal part: a 50 mm leg, a 90 degree bend of 5 mm
radius, a 55 mm leg, 200 mm wide and 2 mm thick. Its outer surface is three
CAD faces that meet smoothly, which is the case the setting exists for.
A press brake would flatten those three into one rectangle about 113 mm by
200 mm, and that is what the merged island has to measure.
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

bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import main as m

FAILS = []
# The flat pattern of one face: the profile length by the width.
PROFILE = 50.0 + 5.0 * math.pi / 2.0 + 55.0
WIDTH = 200.0
FLAT_AREA = PROFILE * WIDTH


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """A bent plate, built from its profile and then given a thickness."""
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

    r = 5.0
    p1 = gp_Pnt(0, 0, 0)
    p2 = gp_Pnt(50, 0, 0)
    mid = gp_Pnt(50 + r * math.cos(-math.pi / 4),
                 r + r * math.sin(-math.pi / 4), 0)
    p3 = gp_Pnt(50 + r, r, 0)
    p4 = gp_Pnt(50 + r, 60, 0)
    arc = GC_MakeArcOfCircle(p2, mid, p3).Value()
    wire = BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(p1, p2).Edge(),
        BRepBuilderAPI_MakeEdge(arc).Edge(),
        BRepBuilderAPI_MakeEdge(p3, p4).Edge()).Wire()
    shell = BRepPrimAPI_MakePrism(wire, gp_Vec(0, 0, WIDTH)).Shape()
    mk = BRepOffsetAPI_MakeThickSolid()
    mk.MakeThickSolidBySimple(shell, 2.0)
    mk.Build()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    lab = tool.AddShape(mk.Shape(), False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("bent_plate"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_tangent_")
STEP = os.path.join(tmp, "bent_plate.step")
write_step(STEP)


def load(**kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
    m._cache_drop(STEP)
    opts = dict(htypes="FLAT", up_as="Z", tris_to_quads=False,
                uv_normalize=False)
    opts.update(kw)
    m.load_step(bpy.context, STEP, **opts)
    bpy.context.view_layer.update()
    return [o for o in bpy.data.objects if o.type == "MESH" and o.data
            and len(o.data.polygons) and len(o.data.uv_layers)]


def islands(objs):
    """(world area in mm2, UV area in mm2) for each UV island."""
    out = []
    for o in objs:
        sc = o.matrix_world.to_scale()
        lin = (abs(sc.x) * abs(sc.y) * abs(sc.z)) ** (1.0 / 3.0)
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bm.faces.ensure_lookup_table()
        uvl = bm.loops.layers.uv.active
        seen = set()
        for face in bm.faces:
            if face.index in seen:
                continue
            stack, group = [face], [face]
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
                                group.append(other)
                                break
            a3 = sum(f.calc_area() for f in group) * lin * lin
            a2 = 0.0
            for f in group:
                q = [lp[uvl].uv for lp in f.loops]
                for k in range(1, len(q) - 1):
                    p, s, r = q[0], q[k], q[k + 1]
                    a2 += 0.5 * abs((s.x - p.x) * (r.y - p.y)
                                    - (r.x - p.x) * (s.y - p.y))
            # The meshes arrive in scene units. Report both in mm2 so the
            # numbers can be read against the fixture.
            out.append((a3 * 1e6, a2 * 1e6))
        bm.free()
    return out


# ---- None leaves both modes as they were ---------------------------------
# None must not touch the seams. Cutting every CAD face boundary here would
# undo Closed surfaces, whose whole job is to keep a hole in one island.
print("\n== None is the layout each UV mode already made")
surf_none = islands(load(uv_mode="SURFACE", uv_merge_tangent="NONE"))
unwr_none = islands(load(uv_mode="UNWRAP", uv_merge_tangent="NONE"))
unwr_all = islands(load(uv_mode="UNWRAP", uv_merge_tangent="ALL"))
check(len(unwr_none) == len(unwr_all),
      "Unwrap with None matches Unwrap with All (%d against %d islands)"
      % (len(unwr_none), len(unwr_all)))
check(not any(a3 > FLAT_AREA * 0.5 for a3, _a2 in surf_none),
      "CAD Surface with None keeps the plate in separate faces")

# ---- All gives the flat pattern ------------------------------------------
print("\n== All flattens the bent plate into one island")
surf_all = islands(load(uv_mode="SURFACE", uv_merge_tangent="ALL"))
check(len(surf_all) < len(surf_none),
      "it needs fewer islands than None (%d against %d)"
      % (len(surf_all), len(surf_none)))
big = [(a3, a2) for a3, a2 in surf_all if a3 > FLAT_AREA * 0.8]
check(len(big) == 2,
      "the two plate faces each come back as one island (%d found)"
      % len(big))
if big:
    worst = max(abs(a2 - a3) / a3 for a3, a2 in big)
    # A flat pattern holds its area. A projection or a fold would not.
    check(worst < 0.02,
          "and each holds its surface area within %.1f%%" % (100.0 * worst))

# ---- Smart keeps the plate and drops the strip ---------------------------
# The edge of the plate is one tangent run 113 mm long and 2 mm thick.
# Merging it makes an island 56 times longer than it is wide, which wastes
# the tile. Smart leaves that one in separate faces.
print("\n== Smart merges the plate but not its edge")
surf_smart = islands(load(uv_mode="SURFACE", uv_merge_tangent="SMART"))
big = [a3 for a3, _a2 in surf_smart if a3 > FLAT_AREA * 0.8]
check(len(big) == 2,
      "the plate faces are still merged (%d found)" % len(big))
check(len(surf_smart) > len(surf_all),
      "the thin edge is not (%d islands against %d for All)"
      % (len(surf_smart), len(surf_all)))
check(len(surf_smart) < len(surf_none),
      "and it still beats None (%d against %d)"
      % (len(surf_smart), len(surf_none)))

# ---- the density survives the flattening ---------------------------------
# The flattened islands are packed into 0-1 by the unwrap, which has nothing
# to do with the scale the parametric islands are at. The addon puts them
# back at the density of the rest of the part.
print("\n== one part still holds one texel density")
for label, rows in (("All", surf_all), ("Smart", surf_smart)):
    d = [math.sqrt(a2 / a3) for a3, a2 in rows if a3 > 1e-9 and a2 > 1e-12]
    spread = max(d) / min(d) if len(d) > 1 else 1.0
    check(spread < 1.15,
          "%s: %.2fx between the densest and the sparsest island"
          % (label, spread))

# ---- a refresh reproduces it ---------------------------------------------
print("\n== the setting is stamped on the object")
objs = load(uv_mode="SURFACE", uv_merge_tangent="SMART")
rec = str(objs[0].get("STEP_import_settings")
          or objs[0].get("import_record_json") or "")
check("uv_merge_tangent" in rec, "uv_merge_tangent reaches the record")

if FAILS:
    print("\nuv_tangent_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_tangent_smoke: OK - a bent plate flattens like a flat pattern")
