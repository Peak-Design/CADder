# SPDX-License-Identifier: GPL-3.0-or-later
"""Rotate to Fit off on the live link: are the islands of a send upright?

    blender -b --factory-startup --python-exit-code 1 -P ci/native_upright_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

The CAD application sends one UV patch for each CAD face, in the frame of
its surface, so the islands of a part lie in all directions and some are
mirrored. A send, an update and a refine make them upright to the world
when Rotate to Fit is off (the UV panel), the same as a STEP import does
(uv_upright_smoke.py). Every send runs through bridge._run_job, the path
SolidWorks drives.
"""
import math
import os
import struct
import sys
import tempfile

import numpy as np

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import bridge, rig  # noqa: E402
from CADder.rig import native_import, swmesh, ui as rig_ui  # noqa: E402

FAILS = []
TOL = 1.0  # degrees
STEM = "boxes"


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def _text(s):
    raw = s.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


# (normal, a, b) with a x b = normal: the frame of each face.
FACES = [((1, 0, 0), (0, 1, 0), (0, 0, 1)),
         ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
         ((0, 1, 0), (0, 0, 1), (1, 0, 0)),
         ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
         ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
         ((0, 0, -1), (0, 1, 0), (1, 0, 0))]
HALF = np.array([0.02, 0.015, 0.01])


def box():
    """24 points, 12 triangles, a UV patch per face as the CAD side sends
    it: in a frame of its own, turned, and every other one mirrored."""
    pos, uvs, tris = [], [], []
    for k, (n, a, b) in enumerate(FACES):
        n, a, b = (np.array(v, dtype=float) for v in (n, a, b))
        c = HALF * n
        ha, hb = abs(a @ HALF), abs(b @ HALF)
        turn = math.radians(37.0 * k + 11.0)
        cs, sn = math.cos(turn), math.sin(turn)
        base = len(pos)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            p = c + su * ha * a + sv * hb * b
            u, v = p @ a, p @ b
            if k % 2:
                u = -u
            pos.append(p)
            uvs.append((cs * u - sn * v, sn * u + cs * v))
        tris += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
    return np.array(pos), np.array(uvs), np.array(tris)


def rot(axis, deg):
    x, y, z = axis
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return np.array([[c + x * x * (1 - c), x * y * (1 - c) - z * s,
                      x * z * (1 - c) + y * s],
                     [y * x * (1 - c) + z * s, c + y * y * (1 - c),
                      y * z * (1 - c) - x * s],
                     [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s,
                      c + z * z * (1 - c)]])


# (component id, name, rotation, x): one box turned about Z, one stood on
# its side and turned, one tipped over two axes.
PARTS = [("c001", "turned", rot((0, 0, 1), 30), 0.0),
         ("c002", "on_side", rot((0, 0, 1), 20) @ rot((1, 0, 0), 90), 0.1),
         ("c003", "tipped", rot((0, 1, 0), 90) @ rot((1, 0, 0), 35), 0.2)]


def write_mesh(path, grown=None):
    """`grown` names a part that is larger, as if it changed in CAD."""
    pos, uvs, tris = box()
    body = struct.pack("<III", swmesh.MAGIC, 3, swmesh.FLAG_UVS)
    body += struct.pack("<d", 0.0005)
    body += struct.pack("<IIII", 1, len(PARTS), len(PARTS), 0)
    body += _text("grey") + struct.pack("<6f", 0.8, 0.8, 0.8, 1.0, 0.5, 0.0)
    body += _text("") + struct.pack("<I", 0)
    for i, (_cid, name, _r, _x) in enumerate(PARTS):
        # A size of its own for each, so the shapes are not one mesh.
        p = pos * (1.0 + 0.1 * i) * (1.25 if name == grown else 1.0)
        body += struct.pack("<i", i + 1) + _text(name)
        body += struct.pack("<II", len(p), len(tris))
        body += struct.pack("<%df" % p.size, *p.ravel())
        body += struct.pack("<%df" % uvs.size, *uvs.ravel())
        body += struct.pack("<%di" % tris.size, *tris.ravel())
        body += struct.pack("<%di" % len(tris), *([0] * len(tris)))
    for i, (cid, name, r, x) in enumerate(PARTS):
        m = np.eye(4)
        m[:3, :3] = r
        m[0, 3] = x
        body += (struct.pack("<i", i + 1) + _text(cid) + _text(name)
                 + _text(name + "-1") + struct.pack("<16d", *m.ravel())
                 + struct.pack("<B", 0))
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def send(mesh, update=False, mode="FLAT"):
    result = bridge._run_job({
        "step": None, "mesh": mesh, "manifest": None,
        "steps": {"import": False, "replace": not update, "update": update,
                  "match": False, "sync_poses": False, "build_rig": False,
                  "relink": False, "cleanup": False},
        "import_options": {"hierarchy_types": mode, "up_as": "ZPOS"},
    })
    check(result.get("ok"), "the send ran (%s)" % result.get("error"))
    return result


