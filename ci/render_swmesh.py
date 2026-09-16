# SPDX-License-Identifier: GPL-3.0-or-later
"""Renders a .swmesh from a CAD named view, flat, for a texture check.

  blender -b --factory-startup -P render_swmesh.py -- <mesh> <out.png>
      [size] [front|right]

The picture it makes is meant to be compared with the CAD application's
own screenshot of the same view, by
sw-addin/tools/Check-Texture-Parity.py. That pair is how a texture
projection is checked: the pattern must land in the same place on the part
in both applications.

The import frame is ZPOS, so Blender's axes are the CAD application's
axes, and the front view looks down -Z with +Y up. The render is unlit:
the world is white and every material becomes an emission of its own base
colour or texture, so the image carries the projection and nothing else.
"""
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from STEPper_NEXT.rig import native_import  # noqa: E402
argv = sys.argv[sys.argv.index("--") + 1:]
mesh_path, out_png = argv[0], argv[1]
size = int(argv[2]) if len(argv) > 2 else 900
view = argv[3] if len(argv) > 3 else "front"


def flatten(mat):
    """Base colour or its image straight out, with no lighting."""
    if not mat or not mat.use_nodes:
        return
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    out = next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None)
    if bsdf is None or out is None:
        return
    emission = nodes.new("ShaderNodeEmission")
    base = bsdf.inputs["Base Color"]
    if base.is_linked:
        links.new(base.links[0].from_socket, emission.inputs["Color"])
    else:
        emission.inputs["Color"].default_value = base.default_value
    links.new(emission.outputs["Emission"], out.inputs["Surface"])


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    objects, _report = native_import.build(
        bpy.context, mesh_path, manifest=None, up_as="ZPOS")
    assert objects, "the mesh brought in nothing"

    from mathutils import Vector
    lo = [1e9, 1e9, 1e9]
    hi = [-1e9, -1e9, -1e9]
    for obj in objects:
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            for i in range(3):
                lo[i] = min(lo[i], world[i])
                hi[i] = max(hi[i], world[i])
    centre = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
    span = max(hi[i] - lo[i] for i in range(3))

    for mat in bpy.data.materials:
        flatten(mat)

    cam_data = bpy.data.cameras.new("view")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = span * 1.25
    cam = bpy.data.objects.new("view", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    reach = span * 4.0
    if view == "front":
        cam.location = (centre[0], centre[1], centre[2] + reach)
        cam.rotation_euler = (0.0, 0.0, 0.0)
    elif view == "right":
        cam.location = (centre[0] + reach, centre[1], centre[2])
        cam.rotation_euler = (1.5707963, 0.0, 1.5707963)
    else:
        raise SystemExit("unknown view " + view)
    bpy.context.scene.camera = cam

    world = bpy.data.worlds.new("flat")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (1, 1, 1, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 1.0
    bpy.context.scene.world = world

    scene = bpy.context.scene
    # Any engine draws an emission the same way. EEVEE is the fast one
    # and its name changed across versions, so take what is there.
    for name in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES"):
        try:
            scene.render.engine = name
            break
        except TypeError:
            continue
    scene.render.resolution_x = size
    scene.render.resolution_y = size
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    scene.render.filepath = out_png
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.render.render(write_still=True)
    print("rendered", out_png, os.path.getsize(out_png), "bytes")


main()
