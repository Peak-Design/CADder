"""Regression test: an engineering material must inherit the part's
imported CAD color.

add_material writes the CAD color to the Principled BSDF's Base Color
input and never to mat.diffuse_color (the viewport swatch), so reading
diffuse_color back returned Blender's default gray and every engineering
material came out gray regardless of the part color.

Run:
  blender -b --factory-startup --python ci/test_eng_material_color.py

Uses ci/baselines/mat_color_ap242.step, which carries both per-part
colors and engineering materials, as a real CATIA/NX export does.
"""
import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "baselines", "mat_color_ap242.step")
GREY = (0.8, 0.8, 0.8)


def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="STEPper_NEXT")


def base_color(mat):
    """Read Base Color without using addon helpers, so this test stays a
    check on behavior rather than on its own implementation."""
    if mat.use_nodes and mat.node_tree is not None:
        for node in mat.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return tuple(node.inputs["Base Color"].default_value[:3])
    return tuple(mat.diffuse_color[:3])


def mesh_objs():
    return [o for o in bpy.data.objects
            if o.type == "MESH" and len(o.data.polygons)]


def close(a, b, eps=1e-4):
    return all(abs(x - y) < eps for x, y in zip(a, b))


def main():
    if not os.path.exists(FIXTURE):
        print("FAIL: fixture missing:", FIXTURE)
        return 1
    failures = 0

    # Color materials only, so we know what each part's CAD color is.
    fresh()
    bpy.ops.import_scene.occ_import_step(
        filepath=FIXTURE, override_file=os.path.basename(FIXTURE),
        eng_materials=False)
    cad = {}
    for obj in mesh_objs():
        mats = [m for m in obj.data.materials if m]
        if mats:
            cad[obj.name] = base_color(mats[0])
    if not cad:
        print("FAIL: fixture produced no materials")
        return 1
    # The fixture must have real colors, or this test cannot tell the bug
    # (everything gray) apart from correct behavior.
    if not [c for c in cad.values() if not close(c, GREY)]:
        print("FAIL: fixture has no non-gray colors, test is vacuous")
        return 1

    # Same import with engineering materials on.
    fresh()
    bpy.ops.import_scene.occ_import_step(
        filepath=FIXTURE, override_file=os.path.basename(FIXTURE),
        eng_materials=True)
    checked = 0
    for obj in mesh_objs():
        mats = [m for m in obj.data.materials if m]
        if not mats or obj.name not in cad:
            continue
        checked += 1
        got, want = base_color(mats[0]), cad[obj.name]
        if close(got, want):
            print(f"OK: {obj.name} -> {mats[0].name!r} keeps "
                  f"{tuple(round(c, 3) for c in got)}")
        else:
            failures += 1
            print(f"FAIL: {obj.name} -> {mats[0].name!r} is "
                  f"{tuple(round(c, 3) for c in got)}, "
                  f"expected CAD color {tuple(round(c, 3) for c in want)}")
    if not checked:
        print("FAIL: no parts were checked")
        return 1
    print(f"{'PASS' if not failures else 'FAIL'}: {checked} part(s) checked")
    return 1 if failures else 0


sys.exit(main())
