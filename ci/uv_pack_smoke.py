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

import bmesh
import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m

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
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)
    opts = dict(htypes="FLAT", up_as="Z", uv_mode="CONFORMAL")
    opts.update(kw)
    m.load_step(bpy.context, STEP, **opts)
    # matrix_world holds the value it had before the import until the
    # depsgraph catches up. Any check that reads a world size has to force
    # that here, or it measures the file units as if they were scene units.
    bpy.context.view_layer.update()
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
# At real world scale, which is the default, the unpacked islands spread out
# over whatever tiles their size puts them in. That sprawl is the thing
# packing is for, so it is measured at that setting.
print("\n== without packing")
objs = load(uv_pack="NONE", uv_normalize=False)
lo, hi = bounds(objs)
# Measure against whole tiles, not against the bounding box of the islands.
# The bounding box flatters the layout: it can report a tight fit while the
# islands sit in one small corner of a tile nobody else can use.
region = (np.floor(lo), np.maximum(np.ceil(hi), np.floor(lo) + 1))
before_used, before_over = coverage(triangles(objs), region)
before_tiles = {tile_of(o) for o in objs}
print("   spans u %.2f..%.2f v %.2f..%.2f over %d tile(s), "
      "%.1f%% of that tile space used"
      % (lo[0], hi[0], lo[1], hi[1], len(before_tiles),
         100.0 * before_used))
check(True, "measured the unpacked layout (%d tile(s), %.1f%% used)"
      % (len(before_tiles), 100.0 * before_used))

# ---- All parts together ---------------------------------------------------
print("\n== All parts together")
objs = load(uv_pack="ALL", uv_normalize=True)
tiles = {tile_of(o) for o in objs}
used, over = coverage(triangles(objs), ((0, 0), (1, 1)))
check(tiles == {(0, 0)},
      "every part lands in tile 0 (%s)" % sorted(tiles))
check(used > 0.4, "the tile is filled (%.0f%% used)" % (100.0 * used))
check(len(tiles) <= len(before_tiles),
      "it needs no more tiles than the unpacked layout (%d against %d)"
      % (len(tiles), len(before_tiles)))
check(over < 0.01, "the islands do not sit on each other (%.2f%% doubled)"
      % (100.0 * over))

# ---- Each part on its own -------------------------------------------------
print("\n== Each part on its own")
objs = load(uv_pack="OBJECT", uv_normalize=True)
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
    objs = load(uv_pack="UDIM", uv_pack_tiles=want, uv_normalize=True)
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

# ---- keeping the island scale --------------------------------------------
# Packing does not have to resize anything. With Normalize UVs off
# the packer only arranges them, so a real world UV scale survives it. This
# is the reason the option exists.
print("\n== packing can keep the island scale")


def texel_ratio(objs):
    """Median UV length for one unit of 3D length, over every triangle."""
    out = []
    for o in objs:
        me = o.data
        uv = loop_uvs(me)
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        vidx = np.empty(len(me.loops), dtype=np.int32)
        me.loops.foreach_get("vertex_index", vidx)
        for poly in me.polygons:
            idx = list(poly.loop_indices)
            for k in range(len(idx)):
                a, b = idx[k], idx[(k + 1) % len(idx)]
                d3 = np.linalg.norm(co[vidx[a]] - co[vidx[b]])
                d2 = np.linalg.norm(uv[a] - uv[b])
                if d3 > 1e-9 and d2 > 1e-12:
                    out.append(d2 / d3)
    return float(np.median(out)) if out else 0.0


loose = texel_ratio(load(uv_pack="NONE", uv_normalize=False))
# Measure everything about this import before the next load, which resets
# the file and leaves the objects dead.
kept = load(uv_pack="ALL", uv_normalize=False)
kept_ratio = texel_ratio(kept)
kept_tris = triangles(kept)
lo, hi = np.array([1e30, 1e30]), np.array([-1e30, -1e30])
for tri in kept_tris:
    lo = np.minimum(lo, tri.min(axis=0))
    hi = np.maximum(hi, tri.max(axis=0))
_used, kept_over = coverage(kept_tris, (lo, hi), grid=384)
fitted = texel_ratio(load(uv_pack="ALL", uv_normalize=True))
check(abs(kept_ratio - loose) <= loose * 0.02,
      "with Normalize UVs off the texel size is unchanged (%.5g against "
      "%.5g)" % (kept_ratio, loose))
check(abs(fitted - loose) > loose * 0.05,
      "with Normalize UVs on the packer does resize the islands (%.5g "
      "against %.5g)" % (fitted, loose))
check(kept_over < 0.01,
      "and the islands still do not sit on each other (%.2f%%)"
      % (100.0 * kept_over))

# ---- texel density inside one part ---------------------------------------
# Two islands of one part have to hold the same number of texels for each
# millimeter of surface. If they do not, one face of a part reads sharp and
# the next reads blurred under the same texture. This is measured as the
# ratio between the densest and the sparsest island of a part.
print("\n== every island of a part has the same texel density")


def island_density(o):
    """sqrt(UV area / world area) for each UV island of one object."""
    me = o.data
    sc = o.matrix_world.to_scale()
    lin = (abs(sc.x) * abs(sc.y) * abs(sc.z)) ** (1.0 / 3.0)
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    uvl = bm.loops.layers.uv.active
    seen = set()
    out = []
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
            pts = [lp[uvl].uv for lp in f.loops]
            tot = 0.0
            for k in range(1, len(pts) - 1):
                p, q, r = pts[0], pts[k], pts[k + 1]
                tot += 0.5 * abs((q.x - p.x) * (r.y - p.y)
                                 - (r.x - p.x) * (q.y - p.y))
            a2 += tot
        if a3 > 1e-14 and a2 > 1e-16:
            out.append(np.sqrt(a2 / a3))
    bm.free()
    return out


