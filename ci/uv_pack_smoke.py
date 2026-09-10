# SPDX-License-Identifier: GPL-3.0-or-later
"""UV island packing at import: does each mode put the islands where it says?

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_pack_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

Why the coverage is rasterized
------------------------------
A bounding box says nothing about whether two islands sit on top of each
other. These checks rasterize every UV triangle into a grid and count the
cells that more than one triangle covers, which is the only measurement
that can tell a packed layout from a pile.
"""
import os
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import main as m

FAILS = []
PARTS = 6


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path, count):
    """Several distinct blocks, each with a hole, so no mesh is shared."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for i in range(count):
        w, d, h = 30.0 + i * 4.0, 20.0 + i * 3.0, 10.0
        shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), w, d, h).Shape()
        hole = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(w * 0.5, d * 0.5, -1), gp_Dir(0, 0, 1)),
            4.0, h + 2).Shape()
        shape = BRepAlgoAPI_Cut(shape, hole).Shape()
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString("part_%d" % i))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_uvpack_")
STEP = os.path.join(tmp, "blocks.step")
write_step(STEP, PARTS)


def load(**kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
    m._cache_drop(STEP)
    opts = dict(htypes="FLAT", up_as="Z", uv_mode="UNWRAP")
    opts.update(kw)
    m.load_step(bpy.context, STEP, **opts)
    return [o for o in bpy.data.objects
            if o.type == "MESH" and o.data and len(o.data.polygons)]


# ---- measurement ----------------------------------------------------------

def loop_uvs(me):
    n = len(me.loops)
    a = np.empty(n * 2, dtype=np.float64)
    me.uv_layers.active.uv.foreach_get("vector", a)
    return a.reshape(-1, 2)


def triangles(objs):
    out = []
    for o in objs:
        me = o.data
        if not len(me.uv_layers):
            continue
        uv = loop_uvs(me)
        for poly in me.polygons:
            idx = list(poly.loop_indices)
            for k in range(1, len(idx) - 1):
                out.append(uv[[idx[0], idx[k], idx[k + 1]]])
    return out


def coverage(tris, region, grid=256):
    """(used, covered twice) as fractions of the region."""
    lo = np.array(region[0], dtype=float)
    span = np.maximum(np.array(region[1], dtype=float) - lo, 1e-9)
    count = np.zeros((grid, grid), dtype=np.int32)
    for tri in tris:
        t = (tri - lo) / span * grid
        a = np.clip(np.floor(t.min(axis=0)).astype(int), 0, grid)
        b = np.clip(np.ceil(t.max(axis=0)).astype(int) + 1, 0, grid)
        if b[0] <= a[0] or b[1] <= a[1]:
            continue
        gx, gy = np.meshgrid(np.arange(a[0], b[0]) + 0.5,
                             np.arange(a[1], b[1]) + 0.5)
        (ax, ay), (bx, by), (cx, cy) = t
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < 1e-12:
            continue
        l1 = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / det
        l2 = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / det
        hit = (l1 >= 0) & (l2 >= 0) & (1.0 - l1 - l2 >= 0)
        if hit.any():
            count[a[1]:b[1], a[0]:b[0]] += hit.astype(np.int32)
    total = float(grid * grid)
    return (count > 0).sum() / total, (count > 1).sum() / total


def tile_of(obj):
    uv = loop_uvs(obj.data)
    return (int(np.floor(uv[:, 0].mean())), int(np.floor(uv[:, 1].mean())))


def bounds(objs):
    lo = np.array([1e30, 1e30])
    hi = np.array([-1e30, -1e30])
    for o in objs:
        uv = loop_uvs(o.data)
        lo = np.minimum(lo, uv.min(axis=0))
        hi = np.maximum(hi, uv.max(axis=0))
    return lo, hi


# ---- what it looks like without packing -----------------------------------
print("\n== without packing")
objs = load(uv_pack="NONE", uv_normalize=False)
lo, hi = bounds(objs)
# Measure against whole tiles, not against the bounding box of the islands.
# The bounding box flatters the layout: it can report a tight fit while the
# islands sit in one small corner of a tile nobody else can use.
region = (np.floor(lo), np.maximum(np.ceil(hi), np.floor(lo) + 1))
before_used, before_over = coverage(triangles(objs), region)
tiles = {tile_of(o) for o in objs}
print("   spans u %.2f..%.2f v %.2f..%.2f over %d tile(s), "
      "%.1f%% of that tile space used"
      % (lo[0], hi[0], lo[1], hi[1], len(tiles), 100.0 * before_used))
check(True, "measured the unpacked layout (%.1f%% used, %.1f%% doubled)"
      % (100.0 * before_used, 100.0 * before_over))

# ---- All parts together ---------------------------------------------------
print("\n== All parts together")
objs = load(uv_pack="ALL", uv_normalize=False)
tiles = {tile_of(o) for o in objs}
used, over = coverage(triangles(objs), ((0, 0), (1, 1)))
check(tiles == {(0, 0)},
      "every part lands in tile 0 (%s)" % sorted(tiles))
check(used > 0.4, "the tile is filled (%.0f%% used)" % (100.0 * used))
check(used > before_used * 2.0,
      "it uses far more of the tile than the unpacked layout "
      "(%.0f%% against %.0f%%)" % (100.0 * used, 100.0 * before_used))
check(over < 0.01, "the islands do not sit on each other (%.2f%% doubled)"
      % (100.0 * over))

# ---- Each part on its own -------------------------------------------------
print("\n== Each part on its own")
objs = load(uv_pack="OBJECT", uv_normalize=False)
check(all(tile_of(o) == (0, 0) for o in objs),
      "every part gets its own 0-1 tile")
worst_used, worst_over = 1.0, 0.0
for o in objs:
    u, ov = coverage(triangles([o]), ((0, 0), (1, 1)))
    worst_used = min(worst_used, u)
    worst_over = max(worst_over, ov)
check(worst_used > 0.4,
      "even the emptiest part fills its tile (%.0f%%)" % (100.0 * worst_used))
check(worst_over < 0.01,
      "no part overlaps itself (worst %.2f%%)" % (100.0 * worst_over))

# ---- Into UDIM tiles ------------------------------------------------------
print("\n== Into UDIM tiles")
for want in (2, 3):
    objs = load(uv_pack="UDIM", uv_pack_tiles=want, uv_normalize=False)
    tiles = sorted({tile_of(o) for o in objs})
    check(len(tiles) == want,
          "%d tiles asked, %d used (%s)" % (want, len(tiles), tiles))
    check(all(t[0] < 10 for t in tiles),
          "the tiles stay inside one UDIM row (%s)" % tiles)
    worst = 0.0
    thin = 1.0
    for t in tiles:
        group = [o for o in objs if tile_of(o) == t]
        u, ov = coverage(triangles(group), (t, (t[0] + 1, t[1] + 1)))
        worst = max(worst, ov)
        thin = min(thin, u)
    check(worst < 0.01, "no tile has islands on top of each other (%.2f%%)"
          % (100.0 * worst))
    check(thin > 0.3, "every tile is filled (emptiest %.0f%%)"
          % (100.0 * thin))

# ---- the setting reaches a refresh ---------------------------------------
print("\n== a refresh reproduces it")
objs = load(uv_pack="UDIM", uv_pack_tiles=3)
rec = str(objs[0].get("STEP_import_settings")
          or objs[0].get("import_record_json") or "")
check("uv_pack" in rec and "uv_pack_tiles" in rec,
      "both settings are stamped on the object")

if FAILS:
    print("\nuv_pack_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_pack_smoke: OK - packed, filled, and nothing on top of anything")
