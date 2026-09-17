# SPDX-License-Identifier: GPL-3.0-or-later
"""CAD Surfaces (Smart): does a bent plate come out as its flat pattern?

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_tangent_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

The fixture is a sheet metal part: a 50 mm leg, a 90 degree bend of 5 mm
radius, a 55 mm leg, 200 mm wide and 2 mm thick. Its outer surface is three
CAD faces that meet smoothly, which is the case the setting exists for.
A press brake would flatten those three into one rectangle about 113 mm by
200 mm, and that is what the merged island has to measure.

The plate has holes on purpose. A sheet with a hole has the same topology
as a tube, and the Closed surfaces test once took it for one and cut the
bend lines. A plate without holes could not show that.
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


def write_step(path, width=WIDTH):
    """A bent plate, built from its profile and then given a thickness."""
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge,
                                    BRepBuilderAPI_MakeWire)
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.BRepPrimAPI import (BRepPrimAPI_MakePrism,
                                 BRepPrimAPI_MakeCylinder)
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
    from OCP.gp import gp_Pnt, gp_Vec, gp_Ax2, gp_Dir
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
    shell = BRepPrimAPI_MakePrism(wire, gp_Vec(0, 0, width)).Shape()
    mk = BRepOffsetAPI_MakeThickSolid()
    mk.MakeThickSolidBySimple(shell, 2.0)
    mk.Build()
    part = mk.Shape()
    # Two holes through the first leg and one through the second.
    for x, z in ((25.0, width * 0.25), (25.0, width * 0.75)):
        hole = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x, -10.0, z), gp_Dir(0, 1, 0)), 4.0, 20.0).Shape()
        part = BRepAlgoAPI_Cut(part, hole).Shape()
    hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(45.0, 35.0, width * 0.5), gp_Dir(1, 0, 0)), 4.0,
        20.0).Shape()
    part = BRepAlgoAPI_Cut(part, hole).Shape()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    lab = tool.AddShape(part, False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("bent_plate"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_tangent_")
STEP = os.path.join(tmp, "bent_plate.step")
write_step(STEP)


def write_pipe(path):
    """A pipe with a hole through it, every closed face split in two.

    SOLIDWORKS writes a cylinder as two half cylinders, so the outside of a
    pipe is two faces that meet smoothly: one region, with a loop at each
    end and one round each hole. That is the topology of a plate with holes.
    But the ends of the pipe go all the way round, and it cannot be
    flattened until one of the joins between its halves is cut. A real
    7.9 m pipe with four holes folded until the test looked at that.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideClosed
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    axis = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    pipe = BRepAlgoAPI_Cut(
        BRepPrimAPI_MakeCylinder(axis, 30.0, 200.0).Shape(),
        BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), 25.0, 202.0).Shape()
    ).Shape()
    hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, -40.0, 100.0), gp_Dir(0, 1, 0)), 5.0, 80.0).Shape()
    pipe = BRepAlgoAPI_Cut(pipe, hole).Shape()
    div = ShapeUpgrade_ShapeDivideClosed(pipe)
    div.SetNbSplitPoints(1)
    div.Perform()
    pipe = div.Result()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    lab = tool.AddShape(pipe, False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("pipe"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def write_block(path):
    """A block with every edge rounded: all tangent, and not developable.

    The faces join smoothly all the way round, so All makes the whole block
    one region, and a closed surface flattened in one piece lands on
    itself. Smart has to stop before that.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Pnt
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 100, 60, 40).Shape()
    fil = BRepFilletAPI_MakeFillet(box)
    ex = TopExp_Explorer(box, TopAbs_EDGE)
    while ex.More():
        fil.Add(8.0, TopoDS.Edge_s(ex.Current()))
        ex.Next()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    lab = tool.AddShape(fil.Shape(), False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("rounded_block"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def write_turned(path, kind):
    """Turned and cast parts that only join when a face is bent.

    "cylinder": a cylinder 60 mm across with both rims rounded. A torus
    chart is only close to its true shape, so a rounded rim misses the
    straight edge of the rolled out wall by a few percent, and a cap is a
    disk that meets that edge along a circle. Neither joins by a turn.

    "tube": the same with a 30 mm bore that has sharp edges. The ends are
    rings, and a ring unrolls into a strip.

    "casting": a plate with every edge rounded, a boss rounded at its root
    and its rim, and a sharp bore through both.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Circle
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    def rounded(shape, radius, circle=None):
        fil = BRepFilletAPI_MakeFillet(shape)
        ex = TopExp_Explorer(shape, TopAbs_EDGE)
        while ex.More():
            edge = TopoDS.Edge_s(ex.Current())
            curve = BRepAdaptor_Curve(edge)
            if circle is None or (
                    curve.GetType() == GeomAbs_Circle
                    and abs(curve.Circle().Radius() - circle) < 1e-6):
                fil.Add(radius, edge)
            ex.Next()
        return fil.Shape()

    def cylinder(r, z0, h):
        return BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0, 0, z0), gp_Dir(0, 0, 1)), r, h).Shape()

    if kind == "casting":
        plate = rounded(
            BRepPrimAPI_MakeBox(gp_Pnt(-60, -40, 0), 120, 80, 15).Shape(), 4.0)
        body = rounded(BRepAlgoAPI_Fuse(plate, cylinder(20, 10, 35)).Shape(),
                       5.0, circle=20)
        shape = BRepAlgoAPI_Cut(body, cylinder(10, -5, 60)).Shape()
    else:
        shape = cylinder(30, 0, 80)
        if kind == "tube":
            shape = BRepAlgoAPI_Cut(shape, cylinder(15, -1, 82)).Shape()
        shape = rounded(shape, 5.0, circle=30)
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    lab = tool.AddShape(shape, False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString(kind))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