def parts():
    return {o.get("SWMESH_path") or o.name: o for o in bpy.data.objects
            if o.type == "MESH" and o.get("SWMESH_file") == STEM}


def worst(obj, world=None):
    """The worst angle, in degrees, between world up and +V over the
    triangles (world Z on an upright face, world Y on a flat one), and the
    share of UV area that is mirrored."""
    bpy.context.view_layer.update()
    me = obj.data
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) @ np.array(
        (world or obj.matrix_world).to_3x3()).T
    lv = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    uv = np.empty(len(me.loops) * 2)
    me.uv_layers.active.uv.foreach_get("vector", uv)
    uv = uv.reshape(-1, 2)
    errs, mirrored, total = [], 0.0, 0.0
    for p in me.polygons:
        s = p.loop_start
        for k in range(1, p.loop_total - 1):
            i0, i1, i2 = s, s + k, s + k + 1
            e1, e2 = co[lv[i1]] - co[lv[i0]], co[lv[i2]] - co[lv[i0]]
            n = np.cross(e1, e2)
            n /= np.linalg.norm(n)
            if abs(n[2]) < 0.1:
                w = np.array([0.0, 0.0, 1.0])
            elif abs(n[2]) > 0.99:
                w = np.array([0.0, 1.0, 0.0])
            else:
                continue
            wt = w - (n @ w) * n
            g = np.array([[e1 @ e1, e1 @ e2], [e1 @ e2, e2 @ e2]])
            a, b = np.linalg.solve(g, [e1 @ wt, e2 @ wt])
            d = a * (uv[i1] - uv[i0]) + b * (uv[i2] - uv[i0])
            errs.append(math.degrees(abs(math.atan2(d[0], d[1]))))
            d1, d2 = uv[i1] - uv[i0], uv[i2] - uv[i0]
            area = 0.5 * (d1[0] * d2[1] - d2[0] * d1[1])
            total += abs(area)
            if area < 0:
                mirrored += -area
    return max(errs) if errs else 0.0, len(errs), mirrored / max(total, 1e-30)


def all_upright(label):
    found = parts()
    check(len(found) == len(PARTS), "%s: %d parts arrived" % (label,
                                                              len(found)))
    for name, obj in sorted(found.items()):
        err, n, mir = worst(obj)
        check(n > 0 and err < TOL,
              "%s: %s is upright (worst %.3f degrees over %d triangles)"
              % (label, name, err, n))
        check(mir < 1e-6, "%s: %s has no mirrored island (%.4f)"
              % (label, name, mir))


def fresh():
    try:
        rig.unregister()
    except Exception:                                   # noqa: BLE001
        pass
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    rig_ui._reset_state()


tmp = tempfile.mkdtemp(prefix="native_upright_")
MESH = write_mesh(os.path.join(tmp, STEM + ".swmesh"))

print("\n== a send with Rotate to Fit off (the default)")
fresh()
check(bpy.context.scene.stepper.uv_rotate is False,
      "Rotate to Fit is off in a new scene")
send(MESH)
all_upright("send")

print("\n== an update keeps them upright")
send(MESH, update=True)
all_upright("update")

