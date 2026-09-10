# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed surfaces: does each seam mode do what its name says?

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_seam_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

A cylinder closes on itself, and CAD data marks no seam along that closure.
An unwrap therefore has nowhere to cut and returns a folded mess. Cutting
once opens the tube into one flat island. Cutting every boundary inside the
closed region opens it too, but leaves one island per patch instead of one.
"""
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


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    """Two drilled blocks: one hole is a single closed cylinder, the other is
    two half cylinders.

    Both turn up in real CAD. The closed one is opened by the parametric
    closure seam, which is a single line already. The two-piece one is the
    case the mode changes: it has two boundaries between its faces, and
    cutting both leaves two half shells where cutting one leaves a tube that
    unrolls whole.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    def drilled(tool_shape, name):
        block = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 60, 40, 20).Shape()
        return BRepAlgoAPI_Cut(block, tool_shape).Shape(), name

    axis = gp_Ax2(gp_Pnt(30, 20, -1), gp_Dir(0, 0, 1))
    whole = BRepPrimAPI_MakeCylinder(axis, 8, 22).Shape()

    # The same cylinder, cut in two by a slab, so the tool carries two
    # cylindrical faces and the hole inherits both.
    slab_a = BRepPrimAPI_MakeBox(gp_Pnt(30, 0, -2), 40, 60, 26).Shape()
    slab_b = BRepPrimAPI_MakeBox(gp_Pnt(-10, 0, -2), 40, 60, 26).Shape()
    half_a = BRepAlgoAPI_Cut(
        BRepPrimAPI_MakeCylinder(axis, 8, 22).Shape(), slab_a).Shape()
    half_b = BRepAlgoAPI_Cut(
        BRepPrimAPI_MakeCylinder(axis, 8, 22).Shape(), slab_b).Shape()
    split_tool = BRepAlgoAPI_Fuse(half_a, half_b).Shape()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for shape, name in (drilled(whole, "closed hole"),
                        drilled(split_tool, "two half hole")):
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_seam_")
STEP = os.path.join(tmp, "block.step")
write_step(STEP)


