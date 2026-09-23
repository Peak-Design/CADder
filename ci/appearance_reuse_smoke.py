# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for what appearances share between materials.

    blender -b --factory-startup --python-exit-code 1 -P ci/appearance_reuse_smoke.py

  color space   One image file used as a color texture and as a bump map
                (SolidWorks' carbon fiber appearances do this) is two
                images: the color space belongs to the image, and it was
                the first use of the file that set it for every other use.
"""

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import appearance  # noqa: E402


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


def main():
    tmp = tempfile.mkdtemp(prefix="cadlink_appearance_reuse_")
    check_colour_space(tmp)
    print("appearance_reuse_smoke: OK: a file used as color and as data is "
          "two images")


main()
