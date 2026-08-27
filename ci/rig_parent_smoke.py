# SPDX-License-Identifier: GPL-3.0-or-later
"""Parenting an import to its rig, in every hierarchy mode.

The rig has to attach to what the user actually SEES, and leave everything
else exactly as they arranged it. Four import modes lay the same assembly
out four different ways — parts parented to empties, parts sorted into a
tree of collections, parts flat in one collection each, and one copy of
each part in a hidden collection with empties instancing it — so a
parenting rule that only holds in one of them is no rule at all.

What is checked, per mode:

  * collection membership is untouched: parenting is not a move, and a user
    who imported into a collection still has everything in it;
  * the rig lands INSIDE that collection, so one switch hides the whole
    machine — bones and parts together (live complaint, 2026-08-25: the rig
    sat at the scene root, and hiding the assembly left the bones behind);
  * nothing is stranded: every imported object either rides a bone of its
    own or hangs from something that does, so moving the rig moves the
    assembly whole;
  * posing a bone moves the geometry the user sees — read off the
    depsgraph, so an instanced part counts only if the INSTANCE moved;
  * a second relink changes nothing (it is the operator a user clicks
    twice).

    blender -b --factory-startup -P ci/rig_parent_smoke.py

The fixture is ci/fixtures/assembly.step (ci/fixtures/make_assembly.py
rebuilds it): a nested assembly with one product used twice, which is what
the tree, empties and instance modes need to differ at all.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON_DIR = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ADDON_DIR)
STEP = os.path.join(_HERE, "fixtures", "assembly.step")

import bpy
from mathutils import Vector

bpy.ops.preferences.addon_enable(module="STEPper_NEXT")
from STEPper_NEXT import main as m                                  # noqa: E402
from STEPper_NEXT.rig import (graph, manifest as mm, matching,      # noqa: E402
                              parenting, rig_build)

FAILS = []
MODES = ("EMPTIES", "TREE", "FLAT", "COLLECTION_INSTANCES")


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("   FAIL:", msg)
    return cond


def fresh():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for col in list(bpy.data.collections):
        bpy.data.collections.remove(col)
    vl = bpy.context.view_layer
    vl.active_layer_collection = vl.layer_collection


def membership():
    return {o.name: sorted(c.name for c in o.users_collection)
            for o in bpy.data.objects}


def prototype_names():
    """Objects that are a template for an instance rather than scene
    geometry — the instance mode's hidden originals."""
    cols = set()
    for obj in bpy.data.objects:
        if obj.instance_collection is not None:
            cols.add(obj.instance_collection.name)
            cols.update(c.name for c in obj.instance_collection.children_recursive)
    return {o.name for o in bpy.data.objects
            if any(c.name in cols for c in o.users_collection)}


def visible_parts():
    """What stands for an occurrence in this mode: the part object itself,
    or the empty that instances it."""
    protos = prototype_names()
    out = []
    for obj in bpy.data.objects:
        if obj.get("STEP_name") is None or obj.get("STEP_file") != STEP:
            continue
        if obj.instance_collection is not None:
            out.append(obj)
        elif obj.type == "MESH" and obj.name not in protos:
            out.append(obj)
    return sorted(out, key=lambda o: o.name)


def rows(matrix):
    return [[matrix[r][c] for c in range(4)] for r in range(4)]


def manifest_for(parts, source):
    """One component per occurrence, at the pose it was imported to, so
    matching resolves every one of them by transform rather than by luck."""
    comps, groups = [], []
    ground = min(range(len(parts)), key=lambda i: parts[i].get("STEP_name"))
    for i, obj in enumerate(parts):
        cid = "c%03d" % (i + 1)
        comps.append({"id": cid, "sw_path": "%s-%d" % (obj["STEP_name"], i),
                      "step_name": obj["STEP_name"],
                      "step_occurrence_path": None,
                      "transform": rows(obj.matrix_world)})
        groups.append({"id": "g%03d" % i, "name": obj["STEP_name"],
                       "components": [cid], "grounded": i == ground,
                       "frame": None, "bbox_diag": 0.05})
    joints = [{"id": "j%03d" % i, "type": "revolute",
               "parent_group": groups[ground]["id"],
               "child_group": groups[i]["id"],
               "origin": list(parts[i].matrix_world.translation),
               "axis": [0, 0, 1], "secondary_axis": [1, 0, 0], "limits": None}
              for i in range(len(groups)) if i != ground]
    data = {
        "manifest_version": "1.0.0",
        "generator": {"name": "rig_parent_smoke", "version": "1"},
        "units": {"length": "meter", "angle": "radian"},
        "frame": {"handedness": "right", "up_axis": "Z",
                  "transform_convention": "row_major_4x4_global"},
        "step_export": {"file": "assembly.step", "ap": "AP214",
                        "sha1": None, "occurrence_matching": None},
        "components": comps, "rigid_groups": groups, "joints": joints,
        "loops": [], "warnings": [],
    }
    return mm.parse(data, source_path=source), groups[ground]["id"]