def write_bracket(path):
    """An L shaped block with a hole through it: every edge is sharp.

    Smart joins only smooth edges unless Join sharp edges is on, so this
    part is the same with Smart as without, until that option is on.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    foot = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 80, 40, 15).Shape()
    wall = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 15, 40, 60).Shape()
    shape = BRepAlgoAPI_Fuse(foot, wall).Shape()
    hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(50, 20, -1), gp_Dir(0, 0, 1)), 8, 20).Shape()
    shape = BRepAlgoAPI_Cut(shape, hole).Shape()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    lab = tool.AddShape(shape, False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("bracket"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


STAIR_STEPS = 7
STAIR_RUN = 80.0
STAIR_WIDTH = 50.0
STAIR_PROFILE = STAIR_STEPS * STAIR_RUN + (STAIR_STEPS - 1) * 5.0 * math.pi / 2


def write_stair(path):
    """A sheet bent into a staircase, next to a large plate.

    The flat pattern of the stair is about 607 mm by 50 mm, longer than the
    square the stair would pack into on its own, while each of its faces is
    only 80 mm. So in a tile of its own the pattern has to be cut, and in a
    tile shared with the plate there is room for it whole.
    """
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge,
                                    BRepBuilderAPI_MakeWire)
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeBox
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    r = 5.0
    s2 = math.sqrt(0.5)
    mk = BRepBuilderAPI_MakeWire()
    x = y = 0.0
    for i in range(STAIR_STEPS):
        along_x = i % 2 == 0
        end = (x + STAIR_RUN, y) if along_x else (x, y + STAIR_RUN)
        mk.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(x, y, 0),
                                       gp_Pnt(end[0], end[1], 0)).Edge())
        x, y = end
        if i == STAIR_STEPS - 1:
            break
        if along_x:
            # Left turn, from +x to +y, round a center above the path.
            cx, cy = x, y + r
            mid = (cx + r * s2, cy - r * s2)
            nxt = (cx + r, cy)
        else:
            # Right turn, from +y to +x, round a center to the right.
            cx, cy = x + r, y
            mid = (cx - r * s2, cy + r * s2)
            nxt = (cx, cy + r)
        arc = GC_MakeArcOfCircle(gp_Pnt(x, y, 0), gp_Pnt(mid[0], mid[1], 0),
                                 gp_Pnt(nxt[0], nxt[1], 0)).Value()
        mk.Add(BRepBuilderAPI_MakeEdge(arc).Edge())
        x, y = nxt
    shell = BRepPrimAPI_MakePrism(mk.Wire(),
                                  gp_Vec(0, 0, STAIR_WIDTH)).Shape()
    thick = BRepOffsetAPI_MakeThickSolid()
    thick.MakeThickSolidBySimple(shell, 2.0)
    thick.Build()
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-1500, 0, 0), 1000, 1000, 10).Shape()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for shape, name in ((thick.Shape(), "stair"), (plate, "big_plate")):
        lab = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(name))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


LONG_WIDTH = 1500.0
LONG_STEP = os.path.join(tmp, "long_plate.step")
write_step(LONG_STEP, LONG_WIDTH)
PIPE_STEP = os.path.join(tmp, "pipe.step")
write_pipe(PIPE_STEP)
BLOCK_STEP = os.path.join(tmp, "rounded_block.step")
write_block(BLOCK_STEP)
STAIR_STEP = os.path.join(tmp, "stair.step")
write_stair(STAIR_STEP)
BRACKET_STEP = os.path.join(tmp, "bracket.step")
write_bracket(BRACKET_STEP)
TURNED = {}
for _kind in ("cylinder", "tube", "casting"):
    TURNED[_kind] = os.path.join(tmp, _kind + ".step")
    write_turned(TURNED[_kind], _kind)


def overlap_share(tris, grid=256):
    """Share of an island's UV area that two of its triangles both cover."""
    if len(tris) < 2:
        return 0.0
    lo = tris.reshape(-1, 2).min(axis=0)
    span = max(float((tris.reshape(-1, 2).max(axis=0) - lo).max()), 1e-12)
    g = (tris - lo) / (span / grid)
    count = np.zeros((grid + 2, grid + 2), dtype=np.int32)
    for tri in g:
        a = np.clip(np.floor(tri.min(axis=0)).astype(int), 0, grid + 1)
        b = np.clip(np.ceil(tri.max(axis=0)).astype(int) + 1, 0, grid + 2)
        if b[0] <= a[0] or b[1] <= a[1]:
            continue
        gx, gy = np.meshgrid(np.arange(a[0], b[0]) + 0.5,
                             np.arange(a[1], b[1]) + 0.5)
        (ax, ay), (bx, by), (cx, cy) = tri
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < 1e-12:
            continue
        l1 = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / det
        l2 = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / det
        count[a[1]:b[1], a[0]:b[0]] += (l1 > 0) & (l2 > 0) & (l1 + l2 < 1)
    return float((count > 1).sum()) / max(int((count > 0).sum()), 1)


