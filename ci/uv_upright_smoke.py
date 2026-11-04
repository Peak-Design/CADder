# SPDX-License-Identifier: GPL-3.0-or-later
"""Rotate to Fit off: do the islands stand upright to the world?

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_upright_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

CAD Surfaces gives each CAD face an island in the parameter frame of its
surface, so the islands of one part lie in all directions, and a texture
with a direction (wood grain, brushed metal) runs a different way on each.
With Rotate to Fit off, every island is turned so world Z points up the
tile, world Y on a flat face, and the pack keeps them that way.

The parts stand in an assembly at several rotations, so the world frame
comes from the placement and the up axis, not from the part file. Each
check is made on every triangle: the UV direction of world Z (or world Y,
or the axis of a lying cylinder) is measured through the 3D to UV map of
that triangle.
"""
import math
import os
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy
from mathutils import Matrix

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, tools as T

FAILS = []
TOL = 1.0  # degrees


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """An assembly of five parts, each placed with its own rotation.

    flat_box    a box turned 30 degrees about Z
    tilt_box    a box turned 90 degrees about X, then 20 about Z, so the
                face that was its top now stands up
    up_cyl      a cylinder standing on end
    lying_cyl   a cylinder turned onto its side, axis along world X
    round_box   a box with its four upright edges filleted, turned 25
                degrees about Z. CAD Surfaces (Smart) joins its sides and
                fillets into one band.
    The file is Z up (the import uses up axis Z), except where a test asks
    for Y up.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Line
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec, gp_Ax1, gp_Dir, gp_Ax2
    from OCP.TopLoc import TopLoc_Location
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    def rounded():
        box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 60.0, 40.0, 30.0).Shape()
        mk = BRepFilletAPI_MakeFillet(box)
        ex = TopExp_Explorer(box, TopAbs_EDGE)
        while ex.More():
            e = TopoDS.Edge_s(ex.Current())
            c = BRepAdaptor_Curve(e)
            if c.GetType() == GeomAbs_Line:
                d = c.Line().Direction()
                if abs(d.Z()) > 0.99:
                    mk.Add(6.0, e)
            ex.Next()
        return mk.Shape()

    def turn(*steps):
        t = gp_Trsf()
        for axis, deg, at in steps:
            r = gp_Trsf()
            r.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(*axis)),
                          math.radians(deg))
            t = r.Multiplied(t)
        move = gp_Trsf()
        move.SetTranslation(gp_Vec(*steps[-1][2]))
        return move.Multiplied(t)

    parts = [
        ("flat_box", BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 50.0, 30.0,
                                         20.0).Shape(),
         turn(((0, 0, 1), 30, (0, 0, 0)))),
        ("tilt_box", BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 45.0, 25.0,
                                         15.0).Shape(),
         turn(((1, 0, 0), 90, (0, 0, 0)), ((0, 0, 1), 20, (100, 0, 0)))),
        ("up_cyl", BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0),
                                                   gp_Dir(0, 0, 1)),
                                            10.0, 60.0).Shape(),
         turn(((0, 0, 1), 15, (200, 0, 0)))),
        ("lying_cyl", BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0),
                                                      gp_Dir(0, 0, 1)),
                                               12.0, 70.0).Shape(),
         turn(((0, 1, 0), 90, (300, 0, 0)))),
        ("round_box", rounded(), turn(((0, 0, 1), 25, (400, 0, 0)))),
    ]
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    top = tool.NewShape()
    TDataStd_Name.Set_s(top, TCollection_ExtendedString("top"))
    for name, shape, trsf in parts:
        leaf = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(leaf, TCollection_ExtendedString(name))
        tool.AddComponent(top, leaf, TopLoc_Location(trsf))
    tool.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_upright_")
STEP = os.path.join(tmp, "upright.step")
write_step(STEP)


def load(**kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)
    opts = dict(htypes="FLAT", up_as="ZPOS", tris_to_quads=True,
                uv_mode="SURFACE", uv_normalize=False, uv_rotate=False)
    opts.update(kw)
    m.load_step(bpy.context, STEP, **opts)
    bpy.context.view_layer.update()
    return parts()


def parts():
    """Part name -> (mesh object, the world matrix that shows it)."""
    out = {}
    holders = {}
    for o in bpy.context.scene.objects:
        if o.instance_type == "COLLECTION" and o.instance_collection:
            holders.setdefault(o.instance_collection.name, o)
    for o in bpy.data.objects:
        if o.type != "MESH" or not o.data or not len(o.data.polygons):
            continue
        name = o.get("STEP_name", o.name)
        mw = o.matrix_world
        for col in o.users_collection:
            if col.name in holders:
                mw = holders[col.name].matrix_world
        out.setdefault(name, (o, mw.copy()))
    return out


def triangles(obj, mw):
    """World corners, UV corners and world normals of the fan triangles."""
    me = obj.data
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    rot = np.array(mw.to_3x3())
    co = co @ rot.T
    lv = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    uv = np.empty(len(me.loops) * 2)
    me.uv_layers.active.uv.foreach_get("vector", uv)
    uv = uv.reshape(-1, 2)
    t0, t1, t2 = [], [], []
    for p in me.polygons:
        s = p.loop_start
        for k in range(1, p.loop_total - 1):
            t0.append(s)
            t1.append(s + k)
            t2.append(s + k + 1)
    t0, t1, t2 = (np.array(t, dtype=np.int64) for t in (t0, t1, t2))
    return co[lv[t0]], co[lv[t1]], co[lv[t2]], uv[t0], uv[t1], uv[t2]


def directions(tri, w):
    """The UV direction of the world direction `w` on each triangle, the
    triangle normals, the 3D areas and the signed UV areas."""
    p0, p1, p2, u0, u1, u2 = tri
    e1, e2 = p1 - p0, p2 - p0
    n = np.cross(e1, e2)
    area = 0.5 * np.linalg.norm(n, axis=1)
    nn = n / np.maximum(2.0 * area, 1e-30)[:, None]
    w = np.asarray(w, dtype=float)
    wt = w - (nn @ w)[:, None] * nn
    g = np.stack([[(e1 * e1).sum(1), (e1 * e2).sum(1)],
                  [(e1 * e2).sum(1), (e2 * e2).sum(1)]]).transpose(2, 0, 1)
    b = np.stack([(e1 * wt).sum(1), (e2 * wt).sum(1)], axis=1)
    ok = np.abs(np.linalg.det(g)) > 1e-12
    ab = np.zeros_like(b)
    ab[ok] = np.linalg.solve(g[ok], b[ok][:, :, None])[:, :, 0]
    d = ab[:, :1] * (u1 - u0) + ab[:, 1:] * (u2 - u0)
    suv = 0.5 * ((u1 - u0)[:, 0] * (u2 - u0)[:, 1]
                 - (u2 - u0)[:, 0] * (u1 - u0)[:, 1])
    return d, nn, np.where(ok, area, 0.0), suv


def off_up(d):
    """Degrees between each direction and +V."""
    return np.degrees(np.abs(np.arctan2(d[:, 0], d[:, 1])))


def off_across(d):
    """Degrees between each direction and the U axis, either way."""
    a = np.degrees(np.abs(np.arctan2(d[:, 1], d[:, 0])))
    return np.minimum(a, 180.0 - a)


def worst(obj, mw, what):
    """The worst error in degrees over the triangles that `what` picks,
    and the share of UV area that is mirrored."""
    tri = triangles(obj, mw)
    dz, nn, area, suv = directions(tri, (0, 0, 1))
    dy, _n, _a, _s = directions(tri, (0, 1, 0))
    dx, _n, _a, _s = directions(tri, (1, 0, 0))
    big = area > 1e-6 * area.max()
    upright_side = big & (np.abs(nn[:, 2]) < 0.1)
    flat = big & (np.abs(nn[:, 2]) > 0.99)
    errs = []
    if what in ("sides", "all"):
        errs.append(off_up(dz[upright_side]))
    if what == "all":
        errs.append(off_up(dy[flat]))
    if what in ("axis_x", "axis_y"):
        # A cylinder lying along world X or Y: its axis runs across the
        # tile on the curved side. Its end caps stand up, so Z is up there.
        k = 0 if what == "axis_x" else 1
        curved = big & (np.abs(nn[:, k]) < 0.1)
        errs.append(off_across((dx if k == 0 else dy)[curved]))
        caps = big & (np.abs(nn[:, k]) > 0.99)
        errs.append(off_up(dz[caps]))
    e = np.concatenate(errs) if errs else np.zeros(0)
    mirrored = float(np.abs(suv[big & (suv < 0)]).sum()
                     / max(np.abs(suv[big]).sum(), 1e-30))
    return (float(e.max()) if len(e) else 0.0), mirrored, int(len(e))


EXPECT = {"flat_box": "all", "tilt_box": "all", "up_cyl": "all",
          "lying_cyl": "axis_x", "round_box": "sides"}


def all_upright(label, objs, mirrored_ok=False):
    for name, what in EXPECT.items():
        if name not in objs:
            check(False, "%s: %s was imported" % (label, name))
            continue
        obj, mw = objs[name]
        err, mir, n = worst(obj, mw, what)
        check(n > 0 and err < TOL,
              "%s: %s is upright (worst %.3f degrees over %d triangles)"
              % (label, name, err, n))
        if not mirrored_ok:
            check(mir < 1e-6, "%s: %s has no mirrored island (%.4f of "
                  "its UV area)" % (label, name, mir))


print("\n== CAD Surfaces, no pack, Rotate to Fit off")
objs = load()
all_upright("surface", objs)

print("\n== the up axis of the file counts: the same parts from a Y up file")
# The parts of the fixture stand in Z. Read as a Y up file they lie down
# in Blender, and what stands up in the world is what was Y in the file.
# The check is made in the world, so it must hold either way. The standing
# cylinder of the file lies along world Y here.
objs = load(up_as="YPOS")
for name, what in (("flat_box", "all"), ("tilt_box", "all"),
                   ("up_cyl", "axis_y")):
    obj, mw = objs[name]
    err, mir, n = worst(obj, mw, what)
    check(n > 0 and err < TOL,
          "Y up: %s is upright in the world (worst %.3f degrees over %d "
          "triangles)" % (name, err, n))

print("\n== the pack keeps them upright")
for pack in ("ALL", "OBJECT", "UDIM"):
    objs = load(uv_pack=pack, uv_normalize=True)
    all_upright("pack %s" % pack, objs)

print("\n== CAD Surfaces (Smart) joins the band of the round box")
objs = load(uv_mode="SMART", uv_pack="ALL", uv_normalize=True)
all_upright("smart", objs)
obj, _mw = objs["round_box"]
r = m.uv_mod._Read(obj.data)
n_islands = int(r.charts(r.uv).max()) + 1
check(n_islands <= 4,
      "Smart joined the sides and fillets of the round box (%d islands)"
      % n_islands)

print("\n== collection instances: the empty holds the placement")
objs = load(htypes="COLLECTION_INSTANCES")
all_upright("instances", objs)

print("\n== Rotate to Fit on makes the map it always made")
objs = load(uv_rotate=True)
errs = [worst(o, mw, EXPECT[n])[0] for n, (o, mw) in objs.items()
        if n in EXPECT]
check(max(errs) > 5.0,
      "with the option on, some island is not upright (worst %.1f degrees)"
      % max(errs))
rec = objs["flat_box"][0].get("STEP_import_settings", "")
check('"uv_rotate": true' in rec, "the record says Rotate to Fit is on")

print("\n== Regenerate reads the option from the record")
objs = load()
rec = objs["flat_box"][0].get("STEP_import_settings", "")
check('"uv_rotate": false' in rec, "the record says Rotate to Fit is off")
targets = [o for o, _mw in objs.values()]
failed = T.STEPPER_OT_regenerate.rebuild(bpy.context, targets, False,
                                         lambda *a: None)
check(not failed, "Regenerate rebuilt every part")
all_upright("regenerate", parts())

print("\n== an older record has no key and keeps its islands")
objs = load()
for o, _mw in objs.values():
    rec = o.get("STEP_import_settings", "{}").replace(
        '"uv_rotate": false, ', "")
    o["STEP_import_settings"] = rec
check('"uv_rotate"' not in objs["flat_box"][0]["STEP_import_settings"],
      "the key is gone from the record")
T.STEPPER_OT_regenerate.rebuild(bpy.context, [o for o, _ in objs.values()],
                                False, lambda *a: None)
objs = parts()
errs = [worst(o, mw, EXPECT[n])[0] for n, (o, mw) in objs.items()
        if n in EXPECT]
check(max(errs) > 5.0,
      "the old record makes the old map (worst %.1f degrees)" % max(errs))

print("\n== Apply UVs with Rotate to Fit off")
objs = load(uv_rotate=True)
prg = bpy.context.scene.stepper
prg.uv_mode = "SURFACE"
prg.uv_pack = "ALL"
prg.uv_rotate = False
for o in bpy.context.view_layer.objects:
    o.select_set(o.type == "MESH")
bpy.context.view_layer.objects.active = objs["flat_box"][0]
bpy.ops.stepper.reapply_uv()
all_upright("apply UVs", parts())

print("\n== Smart UV Project")
objs = load(uv_mode="SMART_PROJECT", uv_normalize=True)
for name, (o, _mw) in objs.items():
    uv = np.empty(len(o.data.loops) * 2)
    o.data.uv_layers.active.uv.foreach_get("vector", uv)
    uv = uv.reshape(-1, 2)
    check(len(o.data.uv_layers) == 1 and o.data.uv_layers[0].name == "UVMap",
          "%s has one UV layer, UVMap" % name)
    check(uv.min() >= -1e-6 and uv.max() <= 1.0 + 1e-6 and np.ptp(uv) > 0.5,
          "%s: Smart UV Project filled the 0-1 tile (%.3f to %.3f)"
          % (name, uv.min(), uv.max()))
rec = objs["flat_box"][0].get("STEP_import_settings", "")
check('"uv_mode": "SMART_PROJECT"' in rec and '"uv_project_angle"' in rec,
      "the record holds the mode and its angle")
r = m.uv_mod._Read(objs["flat_box"][0].data)
check(int(r.charts(r.uv).max()) + 1 == 6,
      "a box at 66 degrees is cut into its 6 faces")

print("\n== the angle limit: 89 degrees still cuts a box, 30 cuts a "
      "cylinder into more islands than 89")
def cyl_islands(angle):
    o = load(uv_mode="SMART_PROJECT", uv_project_angle=math.radians(angle),
             tris_to_quads=False)["up_cyl"][0]
    rr = m.uv_mod._Read(o.data)
    return int(rr.charts(rr.uv).max()) + 1
few, many = cyl_islands(89.0), cyl_islands(5.0)
check(many > few, "a smaller angle limit cuts the cylinder into more "
      "islands (%d at 5 degrees, %d at 89)" % (many, few))

print("\n== Apply UVs with Smart UV Project keeps the mesh")
objs = load()
obj = objs["flat_box"][0]
faces = len(obj.data.polygons)
prg = bpy.context.scene.stepper
prg.uv_mode = "SMART_PROJECT"
prg.uv_pack = "NONE"
for o in bpy.context.view_layer.objects:
    o.select_set(o == obj)
bpy.context.view_layer.objects.active = obj
bpy.ops.stepper.reapply_uv()
check(len(obj.data.polygons) == faces, "the mesh is the one it was")
check('"uv_mode": "SMART_PROJECT"' in obj.get("STEP_import_settings", ""),
      "the record says Smart UV Project")
r = m.uv_mod._Read(obj.data)
check(int(r.charts(r.uv).max()) + 1 == 6, "the box has 6 islands")

if FAILS:
    print("\n%d check(s) failed:" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nall checks passed")
