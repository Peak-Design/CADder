# UV layer helpers for STEPper NEXT.
#
# All import modes write a single "UVMap" layer (Blender's default name):
# the CAD parametric UVs extracted during tessellation are written by the
# mesh apply paths in main.py. This module provides the shared writer plus
# the triplanar box-projection generator that works on any finished
# Blender mesh.

import numpy as np


def write_uv_layer(me, name, loop_uvs):
    """Write a per-loop (L, 2) float array as a UV layer on mesh `me`."""
    if loop_uvs is None or len(loop_uvs) == 0 or len(me.loops) != len(loop_uvs):
        return False
    layer = me.uv_layers.get(name)
    if layer is None:
        layer = me.uv_layers.new(name=name, do_init=False)
    if layer is None:  # Blender's 8-UV-layer limit reached
        return False
    arr = np.asarray(loop_uvs, dtype=np.float32).reshape(-1, 2)
    try:
        # Blender 3.5+ float2 attribute accessor
        layer.uv.foreach_set("vector", arr.ravel())
    except AttributeError:
        layer.data.foreach_set("uv", arr.ravel())
    return True


def add_box_uv(me, scale=1.0, name="UVMap"):
    """Add a triplanar box-projected UV layer to a finished Blender mesh.

    Each face is projected along its dominant normal axis. UVs are the two
    remaining vertex coordinates divided by `scale` (world units per tile).
    """
    n_loops = len(me.loops)
    n_polys = len(me.polygons)
    if n_loops == 0 or n_polys == 0:
        return False

    verts = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)

    loop_vidx = np.empty(n_loops, dtype=np.int32)
    me.loops.foreach_get("vertex_index", loop_vidx)
    loop_pos = verts[loop_vidx]  # (L, 3)

    poly_normals = np.empty(n_polys * 3, dtype=np.float32)
    me.polygons.foreach_get("normal", poly_normals)
    poly_normals = poly_normals.reshape(-1, 3)

    loop_starts = np.empty(n_polys, dtype=np.int32)
    me.polygons.foreach_get("loop_start", loop_starts)
    loop_totals = np.empty(n_polys, dtype=np.int32)
    me.polygons.foreach_get("loop_total", loop_totals)

    # Per-loop face normal (expand by loop_total to support ngons)
    loop_face_n = np.repeat(poly_normals, loop_totals, axis=0)

    dominant = np.argmax(np.abs(loop_face_n), axis=1)  # (L,)
    sign = np.sign(loop_face_n[np.arange(n_loops), dominant])
    sign[sign == 0] = 1.0

    # Axis -> (u_axis, v_axis): X->(Y,Z), Y->(X,Z), Z->(X,Y)
    u_axis = np.where(dominant == 0, 1, 0)
    v_axis = np.where(dominant == 2, 1, 2)

    inv = 1.0 / scale if scale != 0 else 1.0
    li = np.arange(n_loops)
    u = loop_pos[li, u_axis] * inv
    v = loop_pos[li, v_axis] * inv
    # Flip U on negative-facing sides for projection continuity
    u = u * sign

    uvs = np.stack([u, v], axis=1).astype(np.float32)
    return write_uv_layer(me, name, uvs)


classes = ()
