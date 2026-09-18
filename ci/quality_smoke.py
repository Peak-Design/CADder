# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the quality settings every route shares.

The Mesh Quality panel holds the same five settings as the import dialog.
Regenerate must follow them: a name, Custom, and Relative Tessellation.
An import made with Relative Tessellation must say so in its record, so a
refresh reads the recorded value as a share and not as a length.

Run:  blender -b --factory-startup -P quality_smoke.py
"""

import json
import os
import sys

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
bpy.ops.preferences.addon_enable(module="CADder")

from CADder import import_ui, main as m, quality, refresh  # noqa: E402

STEP = os.path.join(_HERE, "fixtures", "holes.step")


def check(ok, what):
    if not ok:
        raise SystemExit("quality_smoke: FAIL: " + what)


class Layout:
    """Enough of UILayout to run a draw function."""
    active = True

    def prop(self, owner, name, **_kw):
        getattr(owner, name)          # raises on a property that is not there
        return self

    def column(self, **_kw):
        return Layout()

    def row(self, **_kw):
        return Layout()


def main():
    sc = bpy.context.scene.stepper
    import_ui.draw_quality(sc, Layout())
    check(sc.quality_preset == quality.DEFAULT, "the scene does not start at Balanced")

    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, htypes="EMPTIES", up_as="Z",
                deflection_spec=quality.spec("DRAFT"))
    parts = [o for o in bpy.data.objects if o.type == "MESH" and "STEP_tag" in o]
    check(parts, "the import made no parts")

    def regen(**settings):
        for key, value in settings.items():
            setattr(sc, key, value)
        for o in bpy.data.objects:
            o.select_set(o in parts)
        bpy.context.view_layer.objects.active = parts[0]
        bpy.ops.stepper.regenerate(use_scene_settings=True)
        return sum(len(o.data.vertices) for o in parts)

    draft = regen(quality_preset="DRAFT", tessellation_relative=False)
    ultra = regen(quality_preset="ULTRA")
    coarse = regen(quality_preset="CUSTOM", lin_deflection_len=0.005,
                   ang_deflection_rot=0.8)
    fine_share = regen(tessellation_relative=True, lin_deflection_rel=0.001,
                       ang_deflection_rot=0.3)
    wide_share = regen(lin_deflection_rel=0.05, ang_deflection_rot=0.8)
    check(ultra > draft, "Ultra (%d) is not finer than Draft (%d)" % (ultra, draft))
    check(coarse <= draft, "Custom at 5 mm (%d) is finer than Draft (%d)" % (coarse, draft))
    check(fine_share > wide_share,
          "a small share (%d) is not finer than a large one (%d)" % (fine_share, wide_share))

    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    m._cache_drop(STEP)
    m.load_step(bpy.context, STEP, htypes="EMPTIES", up_as="Z",
                deflection_spec=quality.spec("DRAFT", angle=0.5, relative=True,
                                             relative_distance=0.02))
    part = next(o for o in bpy.data.objects if o.type == "MESH" and "STEP_tag" in o)
    record = json.loads(part["STEP_import_settings"])
    check(record.get("tessellation_relative") is True,
          "the record does not say the import was relative")
    kwargs, _record, _source = refresh.import_settings(bpy.context.scene, STEP)
    check(kwargs.get("deflection_spec") == {"mode": "relative", "lin": 0.02, "ang": 0.5},
          "a refresh reads the share as %r" % kwargs.get("deflection_spec"))
    check("lin_deflection" not in kwargs, "a refresh also passes the share as a length")

    print("quality_smoke: OK: Regenerate follows Draft %d, Ultra %d, Custom %d, "
          "relative %d and %d, and a relative import refreshes as relative"
          % (draft, ultra, coarse, fine_share, wide_share))


main()