def load(step=None, **kw):
    step = step or STEP
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(step)
    opts = dict(htypes="FLAT", up_as="Z", tris_to_quads=False,
                uv_normalize=False)
    opts.update(kw)
    opts.pop("_only", None)
    m.load_step(bpy.context, step, **opts)
    bpy.context.view_layer.update()
    return [o for o in bpy.data.objects if o.type == "MESH" and o.data
            and len(o.data.polygons) and len(o.data.uv_layers)
            and o.name.startswith(kw.get("_only", ""))]


def islands(objs, want_tris=False):
    """(world area in mm2, UV area in mm2) for each UV island.

    With want_tris, each row also carries the island's UV triangles.
    """
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
            tris = []
            for f in group:
                q = [lp[uvl].uv for lp in f.loops]
                for k in range(1, len(q) - 1):
                    p, s, r = q[0], q[k], q[k + 1]
                    a2 += 0.5 * abs((s.x - p.x) * (r.y - p.y)
                                    - (r.x - p.x) * (s.y - p.y))
                    tris.append(((p.x, p.y), (s.x, s.y), (r.x, r.y)))
            # The meshes arrive in scene units. Report both in mm2 so the
            # numbers can be read against the fixture.
            row = (a3 * 1e6, a2 * 1e6)
            if want_tris:
                row += (np.array(tris, dtype=np.float64),)
            out.append(row)
        bm.free()
    return out


# ---- CAD Surfaces keeps the faces apart ----------------------------------
print("\n== CAD Surfaces is one island for each face")
surf_none = islands(load(uv_mode="SURFACE"))
check(not any(a3 > FLAT_AREA * 0.5 for a3, _a2 in surf_none),
      "the plate stays in separate faces")

# ---- Smart gives the flat pattern ----------------------------------------
# This runs with the default Closed surfaces setting, Single seam, which is
# the setting that once cut the bend lines of a plate with holes. Nothing on
# this plate overlaps and everything fits its tile, so Smart has no reason
# to cut anything.
print("\n== Smart flattens the bent plate into one island, holes and all")
surf_smart = islands(load(uv_mode="SMART"))
check(len(surf_smart) < len(surf_none),
      "it needs fewer islands than CAD Surfaces (%d against %d)"
      % (len(surf_smart), len(surf_none)))
