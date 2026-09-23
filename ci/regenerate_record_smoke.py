# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for what Regenerate keeps in the record of a part.

    blender -b --factory-startup --python-exit-code 1 -P ci/regenerate_record_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

Apply UVs and Apply Defeature rebuild a STEP part with the settings in its
record. The record must describe the mesh the part has now, so a part that
was regenerated at a different Mesh Quality keeps that quality when its UVs
change or when it is defeatured.
"""

import json
import os
import sys

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

STEP = os.path.join(_HERE, "fixtures", "holes.step")

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def load(**kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m = sys.modules["CADder.main"]
    m._cache_drop(STEP)
    opts = dict(htypes="FLAT", up_as="Z", tris_to_quads=False,
                uv_mode="SURFACE")
    opts.update(kw)
    m.load_step(bpy.context, STEP, **opts)
    bpy.context.view_layer.update()
    parts = [o for o in bpy.data.objects
             if o.type == "MESH" and "STEP_tag" in o]
    assert parts, "the fixture did not import"
    return parts[0]


def only(obj):
    for o in bpy.context.view_layer.objects:
        o.select_set(o == obj)
    bpy.context.view_layer.objects.active = obj


def record(obj):
    return json.loads(obj.get("STEP_import_settings", "{}"))


# ---- a new Mesh Quality survives Apply UVs and Apply Defeature -----------
print("\n== the quality of the last Regenerate stays")
part = load()
imported = len(part.data.polygons)
scene = bpy.context.scene.stepper
scene.quality_preset = "ULTRA"
scene.tessellation_relative = False
only(part)
bpy.ops.stepper.regenerate(use_scene_settings=True)
ultra = len(part.data.polygons)
check(ultra > imported,
      "Regenerate at Ultra makes a finer mesh (%d faces against %d)"
      % (ultra, imported))
rec = record(part)
check(abs(rec.get("ang_deflection", 0.0) - 0.1) < 1e-9,
      "the record now holds the angle of Ultra (%r)"
      % rec.get("ang_deflection"))

# The panel can change after the Regenerate. The part keeps what it has.
scene.quality_preset = "DRAFT"
scene.uv_mode = "SURFACE"
scene.uv_pack = "NONE"
only(part)
bpy.ops.stepper.reapply_uv()
check(len(part.data.polygons) == ultra,
      "Apply UVs keeps the Ultra mesh (%d faces against %d)"
      % (len(part.data.polygons), ultra))

part.cad_defeature.enabled = True
part.cad_defeature.size = 0.0001        # smaller than every hole
only(part)
bpy.ops.stepper.apply_defeature()
check(len(part.data.polygons) == ultra,
      "Apply Defeature keeps the Ultra mesh (%d faces against %d)"
      % (len(part.data.polygons), ultra))

only(part)
bpy.ops.stepper.regenerate(use_scene_settings=False)
check(len(part.data.polygons) == ultra,
      "and so does Regenerate with the settings of the part (%d faces)"
      % len(part.data.polygons))

# ---- Tris to quads is part of the quality --------------------------------
print("\n== the quads of the last Regenerate stay")
part = load()
tris = len(part.data.polygons)
scene = bpy.context.scene.stepper
scene.tris_to_quads = True
only(part)
bpy.ops.stepper.regenerate(use_scene_settings=True)
quads = len(part.data.polygons)
check(quads < tris, "Regenerate with quads pairs the triangles (%d against %d)"
      % (quads, tris))
check(record(part).get("tris_to_quads") is True,
      "the record says the part has quads")
scene.tris_to_quads = False
scene.uv_mode = "SURFACE"
scene.uv_pack = "NONE"
only(part)
bpy.ops.stepper.reapply_uv()
check(len(part.data.polygons) == quads,
      "Apply UVs keeps the quads (%d faces against %d)"
      % (len(part.data.polygons), quads))

if FAILS:
    print("\nregenerate_record_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nregenerate_record_smoke: OK: the record follows the mesh")
