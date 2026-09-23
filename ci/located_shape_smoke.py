# SPDX-License-Identifier: GPL-3.0-or-later
"""A shape that carries its own location must import in place, with
normals that match its faces, on both mesh paths.

    blender -b --factory-startup --python-exit-code 1 -P ci/located_shape_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

A face can hold a location: a BREP file whose top shape is moved or
turned, or a body of a multibody part that is placed by a location
(mapped or patterned bodies, with Separate Solids on). The native module
puts the mesh nodes in that located frame, and the surface normals are
in that frame too. Two faults came from this:

  1. The native path turned the normals back by the inverse of the shape
     location. On a turned shape the shading normals then pointed the
     wrong way for most faces.

  2. The Python fallback did not apply the location to the nodes. A moved
     body was drawn on top of the first one, and a turned shape came in
     unturned.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, importer

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def _loc(trsf):
    from OCP.TopLoc import TopLoc_Location
    return TopLoc_Location(trsf)


def turned():
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf
    t = gp_Trsf()
    t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 3.141592653589793 / 2)
    return _loc(t)


def moved(dx):
    from OCP.gp import gp_Trsf, gp_Vec
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(dx, 0, 0))
    return _loc(t)


def box():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    return BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()


def write_brep(path, shape):
    from OCP.BRepTools import BRepTools
    BRepTools.Write_s(shape, path)
    return path


def compound(*shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    b = BRep_Builder()
    c = TopoDS_Compound()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def import_file(path, **kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(path)
    m.load_step(bpy.context, path, htypes="FLAT", up_as="Z",
                custom_scale=1.0, tris_to_quads=False, **kw)
    bpy.context.view_layer.update()
    return sorted((o for o in bpy.data.objects
                   if o.type == "MESH" and len(o.data.polygons)),
                  key=lambda o: o.name)


def wrong_normals(objs):
    """Triangles with a corner normal that points away from the triangle."""
    bad = total = 0
    for obj in objs:
        me = obj.data
        for poly in me.polygons:
            total += 1
            if any(me.corner_normals[li].vector.dot(poly.normal) < 0.5
                   for li in poly.loop_indices):
                bad += 1
    return bad, total


def extent(objs):
    pts = [o.matrix_world @ v.co for o in objs for v in o.data.vertices]
    return tuple(round(max(p[k] for p in pts) - min(p[k] for p in pts), 4)
                 for k in range(3))


def x_range(objs):
    xs = [(o.matrix_world @ v.co).x for o in objs for v in o.data.vertices]
    return round(min(xs), 4), round(max(xs), 4)


tmp = tempfile.mkdtemp(prefix="cadder_located_")
TURNED = write_brep(os.path.join(tmp, "turned.brep"),
                    box().Located(turned()))
MOVED = write_brep(os.path.join(tmp, "moved.brep"),
                   compound(box(), box().Located(moved(100.0))))
BODIES = write_brep(os.path.join(tmp, "bodies.brep"),
                    compound(box(), box().Located(turned())))

native = importer._HAS_NATIVE
check(native, "the native module is loaded")
for path_name, use_native in (("native module", True),
                              ("Python fallback", False)):
    importer._HAS_NATIVE = native and use_native
    try:
        print("\n== %s: a turned shape" % path_name)
        objs = import_file(TURNED)
        # 1 file unit is 1 m here, so the extent is in file units.
        check(extent(objs) == (20.0, 10.0, 30.0),
              "%s: the shape comes in turned (%s)" % (path_name,
                                                     extent(objs)))
        bad, total = wrong_normals(objs)
        check(total == 12 and bad == 0,
              "%s: the normals match the faces (%d of %d wrong)"
              % (path_name, bad, total))

        print("\n== %s: a moved body" % path_name)
        objs = import_file(MOVED)
        check(x_range(objs) == (0.0, 110.0),
              "%s: the moved body is in its place (x %s)"
              % (path_name, x_range(objs)))

        print("\n== %s: Separate Solids with a turned body" % path_name)
        objs = import_file(BODIES, separate_solids=True)
        bad, total = wrong_normals(objs)
        check(len(objs) == 2 and total == 24 and bad == 0,
              "%s: the normals of both bodies match their faces "
              "(%d of %d wrong, %d objects)"
              % (path_name, bad, total, len(objs)))
    finally:
        importer._HAS_NATIVE = native

if FAILS:
    print("\nlocated_shape_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nlocated_shape_smoke: OK: located shapes import in place with "
      "matching normals on both mesh paths")
