# SPDX-License-Identifier: GPL-3.0-or-later
"""A STEP file with a color out of range still imports.

    blender -b --factory-startup --python-exit-code 1 -P ci/step_colour_out_smoke.py

SolidWorks writes COLOUR_RGB('',-1,-1,-1) for "no color", and OCCT stops
the whole transfer on it with "Standard_OutOfRange: Color out". Three cam
samples of the corpus did not import (the corpus rigging test,
2026-09-27). The importer now reads a copy with those colors made
neutral, and keeps the colors that are in range.
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy  # noqa: E402

FAILS = []


def check(ok, text):
    print(("  ok   " if ok else "  FAIL ") + text)
    if not ok:
        FAILS.append(text)


bpy.ops.preferences.addon_enable(module="CADder")
src = os.path.join(_HERE, "baselines", "mat_color_ap242.step")
with open(src, encoding="latin-1", newline="") as fh:
    text = fh.read()
old = "COLOUR_RGB('',0.349190215138,0.583831500637,0.930924556504)"
check(old in text, "the fixture has the color to break")
text = text.replace(old, "COLOUR_RGB('',-1.,-1.,-1.)")
folder = tempfile.mkdtemp(prefix="cadder_colour_out_")
path = os.path.join(folder, "colour_out.step")
with open(path, "w", encoding="latin-1", newline="") as fh:
    fh.write(text)

before = set(bpy.data.objects)
try:
    res = bpy.ops.import_scene.occ_import_step(
        filepath=path, directory=folder + os.sep, files=[{"name": "colour_out.step"}])
    err = None
except RuntimeError as exc:
    res, err = {"CANCELLED"}, str(exc)
meshes = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
check(res == {"FINISHED"} and err is None,
      "the file with COLOUR_RGB(-1,-1,-1) imports: %s %s" % (res, err or ""))
check(len(meshes) > 0, "it gives %d mesh object(s)" % len(meshes))
# The color in range (a salmon) is kept on its solid, and the solid of the
# broken color is not given another hue.
colors = sorted(tuple(round(c, 2) for c in o.color[:3]) for o in meshes)
check(any(c[0] > 0.8 and c[1] < 0.4 for c in colors),
      "the color in range is kept: %s" % colors)
check(not any(c[2] > 0.7 and c[0] < 0.3 for c in colors),
      "the color out of range gives no blue: %s" % colors)
leftover = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("cadder_colors_")]
check(not leftover, "the neutral copy is removed after the import: %s" % leftover)
shutil.rmtree(folder, ignore_errors=True)

print("step_colour_out_smoke: %s" % ("FAIL: " + "; ".join(FAILS) if FAILS else "OK"))
sys.stdout.flush()
os._exit(1 if FAILS else 0)