big = [(a3, a2) for a3, a2 in surf_smart if a3 > FLAT_AREA * 0.8]
check(len(big) == 2,
      "the two plate faces each come back as one island (%d found)"
      % len(big))
if big:
    worst = max(abs(a2 - a3) / a3 for a3, a2 in big)
    # A flat pattern holds its area. A projection or a fold would not.
    check(worst < 0.02,
          "and each holds its surface area within %.1f%%" % (100.0 * worst))

# ---- the density survives the join ---------------------------------------
print("\n== one part still holds one texel density")
d = [math.sqrt(a2 / a3) for a3, a2 in surf_smart if a3 > 1e-9 and a2 > 1e-12]
spread = max(d) / min(d) if len(d) > 1 else 1.0
check(spread < 1.15,
      "%.2fx between the densest and the sparsest island" % spread)

# ---- Smart keeps a long flat pattern --------------------------------------
# A plate 1500 mm wide flattens to about 113 by 1500 mm. Each leg is one CAD
# face 1500 mm long, and a CAD face is never split, so the tile can never be
# smaller than that. The whole pattern then fits it.
print("\n== Smart keeps a long flat pattern whole")
long_area = PROFILE * LONG_WIDTH
rows = islands(load(LONG_STEP, uv_mode="SMART"))
big = [a3 for a3, _a2 in rows if a3 > long_area * 0.8]
check(len(big) == 2,
      "both faces of a 13 to 1 plate stay merged (%d found)" % len(big))


def folded(objs):
    """UV islands whose faces do not all wind the same way."""
    bad = 0
    for o in objs:
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
            signs = []
            for f in group:
                q = [lp[uvl].uv for lp in f.loops]
                tot = 0.0
                for k in range(1, len(q) - 1):
                    p, s, r = q[0], q[k], q[k + 1]
                    tot += ((s.x - p.x) * (r.y - p.y)
                            - (r.x - p.x) * (s.y - p.y))
                signs.append(tot)
            v = np.array(signs)
            if (v > 1e-12).any() and (v < -1e-12).any():
                bad += 1
        bm.free()
    return bad


def stretched(objs, limit=5.0):
    """Share of the surface stretched more than `limit` times one way."""
    tot = bad = 0.0
    for o in objs:
        me = o.data
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        vidx = np.empty(len(me.loops), dtype=np.int32)
        me.loops.foreach_get("vertex_index", vidx)
        uv = np.empty(len(me.loops) * 2, dtype=np.float64)
        me.uv_layers.active.uv.foreach_get("vector", uv)
        uv = uv.reshape(-1, 2)
        for poly in me.polygons:
            idx = list(poly.loop_indices)
            for k in range(1, len(idx) - 1):
                c = (idx[0], idx[k], idx[k + 1])
                p = co[vidx[list(c)]]
                q = uv[list(c)]
                e1, e2 = p[1] - p[0], p[2] - p[0]
                n = np.cross(e1, e2)
                area = 0.5 * np.linalg.norm(n)
                if area < 1e-14 or np.linalg.norm(e1) < 1e-12:
                    continue
                x = e1 / np.linalg.norm(e1)
                y = np.cross(n, x)
                y /= np.linalg.norm(y)
                src = np.array([[e1 @ x, e2 @ x], [e1 @ y, e2 @ y]])
                dst = np.array([q[1] - q[0], q[2] - q[0]]).T
                if abs(np.linalg.det(src)) < 1e-18:
                    continue
                s = np.linalg.svd(dst @ np.linalg.inv(src), compute_uv=False)
                tot += area
                if s[1] <= 1e-12 or s[0] / s[1] > limit:
                    bad += area
    return bad / tot if tot else 0.0


# ---- a pipe with holes is not a sheet ------------------------------------
# The Closed surfaces test leaves a sheet with holes whole, and a pipe with
# holes has the same topology as one. What sets the pipe apart is its ends,
# where the surface normal goes all the way round. If the test missed that,
# the pipe would stay uncut and fold when it is flattened.
print("\n== a pipe with holes still gets its cut")
for label, kw in (("Unwrap (Conformal)", dict(uv_mode="CONFORMAL")),
                  ("CAD Surfaces (Smart)", dict(uv_mode="SMART"))):
    objs = load(PIPE_STEP, **kw)
    share = stretched(objs)
    n = folded(objs)
    check(share < 0.02 and n == 0,
          "%s: %.1f%% of the surface stretched over 5x, %d folded"
          % (label, 100.0 * share, n))

