# SPDX-License-Identifier: GPL-3.0-or-later
"""CAD Surface UVs: does a cylinder arrive with the proportions it has?

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_surface_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

What went wrong before
----------------------
OCC hands back raw surface parameters. On a plane they are lengths, but on a
cylinder u is an angle in radians and v is a length. A drum 40 mm across and
500 mm tall therefore measured 6.28 by 500 in parameter space, and
normalizing those two together squashed the island into a thin ribbon: the
texture came out smeared around the drum and squeezed along it.

The fix scales each direction by the length of its own derivative, so u
becomes arc length. This test builds a cylinder whose right answer is known
by hand and checks the island against it.
"""
import math
import os
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m

FAILS = []

RADIUS = 20.0
HEIGHT = 500.0


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """One tall thin cylinder: the shape the old code got most wrong."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    shape = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), RADIUS, HEIGHT).Shape()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    label = tool.AddShape(shape, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString("drum"))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_uvsurf_")
STEP = os.path.join(tmp, "drum.step")
write_step(STEP)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
m._cache_drop(STEP)
m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z", uv_mode="SURFACE")
obj = sorted([o for o in bpy.data.objects
              if o.type == "MESH" and len(o.data.polygons)],
             key=lambda o: -len(o.data.polygons))[0]

me = obj.data
co = np.empty(len(me.vertices) * 3, dtype=np.float64)
me.vertices.foreach_get("co", co)
co = co.reshape(-1, 3)
vidx = np.empty(len(me.loops), dtype=np.int32)
me.loops.foreach_get("vertex_index", vidx)
uv = np.empty(len(me.loops) * 2, dtype=np.float64)
me.uv_layers.active.uv.foreach_get("vector", uv)
uv = uv.reshape(-1, 2)

print("\n== the shape reached Blender")
check(len(me.polygons) > 0, "the drum has faces (%d)" % len(me.polygons))
check(len(me.uv_layers) == 1, "there is one UV layer")

print("\n== the curved wall keeps its proportions")
# A cylinder tessellates with vertices only on its two rims, so the wall has
# to be picked out by face, not by vertex height. A wall face has its normal
# across the axis, an end cap has its normal along it. The wall island should
# measure circumference by height.
# The importer scales the mesh into scene units, so measure the drum as it
# arrived rather than assuming millimetres.
z_all = co[:, 2]
height = float(z_all.max() - z_all.min())
r_max = float(np.linalg.norm(co[:, :2], axis=1).max())
wall_loops = [i for poly in me.polygons if abs(poly.normal.z) < 0.1
              for i in poly.loop_indices]
check(len(wall_loops) > 0, "found the wall faces (%d loops of %d)"
      % (len(wall_loops), len(me.loops)))

wall_uv = uv[np.asarray(wall_loops, dtype=np.int64)]
span_u = float(wall_uv[:, 0].max() - wall_uv[:, 0].min())
span_v = float(wall_uv[:, 1].max() - wall_uv[:, 1].min())
got = max(span_u, span_v) / max(min(span_u, span_v), 1e-12)
want = height / (2.0 * math.pi * r_max)
check(abs(got - want) / want < 0.10,
      "the island is %.2f to 1, and height over circumference is %.2f to 1"
      % (got, want))

print("\n== nothing is stretched")
# Per triangle, the ratio of the two singular values of the map from the
# triangle's own plane into UV. 1.0 means the triangle keeps its shape.
tri = []
for poly in me.polygons:
    idx = list(poly.loop_indices)
    for k in range(1, len(idx) - 1):
        tri.append((idx[0], idx[k], idx[k + 1]))
tri = np.asarray(tri, dtype=np.int64)
P = co[vidx[tri]]
Q = uv[tri]
e1, e2 = P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]
f1, f2 = Q[:, 1] - Q[:, 0], Q[:, 2] - Q[:, 0]
l1 = np.linalg.norm(e1, axis=1)
n = np.cross(e1, e2)
area = 0.5 * np.linalg.norm(n, axis=1)
ok = (l1 > 1e-12) & (area > 1e-12)
x = np.zeros_like(e1)
x[ok] = e1[ok] / l1[ok, None]
y = np.cross(n, x)
ly = np.linalg.norm(y, axis=1)
ok &= ly > 1e-12
y[ok] /= ly[ok, None]
A = np.zeros((len(tri), 2, 2))
A[:, 0, 0] = l1
A[:, 0, 1] = np.einsum("ij,ij->i", e2, x)
A[:, 1, 1] = np.einsum("ij,ij->i", e2, y)
det = A[:, 0, 0] * A[:, 1, 1]
ok &= np.abs(det) > 1e-16
Ainv = np.zeros((int(ok.sum()), 2, 2))
a, d = A[ok], det[ok]
Ainv[:, 0, 0] = a[:, 1, 1] / d
Ainv[:, 0, 1] = -a[:, 0, 1] / d
Ainv[:, 1, 1] = a[:, 0, 0] / d
sv = np.linalg.svd(np.stack([f1, f2], axis=2)[ok] @ Ainv, compute_uv=False)
ratio = sv[:, 0] / np.maximum(sv[:, 1], 1e-20)
w = area[ok]
order = np.argsort(ratio)
cum = np.cumsum(w[order]) / w.sum()
median = float(ratio[order][np.searchsorted(cum, 0.5)])
bad = float(w[ratio > 2.0].sum() / w.sum())
check(median < 1.2,
      "the median triangle keeps its shape (%.2f, was about 157 before)"
      % median)
check(bad < 0.02,
      "almost none of the surface is stretched twice as far one way as the "
      "other (%.1f%%)" % (100.0 * bad))

if FAILS:
    print("\nuv_surface_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_surface_smoke: OK - a cylinder unwraps to circumference by height")
