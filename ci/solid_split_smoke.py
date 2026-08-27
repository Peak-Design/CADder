"""Does "separate solids" give every body of a multibody part its own object,
covering exactly the same geometry as the merged import?

The invariant is split-vs-unsplit, which is frame independent: the bodies
together must have the same vertex count and the same bounding box as the one
object the option-off import makes, and each body must be disjoint from the
others (the fixture's three solids are).

    blender -b --factory-startup -P ci/solid_split_smoke.py

The fixture is ci/fixtures/multisolid.step: three disjoint bodies in ONE
product with no assembly structure. ci/fixtures/make_multisolid.py
rebuilds it, and the flag that matters there is
XCAFDoc_ShapeTool.AddShape(..., False) - without it the reader hands
back one node per solid and there is nothing to separate.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON_DIR = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ADDON_DIR)
STEP = os.path.join(_HERE, "fixtures", "multisolid.step")

import bpy
bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import main as m

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def fresh():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for col in list(bpy.data.collections):
        bpy.data.collections.remove(col)
    for me in list(bpy.data.meshes):
        bpy.data.meshes.remove(me)
    vl = bpy.context.view_layer
    vl.active_layer_collection = vl.layer_collection


def imported(htypes, split):
    fresh()
    m.load_step(bpy.context, STEP, htypes=htypes, up_as="Z",
                apply_scale=True, separate_solids=split)
    return [o for o in bpy.data.objects
            if o.type == "MESH" and o.get("STEP_file")]


def box(objs):
    """Bounding box over the objects' own mesh coordinates, which the split
    must not disturb: every body keeps the place it had inside the part."""
    pts = [v.co for o in objs for v in o.data.vertices]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def overlaps(a, b):
    return all(a[0][i] < b[1][i] - 1e-9 and b[0][i] < a[1][i] - 1e-9
               for i in range(3))


for htypes in ("EMPTIES", "TREE", "FLAT"):
    merged = imported(htypes, False)
    m_verts = sum(len(o.data.vertices) for o in merged)
    m_box = box(merged)

    bodies = imported(htypes, True)
    b_verts = sum(len(o.data.vertices) for o in bodies)

    print("\n== %s: merged %d object(s)/%d verts -> split %d object(s)/%d verts"
          % (htypes, len(merged), m_verts, len(bodies), b_verts))
    check(len(merged) == 1, "one merged object with the option off")
    check(len(bodies) == 3, "one object per body with it on")
    if len(bodies) != 3:
        continue

    check(len({o.data.name for o in bodies}) == 3,
          "each body has its own mesh datablock")
    check(all(len(o.data.polygons) > 0 for o in bodies),
          "every body has faces")
    check(b_verts == m_verts,
          "no geometry gained or lost (%d vs %d)" % (b_verts, m_verts))

    b_box = box(bodies)
    same = all(abs(b_box[k][i] - m_box[k][i]) < 1e-9
               for k in (0, 1) for i in range(3))
    check(same, "the bodies together occupy the merged object's box\n"
                "        merged %s\n        split  %s" % (m_box, b_box))

    boxes = [box([o]) for o in bodies]
    pairs = [(i, j) for i in range(3) for j in range(i + 1, 3)
             if overlaps(boxes[i], boxes[j])]
    check(not pairs, "the three bodies are separated, not copies (%s)" % pairs)

    check(all(o.users_collection for o in bodies), "no body left unlinked")
    check(len({o.name for o in bodies}) == 3, "the bodies have distinct names")

if FAILS:
    print("\nsolid_split_smoke: FAILED\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nsolid_split_smoke: OK - 3 bodies covering exactly the merged "
      "import, in every hierarchy mode")
