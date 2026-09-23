# SPDX-License-Identifier: GPL-3.0-or-later
"""What does an import do with a shape that fails to tessellate?

    blender -b --factory-startup --python-exit-code 1 -P ci/precompute_fallback_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

The import tessellates every shape first (phase 1), and a shape that fails
there is tried once more when its object is built (phase 2). Two faults
met in that second try:

1. It had no guard. A shape that fails every time failed again, and the
   error left the import halfway: a Python error for the user, and every
   object made so far left in the scene at file units, not placed.
2. It dropped the relative flag, the part name and the instance color. A
   share of 0.005 was cut as 0.005 file units.

The test makes the tessellation of one part fail, first every time and
then only once.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy

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


def clean_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for col in list(bpy.data.collections):
        bpy.data.collections.remove(col)
    for me in list(bpy.data.meshes):
        bpy.data.meshes.remove(me)


STEP = os.path.join(_HERE, "fixtures", "assembly.step")
TARGET = "base"
_precompute = m.precompute_mesh_data


def failing(times):
    """A tessellation that fails `times` times for the shape of TARGET, and
    records every call for that shape."""
    bad, calls = [], []

    def spy(step_reader, shp, lind, angd, hacks, **kw):
        if kw.get("part_name") == TARGET and not any(shp is s for s in bad):
            bad.append(shp)
        if any(shp is s for s in bad):
            calls.append((lind, kw))
            if len(calls) <= times:
                raise RuntimeError("tessellation failed (test)")
        return _precompute(step_reader, shp, lind, angd, hacks, **kw)
    return spy, calls


# 1. The shape fails every time: the import finishes, and the part is
# reported as a part with no geometry.
clean_scene()
m.precompute_mesh_data, calls = failing(times=99)
try:
    result = m.load_step(bpy.context, STEP, htypes="EMPTIES", up_as="Z")
except Exception as exc:
    result = exc
finally:
    m.precompute_mesh_data = _precompute
check(isinstance(result, tuple),
      "the import finishes when one shape never tessellates (%r)" % (result,))
if isinstance(result, tuple):
    failed, _recovered = result
    check(TARGET in failed, "%s is reported as a failed part (%s)"
          % (TARGET, failed))
names = {o.get("STEP_name") for o in bpy.data.objects}
check("arm" in names and "pad" in names, "the other parts came in")
check(not any(o.type == "MESH" and len(o.data.vertices) == 0
              for o in bpy.data.objects),
      "no empty object is left for %s" % TARGET)
check(all(o.parent is not None for o in bpy.data.objects
          if o.get("STEP_name") in ("arm", "pad")),
      "the other parts are placed in the hierarchy")

# 2. The shape fails once: the second try cuts it the same way phase 1
# was asked to.
clean_scene()
m._cache_drop(STEP)
m.precompute_mesh_data, calls = failing(times=1)
try:
    m.load_step(bpy.context, STEP, htypes="EMPTIES", up_as="Z",
                deflection_spec=quality_mod.spec(None, relative=True,
                                                 relative_distance=0.005))
finally:
    m.precompute_mesh_data = _precompute
check(len(calls) == 2, "the shape was tried twice (%d)" % len(calls))
if len(calls) == 2:
    lind, kw = calls[1]
    check(kw.get("relative") is True,
          "the second try cuts the share %.4f as a share (relative=%r)"
          % (lind, kw.get("relative")))
    check(kw.get("part_name") == TARGET,
          "the second try has the part name (%r)" % kw.get("part_name"))
    check("fallback_color" in kw,
          "the second try has the instance color")
base = [o for o in bpy.data.objects
        if o.get("STEP_name") == TARGET and o.type == "MESH"]
check(len(base) == 1 and len(base[0].data.vertices) > 0,
      "%s came in on the second try" % TARGET)

if FAILS:
    print("\nprecompute_fallback_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nprecompute_fallback_smoke: OK")