print("\n== a refine brings new geometry, and makes it upright too")
native_import.refine(bpy.context, MESH, stems=[STEM])
all_upright("refine")

print("\n== a Refresh Model keeps the UVs of a part that did not change")


def uv_of(obj):
    a = np.empty(len(obj.data.loops) * 2)
    obj.data.uv_layers.active.uv.foreach_get("vector", a)
    return a


fresh()
send(MESH)
for obj in parts().values():
    # A map of the user: skewed, so no pass of the addon makes it.
    a = uv_of(obj).reshape(-1, 2) @ np.array([[0.9, 0.3], [-0.1, 0.7]]) + 0.25
    obj.data.uv_layers.active.uv.foreach_set("vector", a.ravel())
    obj.data.update()
kept = {k: uv_of(o) for k, o in parts().items()}
from CADder import main as main_mod  # noqa: E402
real_unwrap = main_mod._unwrap_compound_objects
asked = []


def spy(targets, *args, **kw):
    asked.extend(targets)
    return real_unwrap(targets, *args, **kw)


main_mod._unwrap_compound_objects = spy
try:
    send(MESH, update=True)
finally:
    main_mod._unwrap_compound_objects = real_unwrap
check(not asked, "the unwrap of compound faces leaves the unchanged parts "
      "alone (%d asked)" % len(asked))
for key, obj in sorted(parts().items()):
    check(np.allclose(uv_of(obj), kept[key], atol=1e-5),
          "%s keeps the UVs of the user" % key)

print("\n== a part that changed in CAD comes in upright, the others stay")
os.makedirs(os.path.join(tmp, "changed"), exist_ok=True)
CHANGED = write_mesh(os.path.join(tmp, "changed", STEM + ".swmesh"),
                     grown="turned")
send(CHANGED, update=True)
for key, obj in sorted(parts().items()):
    if key.startswith("turned"):
        err, n, _mir = worst(obj)
        check(n > 0 and err < TOL, "%s changed and is upright (worst %.3f "
              "degrees)" % (key, err))
    else:
        check(np.allclose(uv_of(obj), kept[key], atol=1e-5),
              "%s did not change and keeps the UVs of the user" % key)

print("\n== a mesh from before this option is not turned by a refresh")
fresh()
bpy.context.scene.stepper.uv_rotate = True
send(MESH)
old = {k: uv_of(o) for k, o in parts().items()}
bpy.context.scene.stepper.uv_rotate = False
send(MESH, update=True)
for key, obj in sorted(parts().items()):
    check(np.allclose(uv_of(obj), old[key], atol=1e-5),
          "%s keeps the UVs it had" % key)

print("\n== a send as collection instances is upright too")
fresh()
send(MESH, mode="COLLECTION_INSTANCES")
placed = [o for o in bpy.data.objects if o.instance_type == "COLLECTION"
          and o.instance_collection is not None
          and o.get("SWMESH_file") == STEM]
check(len(placed) == len(PARTS), "%d placements" % len(placed))
for holder in placed:
    for proto in holder.instance_collection.all_objects:
        if proto.type != "MESH":
            continue
        err, n, mir = worst(proto, holder.matrix_world)
        check(n > 0 and err < TOL and mir < 1e-6,
              "%s is upright in the world (worst %.3f degrees)"
              % (holder.name, err))

print("\n== with Rotate to Fit on, the islands stay as the CAD side sent them")
fresh()
bpy.context.scene.stepper.uv_rotate = True
send(MESH)
errs = [worst(o)[0] for o in parts().values()]
check(errs and max(errs) > 5.0,
      "some island is not upright (worst %.1f degrees)" % max(errs or [0]))

print("\n== a mesh made one way is not shared with a send made the other way")
key_on = native_import._post_key(bpy.context.scene)
bpy.context.scene.stepper.uv_rotate = False
key_off = native_import._post_key(bpy.context.scene)
check(key_on != key_off and key_off.endswith("o"),
      "the keys differ (%s, %s)" % (key_on, key_off))

if FAILS:
    print("\n%d check(s) failed:" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nall checks passed")
