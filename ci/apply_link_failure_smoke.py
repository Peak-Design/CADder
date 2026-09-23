# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for Apply UVs and Apply Defeature when the CAD application
cannot be reached.

    blender -b --factory-startup --python-exit-code 1 -P ci/apply_link_failure_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

A scope can hold parts from a STEP file and parts from the live link. When
the CAD application is closed:

- The user sees why, in the message of the operator, not only in the
  system console.
- The parts from the STEP file are still rebuilt, because they do not need
  the CAD application.
- The UV record of a part changes only when the part got the new UVs, so a
  later Regenerate does not make a map that the user never saw.

No SolidWorks here. The link is made to fail as it fails when no CAD
application is found.
"""

import importlib
import json
import os
import sys

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

STEP = os.path.join(_HERE, "fixtures", "holes.step")
CLOSED = "No CAD application was found (smoke)"

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def load():
    """A STEP part, and a mesh that says it came over the live link."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m = sys.modules["CADder.main"]
    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z",
                tris_to_quads=False, uv_mode="SURFACE", uv_normalize=True)
    bpy.context.view_layer.update()
    part = next(o for o in bpy.data.objects
                if o.type == "MESH" and "STEP_tag" in o)
    bpy.ops.mesh.primitive_cube_add(size=0.02, location=(0.3, 0.0, 0.0))
    live = bpy.context.active_object
    live.name = "live part"
    live["RIG_component_id"] = "c001"

    # The link fails the way it fails with SolidWorks closed. The module is
    # loaded again with the addon, so the patch goes on the live one.
    cad_link = importlib.import_module("CADder.rig.cad_link")

    def closed(*_args, **_kw):
        raise cad_link.CadLinkError(CLOSED)

    cad_link.first = closed
    return part, live


def select(*objs):
    for o in bpy.context.view_layer.objects:
        o.select_set(o in objs)
    bpy.context.view_layer.objects.active = objs[0]


def run(op):
    """The error text the operator reported, or "" when it reported none."""
    try:
        op()
    except RuntimeError as exc:
        return str(exc)
    return ""


def record(obj):
    return json.loads(obj.get("STEP_import_settings", "{}"))


def uvs(obj):
    return [tuple(d.uv) for d in obj.data.uv_layers.active.data]


# ---- Apply Defeature ------------------------------------------------------
print("\n== Apply Defeature with the CAD application closed")
part, live = load()
faces = len(part.data.polygons)
select(part, live)
said = run(bpy.ops.stepper.apply_defeature)
check(CLOSED in said,
      "the user is told why the live part did not change (%r)" % said)
check(len(part.data.polygons) < faces,
      "the part from the STEP file is still defeatured (%d faces against %d)"
      % (len(part.data.polygons), faces))

# ---- Apply UVs ------------------------------------------------------------
print("\n== Apply UVs with the CAD application closed")
part, live = load()
before = uvs(part)
live_before = uvs(live)
scene = bpy.context.scene.stepper
scene.uv_mode = "SMART"
scene.uv_pack = "NONE"
select(part, live)
said = run(bpy.ops.stepper.reapply_uv)
check(CLOSED in said,
      "the user is told why the live part did not change (%r)" % said)
check(uvs(part) != before, "the part from the STEP file got its new UVs")
check(record(part).get("uv_mode") == "SMART",
      "and its record says SMART")
check(uvs(live) == live_before, "the live part keeps its UVs")
check(record(live).get("uv_mode") != "SMART",
      "and its record does not say SMART (%r)" % record(live).get("uv_mode"))

# ---- the live part gets the record when its geometry came back ----------
print("\n== Apply UVs when the CAD application answers")
part, live = load()
tools = sys.modules["CADder.tools"]
real_ask = tools._ask_cad_link
tools._ask_cad_link = lambda context, objs: (len(objs), None)
scene = bpy.context.scene.stepper
scene.uv_mode = "ANGLE_BASED"
scene.uv_pack = "NONE"
select(live)
said = run(bpy.ops.stepper.reapply_uv)
tools._ask_cad_link = real_ask
check(said == "", "no error (%r)" % said)
check(record(live).get("uv_mode") == "ANGLE_BASED",
      "the live part records the mode it got")

# ---- a part whose rebuild failed keeps its record -----------------------
print("\n== a part that could not be rebuilt keeps its record")
part, live = load()
part["STEP_tag"] = "no such tag"
scene = bpy.context.scene.stepper
scene.uv_mode = "SMART"
scene.uv_pack = "NONE"
select(part)
run(bpy.ops.stepper.reapply_uv)
check(record(part).get("uv_mode") == "SURFACE",
      "the record still says SURFACE (%r)" % record(part).get("uv_mode"))

# ---- a mesh that did not come from CAD ----------------------------------
print("\n== a plain mesh gets no import record")
part, live = load()
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 2.0))
cube = bpy.context.active_object
scene = bpy.context.scene.stepper
scene.uv_mode = "SMART"
select(cube)
said = run(bpy.ops.stepper.reapply_uv)
check("STEP_import_settings" not in cube,
      "a mode that needs CAD data leaves no record on it")
scene.uv_mode = "BOX"
select(cube)
said = run(bpy.ops.stepper.reapply_uv)
check(said == "", "Box Project works on it (%r)" % said)
check("STEP_import_settings" not in cube,
      "and it leaves no record on it either")

if FAILS:
    print("\napply_link_failure_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\napply_link_failure_smoke: OK: a closed CAD application is "
      "reported and the STEP parts still change")