# ---- Smart does not melt a rounded block ---------------------------------
# A rounded block is tangent all the way round, and a closed surface
# flattened in one piece has to land on itself. Smart builds a net one face
# at a time, the way a paper model is cut, and a net never needs the closed
# surface in one piece.
print("\n== Smart does not melt a rounded block")
rows = islands(load(BLOCK_STEP, uv_mode="SMART"), want_tris=True)
worst = max(overlap_share(r[2]) for r in rows)
check(worst < 0.01, "Smart leaves no island on itself (worst %.1f%%)"
      % (100.0 * worst))
n_none = len(islands(load(BLOCK_STEP, uv_mode="SURFACE")))
total = sum(r[0] for r in rows)
# The whole block joins as one net, and the split then cuts that net where
# its pieces pack better, so a few islands come out, not one.
check(len(rows) <= 5,
      "and it still joins it into a few islands (%d against %d, the "
      "largest %.0f%% of the surface)"
      % (len(rows), n_none, 100.0 * max(r[0] for r in rows) / total))
rows_whole = islands(load(BLOCK_STEP, uv_mode="SMART", uv_smart_split=False),
                     want_tris=True)
worst = max(overlap_share(r[2]) for r in rows_whole)
check(len(rows_whole) < len(rows) and worst < 0.01,
      "with Optimize island shape off, the net stays whole (%d islands, "
      "worst overlap %.1f%%)" % (len(rows_whole), 100.0 * worst))

# ---- Smart bends a face that does not fit by a turn -----------------------
# A rounded rim misses the straight edge of a rolled out wall by a few
# percent, and a ring meets it along a circle. Smart lays each of them out
# along the island's edge instead: a rim becomes a strip, a ring unrolls
# into one. A full disk would have to be stretched without limit at its
# middle, so it stays an island of its own.


def squashed(objs, limit=0.8):
    """Share of the surface squashed below `limit` of the part's density.

    Squash is the stretch that costs texture detail, so a bend may stretch
    a face along the shared edge but must not squash it.
    """
    tot = bad = 0.0
    for o in objs:
        me = o.data
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        vidx = np.empty(len(me.loops), dtype=np.int32)
        me.loops.foreach_get("vertex_index", vidx)
        uv = np.empty(len(me.loops) * 2, dtype=np.float64)
        me.uv_layers.active.uv.foreach_get("vector", uv)
        uv = uv.reshape(-1, 2)
        rows = []
        for poly in me.polygons:
            idx = list(poly.loop_indices)
            for k in range(1, len(idx) - 1):
                c = [idx[0], idx[k], idx[k + 1]]
                p = co[vidx[c]]
                q = uv[c]
                e1, e2 = p[1] - p[0], p[2] - p[0]
                n = np.cross(e1, e2)
                area = 0.5 * np.linalg.norm(n)
                if area < 1e-14:
                    continue
                x = e1 / np.linalg.norm(e1)
                y = np.cross(n, x)
                y /= np.linalg.norm(y)
                src = np.array([[e1 @ x, e2 @ x], [e1 @ y, e2 @ y]])
                dst = np.array([q[1] - q[0], q[2] - q[0]]).T
                s = np.linalg.svd(dst @ np.linalg.inv(src), compute_uv=False)
                rows.append((s[1], np.sqrt(s[0] * s[1]), area))
        rows = np.array(rows)
        density = np.median(rows[:, 1])
        tot += rows[:, 2].sum()
        bad += rows[rows[:, 0] < limit * density, 2].sum()
    return bad / tot if tot else 0.0


print("\n== Smart bends a rounded rim on to the wall it meets")
objs = load(TURNED["cylinder"], uv_mode="SMART")
rows = islands(objs, want_tris=True)
n_fold = folded(objs)
n_face = len(islands(load(TURNED["cylinder"], uv_mode="SURFACE")))
total = sum(r[0] for r in rows)
check(n_face == 5 and len(rows) == 3,
      "wall, rims and caps: %d islands against %d, the caps alone"
      % (len(rows), n_face))
check(max(r[0] for r in rows) > 0.75 * total,
      "the wall and both rims are one island (%.0f%% of the surface)"
      % (100.0 * max(r[0] for r in rows) / total))