def load(mode):
    """Both parts, biggest first: [closed hole, two half hole] by face count
    is not reliable, so they come back keyed by name."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z",
                uv_mode="UNWRAP", uv_closed_seams=mode)
    out = {}
    for o in bpy.data.objects:
        if o.type != "MESH" or not len(o.data.polygons):
            continue
        key = "half" if "half" in o.name.lower() else "closed"
        out.setdefault(key, o)
    return out


def measure(obj):
    """(islands, seam edges, share of surface stretched more than 5x)."""
    me = obj.data
    flags = np.empty(len(me.edges), dtype=bool)
    me.edges.foreach_get("use_seam", flags)

    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    uvl = bm.loops.layers.uv.active
    seen, islands = set(), 0
    for face in bm.faces:
        if face.index in seen:
            continue
        islands += 1
        stack = [face]
        seen.add(face.index)
        while stack:
            f = stack.pop()
            for loop in f.loops:
                a, b = loop[uvl].uv, loop.link_loop_next[uvl].uv
                for other in loop.edge.link_faces:
                    if other.index in seen:
                        continue
                    for ol in other.loops:
                        oa, ob = ol[uvl].uv, ol.link_loop_next[uvl].uv
                        if (((oa - a).length < 1e-6 and (ob - b).length < 1e-6)
                                or ((oa - b).length < 1e-6
                                    and (ob - a).length < 1e-6)):
                            seen.add(other.index)
                            stack.append(other)
                            break
    bm.free()

    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    vidx = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("vertex_index", vidx)
    uv = np.empty(len(me.loops) * 2, dtype=np.float64)
    me.uv_layers.active.uv.foreach_get("vector", uv)
    uv = uv.reshape(-1, 2)
    tri = []
    for poly in me.polygons:
        idx = list(poly.loop_indices)
        for k in range(1, len(idx) - 1):
            tri.append((idx[0], idx[k], idx[k + 1]))
    tri = np.asarray(tri, dtype=np.int64)
    P, Q = co[vidx[tri]], uv[tri]
    e1, e2 = P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]
    f1, f2 = Q[:, 1] - Q[:, 0], Q[:, 2] - Q[:, 0]
    l1 = np.linalg.norm(e1, axis=1)
    nrm = np.cross(e1, e2)
    area = 0.5 * np.linalg.norm(nrm, axis=1)
    ok = (l1 > 1e-12) & (area > 1e-14)
    x = np.zeros_like(e1)
    x[ok] = e1[ok] / l1[ok, None]
    y = np.cross(nrm, x)
    ly = np.linalg.norm(y, axis=1)
    ok &= ly > 1e-12
    y[ok] /= ly[ok, None]
    A = np.zeros((len(tri), 2, 2))
    A[:, 0, 0] = l1
    A[:, 0, 1] = np.einsum("ij,ij->i", e2, x)
    A[:, 1, 1] = np.einsum("ij,ij->i", e2, y)
    det = A[:, 0, 0] * A[:, 1, 1]
    ok &= np.abs(det) > 1e-16
    a, d = A[ok], det[ok]
    Ainv = np.zeros((int(ok.sum()), 2, 2))
    Ainv[:, 0, 0] = a[:, 1, 1] / d
    Ainv[:, 0, 1] = -a[:, 0, 1] / d
    Ainv[:, 1, 1] = a[:, 0, 0] / d
    sv = np.linalg.svd(np.stack([f1, f2], axis=2)[ok] @ Ainv, compute_uv=False)
    ratio = sv[:, 0] / np.maximum(sv[:, 1], 1e-20)
    w = area[ok]
    return islands, int(flags.sum()), float(w[ratio > 5.0].sum() / w.sum())


got = {}
for mode in ("NONE", "SINGLE", "SPLIT"):
    parts = load(mode)
    got[mode] = {k: measure(o) for k, o in parts.items()}
    print("\n== %s" % mode)
    for k in sorted(got[mode]):
        isl, seams, bad = got[mode][k]
        print("   %-7s %d islands, %d seam edges, %.1f%% stretched over 5x"
              % (k, isl, seams, 100.0 * bad))

check(set(got["SINGLE"]) == {"closed", "half"},
      "both test parts imported (%s)" % sorted(got["SINGLE"]))

print("\n== leaving a closed surface alone does not work")
worst_none = max(v[2] for v in got["NONE"].values())
check(worst_none > 0.05,
      "without a seam the unwrap is badly distorted (%.1f%% over 5x)"
      % (100.0 * worst_none))

print("\n== one seam is enough to open the tube")
for key in ("closed", "half"):
    check(got["SINGLE"][key][2] < 0.02,
          "%s: a single seam flattens it (%.1f%% over 5x)"
          % (key, 100.0 * got["SINGLE"][key][2]))
    check(got["SPLIT"][key][2] < 0.02,
          "%s: seaming every boundary flattens it too (%.1f%% over 5x)"
          % (key, 100.0 * got["SPLIT"][key][2]))

print("\n== and it leaves the halves joined")
# A hole made of two half cylinders has two boundaries. Cutting one leaves
# the halves in one island, cutting both splits them.
check(got["SINGLE"]["half"][0] < got["SPLIT"]["half"][0],
      "the two-piece hole gives fewer islands with one seam (%d against %d)"
      % (got["SINGLE"]["half"][0], got["SPLIT"]["half"][0]))
check(got["SINGLE"]["half"][1] < got["SPLIT"]["half"][1],
      "and fewer seam edges (%d against %d)"
      % (got["SINGLE"]["half"][1], got["SPLIT"]["half"][1]))
# A hole that is one closed face already needs only its closure seam, so the
# two modes have nothing to argue about.
check(got["SINGLE"]["closed"][0] == got["SPLIT"]["closed"][0],
      "a hole that is one closed face is the same either way (%d islands)"
      % got["SINGLE"]["closed"][0])

if FAILS:
    print("\nuv_seam_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_seam_smoke: OK - one seam opens a hole and keeps it in one piece")
