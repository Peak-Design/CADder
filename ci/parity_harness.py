"""Headless parity harness for CADder.

Usage:
  blender.exe -b --factory-startup --python parity_harness.py -- \
      <file.step> <out.json> [<operator_kwargs_json>]

Imports the STEP file with the CADder addon and dumps a deterministic
JSON snapshot of the resulting scene (objects, mesh counts, materials,
transforms, collections) for baseline/parity diffing.

The optional third argument is a JSON object of extra operator keyword
arguments, e.g. '{"hierarchy_types": "COLLECTION_INSTANCES"}'.
"""
import json
import os
import sys
import time

import addon_utils
import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
step_file, out_json = argv[0], argv[1]
op_kwargs = json.loads(argv[2]) if len(argv) > 2 else {}

bpy.ops.preferences.addon_enable(module="CADder")

t0 = time.perf_counter()
result = bpy.ops.import_scene.occ_import_step(
    filepath=step_file, override_file=os.path.basename(step_file), **op_kwargs
)
elapsed = time.perf_counter() - t0


def collection_tree(col):
    return {
        "name": col.name,
        "objects": sorted(o.name for o in col.objects),
        "children": [collection_tree(c) for c in sorted(col.children, key=lambda c: c.name)],
    }


objects = []
for obj in sorted(bpy.data.objects, key=lambda o: o.name):
    rec = {
        "name": obj.name,
        "type": obj.type,
        "parent": obj.parent.name if obj.parent else None,
        "matrix_world": [round(v, 6) for row in obj.matrix_world for v in row],
        "collections": sorted(c.name for c in obj.users_collection),
    }
    if obj.instance_type == "COLLECTION" and obj.instance_collection is not None:
        rec["instance_collection"] = obj.instance_collection.name
    if obj.type == "MESH":
        me = obj.data
        rec["verts"] = len(me.vertices)
        rec["polys"] = len(me.polygons)
        rec["loops"] = len(me.loops)
        rec["uv_layers"] = sorted(l.name for l in me.uv_layers)
        rec["materials"] = [
            {
                "name": m.name,
                "diffuse": [round(c, 5) for c in m.diffuse_color],
            }
            for m in me.materials
            if m is not None
        ]
    objects.append(rec)

snapshot = {
    "file": step_file,
    "operator_kwargs": op_kwargs,
    "operator_result": list(result),
    "import_seconds": round(elapsed, 3),
    "num_objects": len(objects),
    "objects": objects,
    "collections": collection_tree(bpy.context.scene.collection),
}

with open(out_json, "w") as f:
    json.dump(snapshot, f, indent=1, sort_keys=True)

print(f"SNAPSHOT_WRITTEN {out_json} objects={len(objects)} time={elapsed:.2f}s")
