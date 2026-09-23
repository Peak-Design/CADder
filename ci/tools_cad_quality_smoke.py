# SPDX-License-Identifier: GPL-3.0-or-later
"""The Custom distance goes to the CAD application in meters.

    blender -b --factory-startup --python-exit-code 1 -P ci/tools_cad_quality_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

The Custom distance is a length property, so Blender holds it in scene
units. The mesh tools asked the CAD application for a part again with that
number as it was, and the CAD application takes meters: in a millimeter
scene, 0.8 mm went to it as 0.8 m. Rebuild from CAD already converted it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import bpy

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
from CADder import tools  # noqa: E402
from CADder import quality as quality_mod  # noqa: E402
from CADder.rig import cad_link  # noqa: E402

sent = []


def fake(ids, quality, **kwargs):
    sent.append(dict(quality))
    raise cad_link.CadLinkError("the test stops here")


cad_link.retessellate = fake

scene = bpy.context.scene
scene.unit_settings.scale_length = 0.001
st = scene.stepper
# A Custom quality with a distance of 0.8 scene units: 0.8 mm here.
spec = quality_mod.spec_of(st)
print("spec before:", spec)
for name in ("quality_preset", "mesh_quality", "quality"):
    if hasattr(st, name):
        try:
            setattr(st, name, "CUSTOM")
        except (TypeError, ValueError):
            pass
for name in ("lin_deflection", "lin_deflection_len"):
    if hasattr(st, name):
        setattr(st, name, 0.8)
spec = quality_mod.spec_of(st)
print("spec:", spec)
if spec.get("mode") != "physical" or spec.get("quality") in quality_mod.PRESETS:
    raise SystemExit("tools_cad_quality_smoke: FAIL: the test could not set "
                     "a Custom distance: %s" % spec)

me = bpy.data.meshes.new("part")
part = bpy.data.objects.new("part", me)
scene.collection.objects.link(part)
part["RIG_component_id"] = "c001"
tools._ask_cad_link(bpy.context, [part])

if not sent:
    raise SystemExit("tools_cad_quality_smoke: FAIL: nothing was asked for")
got = sent[0]
print("sent:", got)
value = next((got[k] for k in ("chord_m", "lin_m", "linear_m", "deflection_m")
              if k in got), None)
if value is None:
    raise SystemExit("tools_cad_quality_smoke: FAIL: no distance in %s" % got)
if abs(value - 0.0008) > 1e-9:
    raise SystemExit("tools_cad_quality_smoke: FAIL: 0.8 mm went to the CAD "
                     "application as %g m" % value)
print("tools_cad_quality_smoke: OK: 0.8 mm goes to the CAD application as "
      "0.0008 m")
