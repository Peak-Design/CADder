# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the winding of box projected UVs.

    blender -b --factory-startup --python-exit-code 1 -P ci/uv_box_winding_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

On every side of a box the UV map must turn the same way as the face. A
side with a mirrored map shows text and decals reversed, and a normal map
lights it from the wrong side. The Y sides were mirrored once, because U
ran along X on both of them while V ran along Z.
"""

import os
import sys

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import uv as uv_mod  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def signed_area(me, poly):
    """The area of a face in the UV map, positive when the map turns the
    same way as the face."""
    layer = me.uv_layers["UVMap"]
    points = [layer.uv[i].vector for i in poly.loop_indices]
    total = 0.0
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        total += a.x * b.y - b.x * a.y
    return total * 0.5


def side(normal):
    axis = max(range(3), key=lambda i: abs(normal[i]))
    return ("+" if normal[axis] > 0 else "-") + "XYZ"[axis]


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube = bpy.context.active_object
me = cube.data
check(uv_mod.add_box_uv(me, 1.0), "the cube gets a box projection")
for poly in me.polygons:
    area = signed_area(me, poly)
    check(area > 0.0, "side %s turns with its face (UV area %+.1f)"
          % (side(poly.normal), area))

# A face that leans still takes the side it faces most.
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2.0, rotation=(0.3, 0.2, 0.1))
bpy.ops.object.transform_apply(rotation=True)
me = bpy.context.active_object.data
uv_mod.add_box_uv(me, 1.0)
check(all(signed_area(me, poly) > 0.0 for poly in me.polygons),
      "a turned cube turns with its faces on every side")

if FAILS:
    print("\nuv_box_winding_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nuv_box_winding_smoke: OK: no side of a box is mirrored")