worst = max(overlap_share(r[2]) for r in rows)
check(worst < 0.01 and n_fold == 0,
      "nothing lands on itself or folds (worst %.1f%%)" % (100.0 * worst))

print("\n== the ends of a tube unroll into strips")
objs = load(TURNED["tube"], uv_mode="SMART")
rows = islands(objs, want_tris=True)
check(len(rows) == 2,
      "the outside is one island and the bore another (%d islands)"
      % len(rows))
worst = max(overlap_share(r[2]) for r in rows)
check(worst < 0.01 and folded(objs) == 0,
      "nothing lands on itself or folds (worst %.1f%%)" % (100.0 * worst))
share = squashed(objs)
check(share < 0.01,
      "no face is squashed, so no face loses texture detail (%.1f%% of "
      "the surface)" % (100.0 * share))
n_rigid = len(islands(load(TURNED["tube"], uv_mode="SMART",
                           uv_smart_distortion=0.0)))
check(n_rigid >= 4,
      "with Smart distortion at 0 nothing is bent (%d islands)" % n_rigid)

print("\n== a casting comes out as a few clean islands")
objs = load(TURNED["casting"], uv_mode="SMART")
rows = islands(objs, want_tris=True)
n_fold = folded(objs)
n_face = len(islands(load(TURNED["casting"], uv_mode="SURFACE")))
worst = max(overlap_share(r[2]) for r in rows)
check(len(rows) <= 4,
      "%d islands against %d for CAD Surfaces" % (len(rows), n_face))
check(worst < 0.01 and n_fold == 0,
      "nothing lands on itself or folds (worst %.1f%%)" % (100.0 * worst))

# ---- Join sharp edges -------------------------------------------------------
# A second pass joins across sharp edges, with each island of the first pass
# as one piece. A join across a fold is the same turn as a join across a
# smooth edge, because every chart is flat already, so the net stays true
# to size and never lands on itself.
print("\n== Join sharp edges unfolds a block that has only sharp edges")
n_face = len(islands(load(BRACKET_STEP, uv_mode="SURFACE")))
n_smart = len(islands(load(BRACKET_STEP, uv_mode="SMART")))
objs = load(BRACKET_STEP, uv_mode="SMART", uv_smart_sharp=True)
rows = islands(objs, want_tris=True)
n_fold = folded(objs)
check(n_smart >= n_face - 2,
      "Smart on its own leaves the sharp edges alone (%d islands against "
      "%d)" % (n_smart, n_face))
check(len(rows) <= n_face // 2,
      "with the option on the block needs far fewer islands (%d against "
      "%d)" % (len(rows), n_face))
worst = max(overlap_share(r[2]) for r in rows)
check(worst < 0.01 and n_fold == 0,
      "nothing lands on itself or folds (worst %.1f%%)" % (100.0 * worst))
d = [math.sqrt(a2 / a3) for a3, a2, _t in rows if a3 > 1e-9 and a2 > 1e-12]
spread = max(d) / min(d) if len(d) > 1 else 1.0
check(spread < 1.05,
      "and every island keeps the same texel density (%.2fx)" % spread)

# ---- Smart cuts an island that packs badly ---------------------------------
# The net is a tree of charts, so an island can be cut along any join and
# each piece keeps its layout. An arm out of the corner of a square leaves
# most of the rectangle round the two of them empty, and cut off it packs
# far better. A strip along a whole edge fills that rectangle, and stays.
print("\n== Smart cuts an island that packs badly, and keeps one that packs well")


