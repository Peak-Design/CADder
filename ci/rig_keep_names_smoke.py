# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke: Refresh with Add and Remove Bones keeps the name of the
handle of a ball.

    blender -b --factory-startup --python-exit-code 1 -P ci/rig_keep_names_smoke.py

A ball with a cone limit is two bones: the handle the user poses, and a
hidden DEF bone that the parts ride. The group tag is on the DEF bone, so
the snapshot of the rig named the group after DEF_<handle>. The rebuild then
gave that name to the handle, and made DEF_DEF_<handle> for the hidden one.
A key on the handle lost its bone, and each refresh added one more DEF_.
"""

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import rig  # noqa: E402
from CADder.ci import rebuild_fixture as fx  # noqa: E402

NAMES = ("base", "stud")


def assembly():
    parts = [
        fx.Part("c001", "base-1", "pbase", fx.translate(0.0), 0),
        fx.Part("c002", "stud-1", "pstud", fx.translate(0.2), 1),
    ]
    joints = [
        # A ball with a cone limit: the swing-cone template.
        {"id": "j001", "type": "ball", "parent_group": "g000",
         "child_group": "g001", "origin": [0.2, 0.0, 0.0],
         "axis": [0.0, 1.0, 0.0], "secondary_axis": [0.0, 1.0, 0.0],
         "limits": {"rotation": {"min": 0.0, "max": 0.785,
                                 "value_at_rest": 0.0},
                    "translation": None}},
    ]
    return parts, joints


def fcurves(action):
    """Every F-curve of an action, in the layered actions of Blender 5 and
    in the older flat ones."""
    out = list(getattr(action, "fcurves", None) or [])
    for layer in getattr(action, "layers", None) or []:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", None) or []:
                out.extend(bag.fcurves)
    return out


def fail(msg):
    raise SystemExit("rig_keep_names_smoke: FAIL: %s" % msg)


def main():
    tmp = os.path.join(tempfile.gettempdir(), "rig_keep_names_smoke")
    os.makedirs(tmp, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    parts, joints = assembly()
    mesh, man = fx.export(tmp, parts, NAMES, joints)
    if not fx.send(mesh, man, "FLAT", "ZPOS").get("ok"):
        fail("the send failed")
    arm = fx.rig_object()
    before = sorted(b.name for b in arm.data.bones)
    handles = [n for n in before if "DEF_" + n in before]
    if not handles:
        fail("the rig has no handle with a DEF bone: %s" % before)

    # A key on every handle, which names the handle.
    for name in handles:
        pb = arm.pose.bones[name]
        pb.rotation_euler = (0.1, 0.0, 0.0)
        pb.keyframe_insert("rotation_euler", frame=1)
    bpy.context.view_layer.update()

    for n in range(2):
        mesh, man = fx.export(tmp, parts, NAMES, joints)
        result = fx.send(mesh, man, "FLAT", "ZPOS", update=True,
                         rig_mode="APPEND")
        if not result.get("ok"):
            fail("refresh %d failed: %s" % (n + 1, result.get("error")))
        after = sorted(b.name for b in arm.data.bones)
        if after != before:
            fail("refresh %d renamed the bones: before %s, after %s"
                 % (n + 1, before, after))
        action = arm.animation_data.action if arm.animation_data else None
        if action is None or not fcurves(action):
            fail("refresh %d lost the animation" % (n + 1))
        for fcurve in fcurves(action):
            try:
                arm.path_resolve(fcurve.data_path)
            except ValueError:
                fail("refresh %d: the key %s names no bone"
                     % (n + 1, fcurve.data_path))
    print("rig_keep_names_smoke: OK: %s kept their names through two "
          "refreshes" % ", ".join(handles))


main()
