# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for SolidWorks appearances as Blender materials.

    blender -b --factory-startup -P ci/appearance_smoke.py

Builds materials from appearance JSON the way a direct send delivers it
(polished gold, clear glass, a checker texture on a planar projection, a
normal map, a decal with alpha over a plastic), checks the node trees,
and renders a plane with the checker to prove the projection tiles at the
appearance's width in the part's own space.
"""

import json
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from STEPper_NEXT.rig import appearance, native_import, swmesh  # noqa: E402


def _check(cond, msg):
    if not cond:
        raise SystemExit("appearance_smoke: FAIL: " + msg)


def _png(path, size, pixel):
    img = bpy.data.images.new(os.path.basename(path), size, size, alpha=True)
    img.pixels = [c for y in range(size) for x in range(size) for c in pixel(x, y)]
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)
    return path


def _nodes(mat, kind):
    return [n for n in mat.node_tree.nodes if n.bl_idname == kind]


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    tmp = tempfile.mkdtemp(prefix="cadlink_appearance_")
    checker = _png(os.path.join(tmp, "checker.png"), 8,
                   lambda x, y: (1, 1, 1, 1) if (x // 4 + y // 4) % 2 == 0 else (0, 0, 0, 1))
    normal = _png(os.path.join(tmp, "plate_n.png"), 4, lambda x, y: (0.5, 0.5, 1, 1))
    logo = _png(os.path.join(tmp, "logo.png"), 8,
                lambda x, y: (1, 0, 0, 1) if 2 <= x < 6 else (0, 0, 0, 0))

    gold = {"name": "polished gold", "category": "metal/gold", "colour": [0.97, 0.88, 0.6],
            "specular": 0.7, "specular_colour": [1, 0.98, 0.84], "reflectivity": 0.65,
            "transparency": 0, "library": {"sw_shader": "polishedgold", "blurryReflections": "off"},
            "blender": {"roughness": 0.125, "metallic": 1.0, "glass": False},
            "mapping": {"type": 4, "width": 0.0254, "height": 0.0254}, "decals": []}
    glass = {"name": "clear glass", "category": "glass/gloss", "colour": [1, 1, 1],
             "transparency": 0.6, "ior": 1.0, "library": {"mtl_ior": "1.52"},
             "blender": {"roughness": 0.08, "metallic": 0.0, "glass": True}, "decals": []}
    textured = {"name": "texture", "colour": [1, 1, 1], "texture": checker,
                "bump_texture": normal, "library": {"bumpIsNormalMap": "on"},
                "blender": {"roughness": 0.4, "metallic": 0.0, "glass": False},
                "mapping": {"type": 1, "width": 0.01, "height": 0.01, "u": [1, 0, 0], "v": [0, 1, 0],
                            "centre": [0, 0, 0], "rotation": 0}, "decals": []}
    decal = {"name": "green high gloss plastic + logo", "colour": [0.28, 0.66, 0.33],
             "specular": 0.5, "reflectivity": 0.15,
             "blender": {"roughness": 0.18, "metallic": 0.0, "glass": False},
             "decals": [{"image": logo, "mask_type": 3, "mask_invert": False, "face": {"angle": 0},
                         "mapping": {"type": 3, "width": 0.04, "height": 0.02, "u": [0, 0, 1],
                                     "v": [1, 0, 0], "centre": [0, 0, 0], "rotation": 0}}]}

    scene = swmesh.Scene()
    for spec, rgba in ((gold, (0.97, 0.88, 0.6, 1.0)), (glass, (1, 1, 1, 1)),
                       (textured, (1, 1, 1, 1)), (decal, (0.28, 0.66, 0.33, 1))):
        scene.materials.append(swmesh.Material(
            name=spec["name"], rgba=rgba, roughness=spec["blender"]["roughness"],
            metallic=spec["blender"]["metallic"], texture=spec.get("texture"),
            appearance_json=json.dumps(spec)))
    mats = [native_import._material(m, "SW ", 1.0) for m in scene.materials]
    m_gold, m_glass, m_tex, m_decal = mats

    b = m_gold.node_tree.nodes["Principled BSDF"]
    _check(abs(b.inputs["Metallic"].default_value - 1.0) < 1e-6, "gold is not metal")
    _check(abs(b.inputs["Roughness"].default_value - 0.125) < 1e-6, "gold roughness not carried")
    _check(m_gold["SWMESH_appearance_name"] == "SW polished gold", "gold name property")
    _check(json.loads(m_gold["SWMESH_appearance"])["library"]["sw_shader"] == "polishedgold",
           "the raw appearance is not on the material")

    b = m_glass.node_tree.nodes["Principled BSDF"]
    _check(b.inputs["Transmission Weight"].default_value > 0.99, "glass does not transmit")
    _check(abs(b.inputs["IOR"].default_value - 1.52) < 1e-6, "glass IOR not from the library")

    images = {n.image.name: n for n in _nodes(m_tex, "ShaderNodeTexImage")}
    _check("checker.png" in images and images["checker.png"].projection == "FLAT",
           "the checker is not a flat projection: %s" % list(images))
    _check(_nodes(m_tex, "ShaderNodeNormalMap"), "no normal map node")
    _check(m_tex.node_tree.nodes["Principled BSDF"].inputs["Base Color"].is_linked,
           "the checker does not drive the base colour")

    # A decal is two group nodes and an image, not twenty loose maths
    # nodes: one group says where it sits, the other lays it over what is
    # under it.
    groups = {n.node_tree.name: n for n in _nodes(m_decal, "ShaderNodeGroup")}
    _check(appearance.FRAME_GROUP in groups and appearance.MIX_GROUP in groups,
           "the decal did not use the node groups: %s" % list(groups))
    _check(not _nodes(m_decal, "ShaderNodeMath"),
           "maths nodes are still loose in the decal material")
    over = groups[appearance.MIX_GROUP]
    _check(over.outputs["Color"].is_linked, "the decal group drives nothing")
    _check(over.inputs["Decal Color"].is_linked
           and over.inputs["Decal Alpha"].is_linked
           and over.inputs["Facing"].is_linked,
           "the decal group is not fed the image")
    decal_tex = [n for n in _nodes(m_decal, "ShaderNodeTexImage") if n.image.name == "logo.png"]
    _check(decal_tex and decal_tex[0].extension == "CLIP", "the decal image does not clip")
    _check(decal_tex[0].inputs["Vector"].links[0].from_node
           == groups[appearance.FRAME_GROUP],
           "the decal image is not placed by the frame group")

    # Both groups are shared, so a second decal material adds no second
    # copy and an edit inside one reaches every decal in the file.
    before = len(bpy.data.node_groups)
    other_decal = native_import._material(
        swmesh.Material(name="another logo", rgba=(0.28, 0.66, 0.33, 1),
                        appearance_json=json.dumps(dict(decal, colour=[0.1, 0.2, 0.3]))),
        "SW ", 1.0)
    _check(len(bpy.data.node_groups) == before,
           "a second decal material copied the node groups")
    _check(_nodes(other_decal, "ShaderNodeGroup")[0].node_tree
           == groups[appearance.FRAME_GROUP].node_tree,
           "the second decal material points at another group")

    # Reuse by identity: the same appearance again is the same material,
    # the same name with another appearance is a new one.
    again = native_import._material(scene.materials[0], "SW ", 1.0)
    _check(again is m_gold, "the same appearance built a second material")
    other = swmesh.Material(name="polished gold", rgba=(1, 0, 0, 1),
                            appearance_json=json.dumps(dict(gold, colour=[1, 0, 0])))
    _check(native_import._material(other, "SW ", 1.0) is not m_gold,
           "a different appearance reused the material of the same name")

    # The projection, rendered: a 40 mm plane under an orthographic camera,
    # the checker at 10 mm. Four tiles of eight texels, 4 texels a square:
    # a 40 x 40 render samples 8 squares across, alternating every 5 px.
    me = bpy.data.meshes.new("plane")
    me.from_pydata([(-0.02, -0.02, 0), (0.02, -0.02, 0), (0.02, 0.02, 0), (-0.02, 0.02, 0)], [], [(0, 1, 2, 3)])
    ob = bpy.data.objects.new("plane", me)
    bpy.context.scene.collection.objects.link(ob)
    me.materials.append(m_tex)
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = 0.04
    cam = bpy.data.objects.new("cam", cam_data)
    cam.location = (0, 0, 1)
    bpy.context.scene.collection.objects.link(cam)
    sc = bpy.context.scene
    sc.camera = cam
    sc.render.engine = "BLENDER_EEVEE"
    sc.render.resolution_x = sc.render.resolution_y = 40
    sc.render.film_transparent = False
    sc.view_settings.view_transform = "Standard"
    out = os.path.join(tmp, "render.png")
    sc.render.filepath = out
    # A plain emission of the base colour, so lighting does not decide the
    # squares: swap the BSDF out for the render.
    nt = m_tex.node_tree
    em = nt.nodes.new("ShaderNodeEmission")
    base_link = nt.nodes["Principled BSDF"].inputs["Base Color"].links[0].from_socket
    nt.links.new(base_link, em.inputs["Color"])
    nt.links.new(em.outputs[0], nt.nodes["Material Output"].inputs["Surface"])
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(out)
    px = list(img.pixels)
    w = img.size[0]

    def lum(x, y):
        i = (y * w + x) * 4
        return px[i]

    row = [1 if lum(x, 20) > 0.5 else 0 for x in range(2, 38, 5)]
    flips = sum(1 for a, b2 in zip(row, row[1:]) if a != b2)
    _check(flips >= 6, "the checker does not tile at 10 mm: row %s" % row)

    print("appearance_smoke: OK: gold metal, glass transmitting at the library IOR, "
          "a checker on a planar projection tiling at 10 mm (row %s), a normal map, "
          "a clipped decal over plastic, materials reused by appearance identity" % row)


main()