def worst_spread(objs):
    """The widest density ratio found inside any one part, and the median."""
    worst = 1.0
    every = []
    for o in objs:
        d = island_density(o)
        if len(d) > 1:
            v = np.array(d)
            worst = max(worst, float(np.percentile(v, 95)
                                     / max(1e-12, np.percentile(v, 5))))
        every.extend(d)
    return worst, (float(np.median(every)) if every else 0.0)


# A part normalized to the 0-1 square is divided by one number, so its
# islands keep their size against each other. Before that each CAD face was
# fitted to the square on its own, which put a 5 mm face and a 200 mm face
# at the same size and measured near 10x here.
for label, kw in (("Normalize UVs on, no packing",
                   dict(uv_normalize=True, uv_pack="NONE")),
                  ("Normalize UVs on, packed per part",
                   dict(uv_normalize=True, uv_pack="OBJECT")),
                  ("real world scale, no packing",
                   dict(uv_normalize=False, uv_pack="NONE"))):
    spread, median = worst_spread(load(uv_mode="SURFACE", **kw))
    check(spread < 1.15,
          "CAD Surface, %s: %.2fx between the densest and the sparsest "
          "island of a part" % (label, spread))

# Box projection flattens every face onto one of three planes, so a face at
# an angle to all three arrives compressed. Packing may resize here, and
# Average Islands Scale runs before it and takes that out.
box_loose, _median = worst_spread(load(uv_mode="BOX", uv_normalize=True,
                                       uv_pack="NONE"))
box_packed, _median = worst_spread(load(uv_mode="BOX", uv_normalize=True,
                                        uv_pack="OBJECT"))
check(box_packed < 1.15 and box_packed < box_loose,
      "box projection evens out when it is packed (%.2fx against %.2fx "
      "loose)" % (box_packed, box_loose))

# Real world scale means one UV unit is one scene unit, on every island.
_spread, density = worst_spread(load(uv_mode="SURFACE", uv_normalize=False,
                                     uv_pack="NONE"))
check(abs(density - 1.0) < 0.05,
      "and with Normalize UVs off one UV unit is one scene unit (%.4f)"
      % density)

# ---- a texture that is not square ------------------------------------------
# Blender's Pack Islands and Unwrap read the image texture in the material,
# and work in the proportions of that image. On a 2 by 1 texture an island
# the packer turns came out 4 times too narrow, and a square face became a
# thin rectangle. The addon makes UVs for a square tile, whatever the
# material holds.
print("\n== a texture that is not square leaves the UVs square")


def squashed(objs):
    """Share of the surface whose UVs are squashed more than 1.2 to 1."""
    total = bad = 0.0
    for o in objs:
        me = o.data
        uv = loop_uvs(me)
        for poly in me.polygons:
            idx = list(poly.loop_indices)
            for k in range(1, len(idx) - 1):
                ids = (idx[0], idx[k], idx[k + 1])
                p = [me.vertices[me.loops[i].vertex_index].co for i in ids]
                e1, e2 = p[1] - p[0], p[2] - p[0]
                n = e1.cross(e2)
                if n.length < 1e-9:
                    continue
                x = e1.normalized()
                y = n.cross(e1).normalized()
                a = np.array([[e1.dot(x), e2.dot(x)], [e1.dot(y), e2.dot(y)]])
                u = uv[list(ids)]
                b = np.array([u[1] - u[0], u[2] - u[0]]).T
                s = np.linalg.svd(b @ np.linalg.inv(a), compute_uv=False)
                total += n.length
                if s[1] <= 0 or s[0] / s[1] > 1.2:
                    bad += n.length
    return bad / max(total, 1e-12)


def textured(objs):
    """A material with a 2 by 1 image texture on every slot."""
    mat = bpy.data.materials.new("wide texture")
    node = mat.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = bpy.data.images.new("wide", 2048, 1024)
    for o in objs:
        if not o.material_slots:
            o.data.materials.append(mat)
        for slot in o.material_slots:
            slot.material = mat
    return mat


objs = load(uv_mode="SURFACE", uv_normalize=True)
mat = textured(objs)
m._pack_uv_objects(objs, "OBJECT")
share = squashed(objs)
check(share < 0.01,
      "packing keeps the islands in proportion (%.1f%% squashed)"
      % (100.0 * share))
check(all(s.material == mat for o in objs for s in o.material_slots),
      "and every part keeps its material")

objs = load(uv_mode="CONFORMAL")
plain = squashed(objs)
textured(objs)
m._unwrap_uv_objects(objs, method="CONFORMAL")
share = squashed(objs)
check(share <= plain + 0.01,
      "Unwrap gives the same proportions as with no texture (%.1f%% "
      "squashed against %.1f%%)" % (100.0 * share, 100.0 * plain))

# ---- the setting reaches a refresh ---------------------------------------
print("\n== a refresh reproduces it")
objs = load(uv_pack="UDIM", uv_pack_tiles=3)
rec = str(objs[0].get("STEP_import_settings")
          or objs[0].get("import_record_json") or "")
check(all(k in rec for k in ("uv_pack", "uv_pack_tiles", "uv_pack_margin",
                            '"uv_mode": "CONFORMAL"')),
      "every packing setting is stamped on the object")

if FAILS:
    print("\nuv_pack_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_pack_smoke: OK - packed, filled, and nothing on top of anything")