def flat_pieces(arm):
    """A square face, and a second face next to it, each its own chart.

    The two lie flat side by side, so the edge between them is smooth, and
    the UVs of each face are its own coordinates, moved apart, so each is a
    chart of its own that Smart can join.
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    if arm:
        # A 10 by 10 square, and a 20 by 1 arm out of its corner.
        verts = [(0, 0, 0), (10, 0, 0), (10, 1, 0), (10, 10, 0), (0, 10, 0),
                 (30, 0, 0), (30, 1, 0)]
        faces = [(0, 1, 2, 3, 4), (1, 5, 6, 2)]
    else:
        # The same square, and a 2 by 10 strip along its whole edge.
        verts = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
                 (12, 0, 0), (12, 10, 0)]
        faces = [(0, 1, 2, 3), (1, 4, 5, 2)]
    me = bpy.data.meshes.new("pieces")
    me.from_pydata([tuple(float(x) for x in v) for v in verts], [], faces)
    me.update()
    layer = me.uv_layers.new(name="UVMap")
    uvs = []
    for k, f in enumerate(faces):
        for v in f:
            uvs += [verts[v][0] + 100.0 * k, verts[v][1] + 50.0 * k]
    layer.uv.foreach_set("vector", uvs)
    return me


def pieces_after(arm, gain=None, split=True):
    me = flat_pieces(arm)
    from CADder import uv as live
    old = live.SMART_SPLIT_GAIN
    if gain is not None:
        live.SMART_SPLIT_GAIN = gain
    try:
        # A tile larger than the arm and the square together, so the tile
        # never decides.
        _charts, made = live.smart_merge(me, side_3d=40.0, split=split)
    finally:
        live.SMART_SPLIT_GAIN = old
    obj = bpy.data.objects.new("pieces", me)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    return len(islands([obj]))


check(pieces_after(True, gain=9.0) == 1,
      "the arm joins the square when nothing cuts it")
check(pieces_after(True) == 2,
      "and the cut takes it off again, because the two pack better apart")
check(pieces_after(False) == 1,
      "a strip along the whole edge stays joined")
check(pieces_after(True, split=False) == 1,
      "with Optimize island shape off, the arm stays on the square")

# ---- Smart refuses a join that overlaps -----------------------------------
# Five squares round one corner add up to 450 degrees. Laid flat one after
# another they go round once and the fifth lands on the first, whatever
# order they come in. This is built as a mesh on purpose, with nothing
# sharp, so the overlap test is the only thing that can stop it.
print("\n== Smart refuses a join that lands on the island")
from CADder import uv as uv_mod


def saddle():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    n = 5
    verts = [(0.0, 0.0, 0.0)]
    for i in range(n):
        a = 2.0 * math.pi * i / n
        verts.append((math.cos(a), math.sin(a), 0.0))
    for i in range(n):
        a = 2.0 * math.pi * (i + 0.5) / n
        z = 0.5 if i % 2 == 0 else -0.5
        verts.append((1.4 * math.cos(a), 1.4 * math.sin(a), z))
    faces = [(0, 1 + i, 1 + n + i, 1 + (i + 1) % n) for i in range(n)]
    me = bpy.data.meshes.new("saddle")
    me.from_pydata(verts, [], faces)
    me.update()
    layer = me.uv_layers.new(name="UVMap")
    # Every square gets the same unit square, so each is a chart of its
    # own: the corner they share is (0, 0) in all of them, but the far end
    # of each shared edge is (0, 1) on one side and (1, 0) on the other.
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    uvs = [c for _ in range(n) for c in square]
    layer.uv.foreach_set("vector", [x for c in uvs for x in c])
    return me


me = saddle()
charts, made = uv_mod.smart_merge(me)
uv = np.empty(len(me.loops) * 2, dtype=np.float64)
me.uv_layers.active.uv.foreach_get("vector", uv)
uv = uv.reshape(-1, 2)
obj = bpy.data.objects.new("saddle", me)
bpy.context.scene.collection.objects.link(obj)
bpy.context.view_layer.update()
rows = islands([obj], want_tris=True)
worst = max(overlap_share(r[2]) for r in rows)
check(charts == 5 and len(rows) == 2,
      "the five squares make two islands, not one (%d)" % len(rows))
check(worst < 0.01,
      "and neither lands on itself (worst %.1f%%)" % (100.0 * worst))

# ---- Smart trims only what does not fit the tile -------------------------
# The stair's flat pattern is longer than the square the stair packs into
# on its own. Packed per part, it has to be cut. Packed together with a
# large plate the tile is far bigger, and the same pattern stays whole.
print("\n== Smart trims a pattern only when it does not fit")
stair_flat = STAIR_PROFILE * STAIR_WIDTH
alone = islands(load(STAIR_STEP, uv_mode="SMART", uv_pack="OBJECT",
                     _only="stair"))
shared = islands(load(STAIR_STEP, uv_mode="SMART", uv_pack="ALL",
                      _only="stair"))
biggest_alone = max(a3 for a3, _a2 in alone)
whole_shared = [a3 for a3, _a2 in shared if a3 > 0.85 * stair_flat]
check(biggest_alone < 0.8 * stair_flat,
      "in a tile of its own the %.0f mm pattern is cut (largest piece %.0f "
      "of %.0f mm2)" % (STAIR_PROFILE, biggest_alone, stair_flat))
check(len(whole_shared) == 2,
      "in a tile shared with the plate both sides stay whole (%d found)"
      % len(whole_shared))

# ---- Smart unwraps what one scale cannot flatten -------------------------
# A sphere does not unroll. Its own coordinates put the same angle in u all
# the way to the pole, where the real distance round it has gone to nothing,
# so no one scale can hold a texture on it. A plane next to it unrolls
# exactly, and has to come out of this untouched.
print("\n== Smart unwraps the faces one scale cannot flatten")


def ball_and_plate(lat=math.radians(75.0), steps=16):
    """A patch of a sphere in its own coordinates, and a flat square.

    The sphere patch runs from the equator to `lat`, and carries the UVs
    the surface gives: the angle round times the radius, and the angle up
    times the radius. Those are right at the equator and wrong at the top.
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    radius = 10.0
    verts, faces, uvs = [], [], []
    for i in range(steps + 1):
        for j in range(steps + 1):
            u = 2.0 * math.pi * i / steps
            v = lat * j / steps
            verts.append((radius * math.cos(v) * math.cos(u),
                          radius * math.cos(v) * math.sin(u),
                          radius * math.sin(v)))
    for i in range(steps):
        for j in range(steps):
            a = i * (steps + 1) + j
            b = (i + 1) * (steps + 1) + j
            faces.append((a, b, b + 1, a + 1))
            for corner in ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)):
                uvs += [radius * 2.0 * math.pi * corner[0] / steps,
                        radius * lat * corner[1] / steps]
    base = len(verts)
    square = [(-30.0, -30.0, -20.0), (-10.0, -30.0, -20.0),
              (-10.0, -10.0, -20.0), (-30.0, -10.0, -20.0)]
    verts += square
    faces.append(tuple(base + k for k in range(4)))
    for x, y, _z in square:
        uvs += [x, y]
    me = bpy.data.meshes.new("ball")
    me.from_pydata(verts, [], faces)
    me.update()
    layer = me.uv_layers.new(name="UVMap")
    layer.uv.foreach_set("vector", uvs)
    obj = bpy.data.objects.new("ball", me)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update()
    return obj


