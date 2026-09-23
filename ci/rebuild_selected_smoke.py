# SPDX-License-Identifier: GPL-3.0-or-later
"""Does Rebuild Selected Objects rebuild each object from its own shape, at
the quality the Mesh Quality panel asks for?

    blender -b --factory-startup --python-exit-code 1 -P ci/rebuild_selected_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

The fixture is one part with three bodies, imported with Separate Solids,
so each body is its own object. The bodies carry the tag of the part.
Three faults met here:

1. The operator found the shape by tag, so a body was rebuilt from the
   whole part: every body merged into it.
2. It rebuilt once per tag, so with all three bodies selected only the
   first was rebuilt.
3. With Relative Tessellation on in the Mesh Quality panel, it passed the
   share (0.005) as a distance in file units, so the mesh was cut at
   0.005 mm.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy
import numpy as np

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, quality as quality_mod  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def extent(obj):
    co = np.empty(len(obj.data.vertices) * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    return tuple(round(float(v), 4) for v in co.max(axis=0) - co.min(axis=0))


def same_size(a, b):
    """A body cut finer is a little rounder, so allow a few percent. A body
    rebuilt from the whole part is several times larger."""
    return bool(np.allclose(a, b, rtol=0.05))


STEP = os.path.join(_HERE, "fixtures", "multisolid.step")
m.load_step(bpy.context, STEP, htypes="EMPTIES", separate_solids=True,
            deflection_spec=quality_mod.spec("BALANCED"))

bodies = sorted((o for o in bpy.data.objects
                 if o.type == "MESH" and ".body" in o.get("STEP_name", "")),
                key=lambda o: o["STEP_name"])
check(len(bodies) == 3, "the import made three bodies (%d)" % len(bodies))
size = {o.name: extent(o) for o in bodies}

# Record every tessellation the operator asks for.
calls = []
_precompute = m.precompute_mesh_data


def spy(step_reader, shp, lind, angd, hacks, **kw):
    calls.append((lind, angd, kw))
    return _precompute(step_reader, shp, lind, angd, hacks, **kw)


m.precompute_mesh_data = spy

stepper = bpy.context.scene.stepper
stepper.tessellation_relative = True
stepper.lin_deflection_rel = 0.005

# 1. One body alone keeps its own shape.
for o in bpy.data.objects:
    o.select_set(False)
body = bodies[1]
body.select_set(True)
bpy.context.view_layer.objects.active = body
bpy.ops.object.occ_rebuild_selected()
check(same_size(extent(body), size[body.name]),
      "%s keeps its own size %s (now %s)"
      % (body.name, size[body.name], extent(body)))

# 3. The panel's relative share goes through as a share.
check(len(calls) == 1, "one tessellation for one body (%d)" % len(calls))
if calls:
    lind, _angd, kw = calls[0]
    check(kw.get("relative") is True,
          "the share %.4f is cut as a share (relative=%r)"
          % (lind, kw.get("relative")))
    check("fallback_color" in kw,
          "the instance color of the node goes through")

# 2. All three bodies selected: all three are rebuilt, each as itself.
calls.clear()
for o in bodies:
    o.select_set(True)
bpy.ops.object.occ_rebuild_selected()
check(len(calls) == 3, "three bodies, three tessellations (%d)" % len(calls))
for o in bodies:
    check(same_size(extent(o), size[o.name]),
          "%s keeps its own size %s (now %s)"
          % (o.name, size[o.name], extent(o)))

# 4. A part from a plain import (no Separate Solids) is still rebuilt
# from its whole shape.
ASSEMBLY = os.path.join(_HERE, "fixtures", "assembly.step")
m.load_step(bpy.context, ASSEMBLY, htypes="EMPTIES",
            deflection_spec=quality_mod.spec("BALANCED"))
part = next(o for o in bpy.data.objects
            if o.type == "MESH" and o.get("STEP_file") == ASSEMBLY)
part_size = extent(part)
for o in bpy.data.objects:
    o.select_set(False)
part.select_set(True)
bpy.context.view_layer.objects.active = part
calls.clear()
bpy.ops.object.occ_rebuild_selected()
check(len(calls) == 1, "a plain part is rebuilt (%d)" % len(calls))
check(same_size(extent(part), part_size),
      "%s keeps its size %s (now %s)" % (part.name, part_size, extent(part)))

m.precompute_mesh_data = _precompute

if FAILS:
    print("\nrebuild_selected_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nrebuild_selected_smoke: OK")
