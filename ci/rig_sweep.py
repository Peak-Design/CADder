# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive every control of a rig through its travel and report what breaks.

    blender -b --factory-startup -P ci/rig_sweep.py -- <manifest|directory>...
    blender -b --factory-startup -P ci/rig_sweep.py -- --grid <...>

Three faults found on live assemblies in one week were all the same shape:
a loop carried somewhere its own geometry says it cannot go. A closure
point that orbits a spinning screw, a chain that comes out of a singular
pose on the mirror branch, a body that inherits a turn it cannot undo.
None of them shows at rest. All of them show the moment a control is
driven far enough, and all of them show as ONE of two numbers:

  gap   how far the two halves of a loop closure have come apart. A loop
        that is closed reads microns. A loop that has been carried away
        reads millimetres.

  jump  how far a body moved between two neighbouring poses, against how
        far it moves on a normal step. A mechanism is continuous, so a
        body that moves twenty times its usual step has been teleported.

So this sweeps each control from its rest pose outward, both ways, and
watches those two numbers. Where a mechanism runs out of travel the gap
opens: that is the mechanism binding, not a fault, so the sweep stops
there and reports the travel it found. Faults are gaps and jumps INSIDE
the travel.

Nothing here proves a rig correct. It turns "Oscar finds it while posing"
into a number, a control and a pose.
"""

import glob
import math
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder.rig import graph, manifest as man_mod, rig_build  # noqa: E402

CONTROLS = "SW_controls"

# A closure wider than this has come apart rather than merely rounded.
# Blender poses in single precision, so a long chain rounds to microns.
GAP_TOL = 1e-4                  # metres

# A step that moves a body this many times the sweep's typical step has
# not moved it, it has put it somewhere else.
JUMP_FACTOR = 20.0

# ... and it has to be worth seeing at all, against the size of the rig.
JUMP_FLOOR = 0.02               # of the rig's own diagonal

STEPS = 90                      # each way, per control
GRID = 7                        # per axis, for a mechanism with two inputs

# A path or surface joint holds its body at the NEAREST point of a curve or
# a face (rig_build's _make_path_rail says why Clamp To cannot be used).
# Nearest point is not continuous: drag the body sideways past the middle
# between two parts of the same curve and the nearest point IS somewhere
# else. That is the mechanism working, so a jump there is reported as
# something to know rather than something to fix.
SNAPPED = "SHRINKWRAP"


def _snapped(pb):
    return any(con.type == SNAPPED for con in pb.constraints)


def _free_channels(pb):
    """(path, index) for every channel of this bone the user can pose."""
    out = []
    for i in range(3):
        if not pb.lock_location[i]:
            out.append(("location", i))
    for i in range(3):
        if not pb.lock_rotation[i]:
            out.append(("rotation_euler", i))
    return out


def _span(pb, path, index, diag):
    """How far out to drive this channel, each way from rest."""
    for con in pb.constraints:
        if path == "rotation_euler" and con.type == "LIMIT_ROTATION":
            if index == 1 and con.use_limit_y:
                return abs(con.min_y), abs(con.max_y)
        if path == "location" and con.type == "LIMIT_LOCATION":
            lo = getattr(con, "use_min_" + "xyz"[index])
            hi = getattr(con, "use_max_" + "xyz"[index])
            if lo and hi:
                return (abs(getattr(con, "min_" + "xyz"[index])),
                        abs(getattr(con, "max_" + "xyz"[index])))
    if path == "rotation_euler":
        return math.pi, math.pi
    return diag, diag


class Rig(object):
    def __init__(self, path):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.manifest = man_mod.load(path)
        self.plan = graph.build(self.manifest)
        self.result = rig_build.build(bpy.context, self.manifest, self.plan)
        self.arm = self.result.armature_object
        self.bodies = [self.arm.pose.bones[n]
                       for n in self.result.bone_names.values()]
        self.pairs = []
        for lplan in self.plan.loops:
            helper = self.result.helper_names.get(lplan.loop.id)
            effector = self.result.effector_names.get(lplan.loop.id)
            if helper and effector:
                self.pairs.append((lplan.loop.id,
                                   self.arm.pose.bones[helper],
                                   self.arm.pose.bones[effector]))
        coll = self.arm.data.collections.get(CONTROLS)
        self.controls = []
        if coll is not None:
            for bone in coll.bones:
                pb = self.arm.pose.bones.get(bone.name)
                if pb is None:
                    continue
                for path, index in _free_channels(pb):
                    self.controls.append((pb, path, index))
        heads = [pb.bone.head_local for pb in self.bodies] or [Vector()]
        lo = Vector((min(h.x for h in heads), min(h.y for h in heads),
                     min(h.z for h in heads)))
        hi = Vector((max(h.x for h in heads), max(h.y for h in heads),
                     max(h.z for h in heads)))
        self.diag = max((hi - lo).length, 0.01)

    def rest(self):
        for pb in self.arm.pose.bones:
            pb.location = (0.0, 0.0, 0.0)
            pb.rotation_euler = (0.0, 0.0, 0.0)
            pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)

    def read(self):
        """(worst closure gap, where every body is) after a depsgraph pass."""
        bpy.context.view_layer.update()
        gap = 0.0
        for _lid, helper, effector in self.pairs:
            gap = max(gap, (effector.tail - helper.head).length)
        return gap, [pb.matrix.translation.copy() for pb in self.bodies]

    def label(self, pb, path, index):
        return "%s.%s%s" % (pb.name, "loc" if path == "location" else "rot",
                            "XYZ"[index])


def _moves(before, after):
    return max((a - b).length for a, b in zip(before, after)) if before else 0.0


def walk(rig, setter, steps, note, faults, snapped=False):
    """Drives one channel out from rest, one way, and watches the two
    numbers. Returns how far it got before the mechanism bound."""
    rig.rest()
    gap, where = rig.read()
    if gap > GAP_TOL:
        faults.append("%s: the loop is open by %.5f m AT REST" % (note, gap))
        return 0.0
    reached = 0.0
    hops = []
    for i in range(1, steps + 1):
        value = setter(i)
        gap, now = rig.read()
        if gap > GAP_TOL:
            break                       # the mechanism binds here, as it may
        hops.append((_moves(where, now), value))
        where = now
        reached = value
    if len(hops) > 4:
        typical = sorted(h for h, _v in hops)[len(hops) // 2]
        floor = JUMP_FLOOR * rig.diag
        for move, value in hops:
            if move > max(typical * JUMP_FACTOR, floor):
                faults.append(
                    "%s%s: a body jumped %.5f m in one step at %.4f, against "
                    "%.7f m on a normal step"
                    % ("(snapped) " if snapped else "", note, move, value,
                       typical))
                break
    return reached


def sweep_one(rig, pb, path, index, faults):
    low, high = _span(pb, path, index, rig.diag)
    name = rig.label(pb, path, index)
    out = []
    for way, reach in ((1.0, high), (-1.0, low)):
        if reach <= 0.0:
            out.append(0.0)
            continue

        def setter(i, way=way, reach=reach):
            rig.rest()
            value = way * reach * i / float(STEPS)
            getattr(pb, path)[index] = value
            return value
        out.append(walk(rig, setter, STEPS, name, faults, _snapped(pb)))
    return name, out[1], out[0]


def sweep_pair(rig, a, b, faults):
    """A mechanism with two inputs can be right along each axis on its own
    and wrong in between. The live wrench was."""
    (pa, patha, ia), (pb_, pathb, ib) = a, b
    reach_a = _span(pa, patha, ia, rig.diag)[1]
    reach_b = _span(pb_, pathb, ib, rig.diag)[1]
    note = "%s with %s" % (rig.label(*a), rig.label(*b))
    for step_a in range(-GRID, GRID + 1):
        va = reach_a * step_a / float(GRID)

        def setter(i, va=va):
            rig.rest()
            getattr(pa, patha)[ia] = va
            value = reach_b * i / float(GRID)
            getattr(pb_, pathb)[ib] = value
            return value
        for way in (1.0, -1.0):
            walk(rig, lambda i, w=way, s=setter: s(w * i), GRID,
                 "%s (at %.4f)" % (note, va), faults)


def survey(path, grid):
    name = os.path.basename(path).replace(".rig.json", "")
    if name in ("manifest", "rig"):
        # Every fixture is called manifest.rig.json. Its folder is its name.
        name = os.path.basename(os.path.dirname(path))
    try:
        rig = Rig(path)
    except Exception as exc:                          # noqa: BLE001
        return "%-28s BUILD FAILED: %s" % (name, exc)

    faults = []
    notes = []
    travel = []
    for pb, chan, index in rig.controls:
        label, low, high = sweep_one(rig, pb, chan, index, faults)
        travel.append("%s %+.3f..%+.3f" % (label, -low, high))
    if grid and len(rig.controls) >= 2:
        multi = any(lp.loop.mobility > 1 for lp in rig.plan.loops)
        if multi:
            sweep_pair(rig, rig.controls[0], rig.controls[1], faults)

    notes = [f for f in faults if f.startswith("(snapped)")]
    faults = [f for f in faults if not f.startswith("(snapped)")]
    head = "%-28s %d control(s), %d loop(s)" % (
        name, len(rig.controls), len(rig.pairs))
    tail = ""
    if notes:
        tail = "\n    " + "\n    ".join(notes)
    if not faults:
        return head + "  clean   [" + "; ".join(travel) + "]" + tail
    return (head + "  %d FAULT(S)\n    " % len(faults)
            + "\n    ".join(faults) + tail)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    grid = "--grid" in argv
    argv = [a for a in argv if not a.startswith("--")]
    paths = []
    for arg in argv:
        if os.path.isdir(arg):
            paths.extend(sorted(glob.glob(os.path.join(
                arg, "**", "*.rig.json"), recursive=True)))
        else:
            paths.extend(sorted(glob.glob(arg)))
    if not paths:
        raise SystemExit("rig_sweep: nothing to sweep. Pass a manifest or a "
                         "directory after --")

    bad = 0
    for path in paths:
        line = survey(path, grid)
        print("rig_sweep| " + line)
        sys.stdout.flush()
        if "FAULT" in line or "BUILD FAILED" in line:
            bad += 1
    print("rig_sweep| %d manifest(s), %d with something to look at"
          % (len(paths), bad))


main()
