# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the size of the empties an import makes.

The empties used to be drawn 2 m across whatever the file, which buries a
small assembly under long lines. Each must now be a tenth of the diagonal
of the parts under it, in the world, whatever scale the import applied.

Run:  blender -b --factory-startup -P empty_size_smoke.py
"""

import os
import sys

import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import empties, main as m  # noqa: E402

STEP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fixtures", "assembly.step")


def check(ok, what):
    if not ok:
        raise SystemExit("empty_size_smoke: FAILED: " + what)


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(STEP)


def world_box(objs):
    pts = []
    for o in objs:
        if o.type == "MESH":
            mw = np.array(o.matrix_world)
            box = np.array([tuple(c) for c in o.bound_box])
            pts.append(box @ mw[:3, :3].T + mw[:3, 3])
        elif o.type == "EMPTY" and o.instance_collection is not None:
            pts.append(world_box_instance(o))
    pts = np.vstack(pts)
    return float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))


def world_box_instance(o):
    mw = np.array(o.matrix_world)
    out = []
    for p in o.instance_collection.all_objects:
        if p.type != "MESH":
            continue
        pm = np.array(p.matrix_world)
        box = np.array([tuple(c) for c in p.bound_box]) @ pm[:3, :3].T + pm[:3, 3]
        out.append(box @ mw[:3, :3].T + mw[:3, 3])
    return np.vstack(out)


def tree_empties():
    return [o for o in bpy.data.objects
            if o.type == "EMPTY" and o.instance_collection is None]


for mode in ("EMPTIES", "COLLECTION_INSTANCES"):
    clean()
    m.load_step(bpy.context, STEP, htypes=mode, up_as="Z")
    bpy.context.view_layer.update()
    tree = tree_empties()
    check(tree, "%s: the import made no empties" % mode)
    everything = world_box([o for o in bpy.data.objects
                            if o.type == "MESH" and o.users_collection
                            and not o.hide_get()
                            or (o.type == "EMPTY" and o.instance_collection)])
    sized = 0
    for e in tree:
        under = [c for c in e.children_recursive
                 if c.type == "MESH" or c.instance_collection is not None]
        drawn = e.empty_display_size * max(e.matrix_world.to_scale())
        check(drawn < everything * 0.5,
              "%s: %s is drawn %.4g across a model of %.4g"
              % (mode, e.name, drawn, everything))
        if not under:
            continue
        want = world_box(under) * empties.FRACTION
        check(abs(drawn - want) <= want * 1e-3,
              "%s: %s is %.6g, the parts under it want %.6g"
              % (mode, e.name, drawn, want))
        sized += 1
    check(sized, "%s: no empty had parts under it" % mode)

print("empty_size_smoke: OK: every empty of an EMPTIES and a "
      "COLLECTION_INSTANCES import is a tenth of the parts under it")
