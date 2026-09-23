# SPDX-License-Identifier: GPL-3.0-or-later
"""UV pack and unwrap on a mesh that is not in the view layer.

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_prototype_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

A collection-instance import keeps each part as a prototype in an excluded
components collection, and the live link does the same. The UV panel
hands those prototypes to the pack and to the unwrap. Both select the
objects and go into edit mode, and an object outside the view layer cannot
be selected. The error went to the console, the UVs stayed as they were,
and the panel reported the work as done.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bmesh
import bpy
import numpy as np

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def uvs(me):
    a = np.empty(len(me.loops) * 2, dtype=np.float64)
    me.uv_layers["UVMap"].uv.foreach_get("vector", a)
    return a.reshape(-1, 2)


def prototype():
    """A cylinder in an excluded collection, and an empty that shows it."""
    me = bpy.data.meshes.new("part")
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=1.0,
                          radius2=1.0, depth=3.0, calc_uvs=True)
    for e in bm.edges:
        # One seam down the side and round both caps, so it unrolls.
        if (e.verts[0].co.z * e.verts[1].co.z < 0
                and abs(e.verts[0].co.y) < 1e-6 and e.verts[0].co.x > 0):
            e.seam = True
        if abs(e.verts[0].co.z - e.verts[1].co.z) < 1e-6:
            e.seam = True
    bm.to_mesh(me)
    bm.free()
    if not me.uv_layers:
        me.uv_layers.new(name="UVMap")
    me.uv_layers[0].name = "UVMap"
    # Every UV in one small corner, so any real unwrap or pack moves them.
    a = uvs(me) * 0.05 + 0.3
    me.uv_layers["UVMap"].uv.foreach_set("vector", a.ravel())
    mat = bpy.data.materials.new("part material")
    me.materials.append(mat)

    comp = bpy.data.collections.new("part.components")
    bpy.context.scene.collection.children.link(comp)
    proto = bpy.data.objects.new("part", me)
    comp.objects.link(proto)
    bpy.context.view_layer.layer_collection.children[comp.name].exclude = True
    inst = bpy.data.objects.new("part.001", None)
    inst.instance_type = "COLLECTION"
    inst.instance_collection = comp
    bpy.context.scene.collection.objects.link(inst)
    return proto, mat


def state(proto, mat):
    return (len(bpy.data.objects),
            [c.name for c in proto.users_collection],
            [s.material for s in proto.material_slots] == [mat])


print("\n== unwrap")
proto, mat = prototype()
try:
    proto.select_set(True)
    outside = False
except RuntimeError:
    outside = True
check(outside, "the prototype is outside the view layer")
before = uvs(proto.data)
was = state(proto, mat)
m._unwrap_uv_objects([proto], method="CONFORMAL")
after = uvs(proto.data)
moved = float(np.abs(after - before).max())
check(moved > 0.01, "the unwrap changed the UVs (moved up to %.3f)" % moved)
check(state(proto, mat) == was,
      "no object left behind, the prototype stays in its collection")

print("\n== pack")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
m = sys.modules["CADder.main"]
proto, mat = prototype()
was = state(proto, mat)
m._pack_uv_objects([proto], "ALL")
after = uvs(proto.data)
span = after.max(axis=0) - after.min(axis=0)
check(span.max() > 0.5,
      "the pack filled the tile (the islands span %.3f)" % span.max())
check(state(proto, mat) == was,
      "no object left behind, the prototype keeps its collection and material")

if FAILS:
    print("\nuv_prototype_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_prototype_smoke: OK")
