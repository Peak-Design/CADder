# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for what appearances share between materials.

    blender -b --factory-startup --python-exit-code 1 -P ci/appearance_reuse_smoke.py

  color space   One image file used as a color texture and as a bump map
                (SolidWorks' carbon fiber appearances do this) is two
                images: the color space belongs to the image, and it was
                the first use of the file that set it for every other use.
  decal groups  A new version of the decal node groups refills them in
                place. Every decal already in the file keeps its links and
                its values. Before, the sockets were made again with new
                identifiers, so the decals lost their placement and their
                image.
"""

import json
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import appearance, native_import, swmesh  # noqa: E402


def fail(msg):
    raise SystemExit("appearance_reuse_smoke: FAIL: " + msg)


def _png(path, size=4):
    img = bpy.data.images.new(os.path.basename(path), size, size, alpha=True)
    img.pixels = [0.5] * (size * size * 4)
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)
    return path


def is_data(img):
    return bool(getattr(img.colorspace_settings, "is_data", False)
                or img.colorspace_settings.name == "Non-Color")


def check_colour_space(tmp):
    for first_bump in (True, False):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        path = _png(os.path.join(tmp, "stone.png"))
        if first_bump:
            bump = appearance._image(path, non_color=True)
            colour = appearance._image(path)
        else:
            colour = appearance._image(path)
            bump = appearance._image(path, non_color=True)
        order = "bump first" if first_bump else "color first"
        if colour is None or bump is None:
            fail("%s: the image did not load" % order)
        if not is_data(bump):
            fail("%s: the bump map is read as color (%s)"
                 % (order, bump.colorspace_settings.name))
        if is_data(colour):
            fail("%s: the color texture is read as data" % order)
        # The same use again reuses the same image.
        if appearance._image(path) is not colour \
                or appearance._image(path, non_color=True) is not bump:
            fail("%s: a use of the file made one more image" % order)


def check_decal_groups(tmp):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    logo = _png(os.path.join(tmp, "logo.png"), 8)
    decal = {"name": "plastic", "category": "plastic", "colour": [0.2, 0.2, 0.2],
             "blender": {"roughness": 0.4, "metallic": 0.0, "glass": False},
             "decals": [{"image": logo, "mask_type": 0,
                         "mapping": {"u": [0, 0, 1], "v": [1, 0, 0],
                                     "centre": [0.01, 0.02, 0.0],
                                     "width": 0.04, "height": 0.02,
                                     "rotation": 30.0}}]}
    mat = native_import._material(
        swmesh.Material(name="logo plastic", rgba=(0.2, 0.2, 0.2, 1),
                        appearance_json=json.dumps(decal)), "SW ", 1.0)
    nodes = [n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeGroup"]
    frame = next(n for n in nodes if n.node_tree.name.startswith(appearance.FRAME_GROUP))
    over = next(n for n in nodes if n.node_tree.name.startswith(appearance.MIX_GROUP))
    # A value the user set by hand on this decal.
    frame.inputs["Width"].default_value = 7.5
    width = frame.inputs["Width"].default_value
    origin = tuple(frame.inputs["Origin"].default_value)

    def links_of(node):
        return sorted((l.from_socket.name, l.to_socket.name)
                      for l in mat.node_tree.links
                      if l.from_node == node or l.to_node == node)

    before = {n.name: links_of(n) for n in (frame, over)}
    appearance.GROUP_VERSION += 1          # as a new release would
    try:
        # A part with a decal arrives in the file, which refills the groups.
        native_import._material(
            swmesh.Material(name="other plastic", rgba=(0.3, 0.3, 0.3, 1),
                            appearance_json=json.dumps(dict(decal, colour=[0.3, 0.3, 0.3]))),
            "SW ", 1.0)
    finally:
        appearance.GROUP_VERSION -= 1
    if frame.node_tree.get(appearance._VERSION_KEY) != appearance.GROUP_VERSION + 1:
        fail("the frame group was not refilled")
    after = {n.name: links_of(n) for n in (frame, over)}
    if after != before:
        fail("the decal lost links: before %s, after %s" % (before, after))
    if abs(frame.inputs["Width"].default_value - width) > 1e-9:
        fail("the decal width went from %s to %s"
             % (width, frame.inputs["Width"].default_value))
    if tuple(frame.inputs["Origin"].default_value) != origin:
        fail("the decal origin changed")
    names = [s.name for s in frame.node_tree.interface.items_tree
             if s.item_type == "SOCKET"]
    if len(names) != len(set(names)):
        fail("the refilled group has sockets twice: %s" % names)


def main():
    tmp = tempfile.mkdtemp(prefix="cadlink_appearance_reuse_")
    check_colour_space(tmp)
    check_decal_groups(tmp)
    print("appearance_reuse_smoke: OK: a file used as color and as data is "
          "two images, and a new version of the decal groups keeps every "
          "decal's links and values")


main()