obj = ball_and_plate()
me = obj.data
flat = len(me.polygons) - 1          # the square is the last face
mask = uv_mod.strained(me)
before = uv_mod.read_uvs(me).copy()
check(mask is not None and mask[:flat].all() and not mask[flat],
      "the sphere is picked out and the plane is not (%d of %d face(s))"
      % (0 if mask is None else int(mask.sum()), len(me.polygons)))

faces, made = m._unwrap_awkward_objects([obj])
after = uv_mod.read_uvs(me)
left = uv_mod.strained(me)
check(faces == flat and made == 1,
      "the unwrap takes the sphere and makes one island of it (%d face(s), "
      "%d island(s))" % (faces, made))
check(not left.any(),
      "and one scale then holds it (%d face(s) still over)"
      % int(left.sum()))
starts = np.empty(len(me.polygons), dtype=np.int64)
me.polygons.foreach_get("loop_start", starts)
totals = np.empty(len(me.polygons), dtype=np.int64)
me.polygons.foreach_get("loop_total", totals)
side = slice(int(starts[flat]), int(starts[flat] + totals[flat]))
check(np.allclose(after[side], before[side]),
      "the plane keeps the chart of its own surface")

# The island comes back at the size of the CAD charts, not packed into the
# 0 to 1 square the unwrap leaves it in.
uv = after[:int(starts[flat])]
real = 4.0 * math.pi * 10.0 * 10.0 * (math.sin(math.radians(75.0)) / 2.0)
span = float((uv.max(axis=0) - uv.min(axis=0)).max())
check(span > 10.0, "and at the size of the part, not packed into 0 to 1 "
      "(%.1f across, %.0f mm2 of surface)" % (span, real))

# ---- a refresh reproduces it ---------------------------------------------
print("\n== the setting is stamped on the object")
objs = load(uv_mode="SMART")
rec = str(objs[0].get("STEP_import_settings")
          or objs[0].get("import_record_json") or "")
check('"uv_mode": "SMART"' in rec and "uv_smart_distortion" in rec,
      "the mode and its distortion limit reach the record")

if FAILS:
    print("\nuv_tangent_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_tangent_smoke: OK - a bent plate flattens like a flat pattern")