def seen_geometry():
    """Where the geometry the user sees actually is, instances included.

    The corners of each part, NOT its origin: a revolute turns a part about
    its own origin, so an origin that has not moved says nothing about
    whether the part turned.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    out = {}
    for inst in dg.object_instances:
        obj = inst.object
        if obj.type != "MESH":
            continue
        key = "%s@%s" % (obj.name, inst.parent.name if inst.is_instance else "")
        mat = inst.matrix_world.copy()
        out.setdefault(key, []).extend(mat @ Vector(c) for c in obj.bound_box)
    return out


def layer_collection(view_layer, name):
    def find(lc):
        if lc.collection.name == name:
            return lc
        for child in lc.children:
            hit = find(child)
            if hit is not None:
                return hit
        return None
    return find(view_layer.layer_collection)


def ancestors_parented(obj):
    """Whether this object rides the rig, directly or through its parents."""
    seen = set()
    while obj is not None and obj.name not in seen:
        seen.add(obj.name)
        if obj.parent_type == "BONE" and obj.parent_bone:
            return True
        obj = obj.parent
    return False


OTHER = os.path.join(_HERE, "fixtures", "multisolid.step")


def run(htypes, wrapper, bystander=False):
    label = "%s%s%s" % (htypes, "" if wrapper else " (no wrapper collection)",
                        " + a second import" if bystander else "")
    print("\n-- %s" % label)
    fresh()
    m.load_step(bpy.context, STEP, htypes=htypes, up_as="Z", apply_scale=True,
                group_in_collection=wrapper)
    other = {}
    if bystander:
        # Another assembly living in the same scene. This rig must not
        # adopt one object of it: group ids restart at g000 for every
        # manifest, and a stray part is a stray part whoever imported it.
        m.load_step(bpy.context, OTHER, htypes=htypes, up_as="Z",
                    apply_scale=True, group_in_collection=wrapper)
        other = {o.name: (o.parent.name if o.parent else None,
                          sorted(c.name for c in o.users_collection))
                 for o in bpy.data.objects
                 if o.get("STEP_file") == OTHER}
        check(other, "%s: the second import made nothing" % label)

    before = membership()
    parts = visible_parts()
    check(len(parts) == 4, "%s: %d occurrences, expected 4 (%s)"
          % (label, len(parts), [o.name for o in parts]))

    man, ground_gid = manifest_for(parts, source="%s|%s" % (STEP, label))
    report = matching.match(man)
    check(len(report.matched) == len(man.components),
          "%s: matched %d of %d components"
          % (label, len(report.matched), len(man.components)))

    plan = graph.build(man)
    result = rig_build.build(bpy.context, man, plan)
    arm = result.armature_object
    prep = parenting.relink(bpy.context, arm)
    check(not prep.violations,
          "%s: %d objects drifted while parenting" % (label, len(prep.violations)))

    # 1) Collection membership is untouched.
    after = membership()
    for name, cols in sorted(before.items()):
        check(after.get(name) == cols,
              "%s: %s moved from %s to %s" % (label, name, cols, after.get(name)))

    # 2) The rig lives with the assembly it drives.
    owner = [c.name for c in bpy.data.collections
             if result.collection.name in [x.name for x in c.children]]
    import_cols = {c.name for c in bpy.data.collections
                   if c.get("STEP_file") and c.name != result.collection.name}
    if import_cols:
        check(owner and owner[0] in import_cols,
              "%s: rig collection sits in %s, not with the import (%s)"
              % (label, owner or ["<scene root>"], sorted(import_cols)))

        # ...so hiding the import hides the whole machine, bones included.
        vl = bpy.context.view_layer
        top = layer_collection(vl, owner[0])
        top.exclude = True
        vl.update()
        visible = {o.name for o in vl.objects}
        top.exclude = False
        vl.update()
        for obj in parts + [arm]:
            check(obj.name not in visible,
                  "%s: %s still shows with the assembly hidden"
                  % (label, obj.name))

    # 3) Nothing is stranded in world space.
    for obj in bpy.data.objects:
        if obj.get("STEP_file") != STEP or obj.name in prototype_names():
            continue
        check(ancestors_parented(obj),
              "%s: %s rides nothing — moving the rig would leave it behind"
              % (label, obj.name))

    # 4) Posing a bone moves what the user sees, and only that.
    before_geo = seen_geometry()
    moving = [gid for gid in result.bone_names if gid != ground_gid]
    posed = sorted(moving)[0]
    pb = arm.pose.bones[result.bone_names[posed]]
    pb.rotation_mode = "XYZ"
    # A bone's DOF is its LOCAL Y: rig_build rests every bone with its
    # joint axis along Y, and the constraints lock the other two.
    pb.rotation_euler[1] = 0.7
    bpy.context.view_layer.update()
    after_geo = seen_geometry()
    check(sorted(before_geo) == sorted(after_geo),
          "%s: posing changed WHICH geometry exists" % label)
    moved = {k: max((a - b).length for a, b in zip(before_geo[k], after_geo[k]))
             for k in before_geo if k in after_geo}
    check(any(d > 1e-4 for d in moved.values()),
          "%s: posing %s moved nothing at all (%s)" % (label, posed, moved))
    still = [k for k, d in moved.items() if d <= 1e-4]
    check(still, "%s: posing one joint moved the whole assembly" % label)
    print("   posed %s: %s" % (result.bone_names[posed],
                               {k: round(v, 4) for k, v in sorted(moved.items())}))
    pb.rotation_euler[1] = 0.0
    bpy.context.view_layer.update()

    # 5) Relinking twice is relinking once.
    world = {o.name: o.matrix_world.copy() for o in bpy.data.objects}
    parents = {o.name: (o.parent.name if o.parent else None, o.parent_bone)
               for o in bpy.data.objects}
    again = parenting.relink(bpy.context, arm)
    check(not again.violations, "%s: a second relink drifted" % label)
    for obj in bpy.data.objects:
        check((obj.parent.name if obj.parent else None,
               obj.parent_bone) == parents[obj.name],
              "%s: %s changed parent on the second relink" % (label, obj.name))
        d = (obj.matrix_world.translation
             - world[obj.name].translation).length
        check(d < 1e-6, "%s: %s moved %.6f on the second relink"
              % (label, obj.name, d))
    for name, was in sorted(other.items()):
        obj = bpy.data.objects.get(name)
        check(obj is not None
              and (obj.parent.name if obj.parent else None) == was[0]
              and sorted(c.name for c in obj.users_collection) == was[1],
              "%s: the other import's %s was disturbed" % (label, name))

    print("   ok: %d bone-parented, %d grounded leftovers, %s"
          % (prep.bone_parented, len(prep.grounded),
             "rig in " + (owner[0] if owner else "<scene root>")))


def main():
    for htypes in MODES:
        run(htypes, wrapper=True)
    # The wrapper is optional, and without it there may be nothing to nest
    # the rig inside — that must degrade to the scene root, not break.
    run("EMPTIES", wrapper=False)
    run("TREE", wrapper=False)
    # A scene with two assemblies in it: neither rig may reach into the
    # other, however alike their group ids are.
    run("EMPTIES", wrapper=True, bystander=True)
    run("TREE", wrapper=True, bystander=True)

    if FAILS:
        print("\nrig_parent_smoke: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("\nrig_parent_smoke: OK - every import mode parents to its rig, "
          "keeps its collections, and hides as one")


if __name__ == "__main__":
    main()
