# Free-edge (sketch / construction curve) import for STEPper NEXT.
#
# Edges that belong to no face are invisible in the mesh pipeline. this
# module samples them into polyline data that main.py turns into Blender
# curve objects.
#
# OCP note: free-edge detection uses ShapeKey wrapper-identity sets, NOT
# OCCT map lookups (per-entity handle lookups are broken in OCP 7.9.3.1).

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

try:
    from .ocp_utils import ShapeKey
except ImportError:  # standalone use outside the package (test harnesses)
    from ocp_utils import ShapeKey


def extract_free_curves(shape, lin_def=0.8, ang_def=0.5):
    """Sample all free edges (edges not owned by any face) of `shape`.

    Returns a list of point lists [[(x, y, z), ...], ...] in file units.
    Sampling density follows the import deflection parameters.
    """
    owned = set()
    fex = TopExp_Explorer(shape, TopAbs_FACE)
    while fex.More():
        eex = TopExp_Explorer(fex.Current(), TopAbs_EDGE)
        while eex.More():
            owned.add(ShapeKey(eex.Current()))
            eex.Next()
        fex.Next()

    polylines = []
    seen = set()
    ex = TopExp_Explorer(shape, TopAbs_EDGE)
    while ex.More():
        key = ShapeKey(ex.Current())
        if key not in owned and key not in seen:
            seen.add(key)
            edge = TopoDS.Edge_s(ex.Current())
            pts = _sample_edge(edge, lin_def, ang_def)
            if len(pts) >= 2:
                polylines.append(pts)
        ex.Next()
    return polylines


def _sample_edge(edge, lin_def, ang_def):
    try:
        adaptor = BRepAdaptor_Curve(edge)
        sampler = GCPnts_TangentialDeflection(adaptor, ang_def, lin_def, 2)
        n = sampler.NbPoints()
        pts = []
        for i in range(1, n + 1):
            p = sampler.Value(i)
            pts.append((p.X(), p.Y(), p.Z()))
        return pts
    except Exception:
        return []


def build_curve_object(bpy, name, polylines):
    """Create a Blender curve object holding one poly spline per polyline."""
    cu = bpy.data.curves.new(name, type="CURVE")
    cu.dimensions = "3D"
    for pts in polylines:
        spline = cu.splines.new("POLY")
        spline.points.add(len(pts) - 1)
        flat = [c for p in pts for c in (*p, 1.0)]
        spline.points.foreach_set("co", flat)
    return bpy.data.objects.new(name, cu)


classes = ()
