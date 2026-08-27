# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometry attachment: matched objects -> bones, directly.

An earlier design put one GRP_ empty per rigid group between the bone and
the geometry. Dropped 2026-08-22: the empties bought nothing the RIG_*
object tags do not already provide (identity survives on the objects, not
on the middleman) and they cluttered the outliner. Legacy GRP_ empties are
still recognised and cleaned up by the rig rebuild (they carry RIG_rig).

BONE parenting evaluates at the bone TAIL, not the head, and against the
POSE matrix, so the parent matrix is computed deterministically as
    P = armature.matrix_world @ pose_bone.matrix @ Translation((0, length, 0))
and matrix_parent_inverse / matrix_basis are set so matrix_world is
byte-for-byte preserved. Guessing with ops or leaving Blender to compute
the inverse gives a rig where every part jumps by one bone length.

pose_bone.matrix, NOT bone.matrix_local: the two only agree at rest, and a
freshly built rig is not guaranteed to BE at rest — a limit constraint
whose range excludes the current pose clamps the bone the moment it
evaluates (live corpus 07, 2026-08-23: a mis-signed manifest limit shoved
the leaf bone 40° and the rest-matrix formula dragged the geometry with
it). The rest-matrix formula silently bakes any such clamp into the
geometry; the pose-matrix formula preserves the geometry no matter what
the constraints did, and report.posed_bones says loudly that a bone was
off rest while relinking.

Big-scene pattern: set ALL parents first, ONE view_layer.update(), then set
all matrices — the per-object update Blender does implicitly otherwise is
quadratic in scene size.
"""

from dataclasses import dataclass, field
from typing import List, Tuple

try:
    import bpy
    from mathutils import Matrix
except ImportError:
    bpy = None
    Matrix = None

_DRIFT_TOL = 1e-6


@dataclass
class ParentReport:
    bone_parented: int = 0
    missing_groups: List[str] = field(default_factory=list)
    # (object name, drift in Blender units); collected, never raised —
    # aborting mid-scene would leave half the assembly re-parented.
    violations: List[Tuple[str, float]] = field(default_factory=list)
    # Objects the rig drives nothing of, hung off the ground bone so the
    # assembly stays whole when the rig is moved.
    grounded: List[str] = field(default_factory=list)
    # (bone name, metres its head sits from rest at relink time). A bone off
    # rest before anyone posed it means a constraint rejects the rest pose —
    # almost always a manifest limit whose value_at_rest lies outside its
    # own min/max. Geometry is preserved regardless; this is the tell.
    posed_bones: List[Tuple[str, float]] = field(default_factory=list)


def _bone_parent_matrix(arm_obj, bone_name):
    pb = arm_obj.pose.bones[bone_name]
    bone = arm_obj.data.bones[bone_name]
    return (arm_obj.matrix_world
            @ pb.matrix
            @ Matrix.Translation((0.0, bone.length, 0.0)))


def _rig_maps(arm_obj):
    bone_by_group = {}
    for pb in arm_obj.pose.bones:
        gid = pb.get("RIG_group")
        if gid and "RIG_helper" not in pb.keys():
            bone_by_group[gid] = pb.name
    # Group ids are per-manifest and start at g000 every time, so a scene
    # holding two rigged assemblies has two of everything. An object says
    # which manifest tagged it; one that predates the tag (or came from a
    # foreign importer) is still taken, because there is nothing better to
    # go on and one rig in a scene is the common case.
    source = arm_obj.get("RIG_source") or None
    geometry = {}
    for obj in bpy.data.objects:
        if obj.get("RIG_group_empty") or obj.get("RIG_rig"):
            continue    # legacy rig empties and the armature itself
        gid = obj.get("RIG_group")
        if not gid:
            continue
        theirs = obj.get("RIG_source") or None
        if source and theirs and theirs != source:
            continue
        geometry.setdefault(gid, []).append(obj)
    return bone_by_group, geometry


def _prototype_collections():
    """Collections some object instances. Their contents are a TEMPLATE, not
    scene geometry: the collection-instance import mode keeps every part
    once in a hidden collection and puts empties where the occurrences are.
    Parenting a template to a bone moves it inside every instance at once."""
    out = set()
    for obj in bpy.data.objects:
        col = getattr(obj, "instance_collection", None)
        if col is not None:
            out.add(col.name)
            for child in col.children_recursive:
                out.add(child.name)
    return out


def _home_subtree(arm_obj):
    """The collections under the one the rig lives in, or None when it lives
    loose at the scene root and there is no subtree to speak of.

    This is what keeps a rig to its own assembly: the same file imported
    twice makes two collections, and each rig may only adopt what is inside
    its own.
    """
    rig_cols = {c.name for c in arm_obj.users_collection}
    for col in bpy.data.collections:
        if rig_cols & {c.name for c in col.children}:
            return {col.name} | {c.name for c in col.children_recursive}
    return None


def _leftovers(arm_obj, plan_objects, files):
    """Imported objects this rig drives nothing of, and that hang from
    nothing — the empties an import made, and any part matching could not
    place.

    They are the reason a rig looked like it half-worked: bone-parented
    geometry follows the armature and everything else stays behind in world
    space, so moving the rig tore the assembly in two. Hanging them off the
    ground bone keeps the machine whole; they simply do not articulate.

    Only objects from the same STEP file(s) as the geometry this rig drives,
    so a second import sitting in the same scene is never adopted.
    """
    if not files:
        return []
    driven = {obj.name for obj in plan_objects}
    prototypes = _prototype_collections()
    home = _home_subtree(arm_obj)
    out = []
    for obj in bpy.data.objects:
        if obj.name in driven or obj.parent is not None:
            continue
        if (obj.get("RIG_rig") or obj.get("RIG_helper")
                or obj.get("SWTB_widget") or obj.get("RIG_group_empty")):
            continue
        if obj.get("STEP_file") not in files:
            continue
        if any(c.name in prototypes for c in obj.users_collection):
            continue
        if home is not None and not any(c.name in home
                                        for c in obj.users_collection):
            continue
        out.append(obj)
    return out


def relink(context, arm_obj) -> ParentReport:
    """(Re-)parents everything the scene tags point at. Works from custom
    properties only — bone and object names are display labels that Blender
    rewrites on collision — so it runs identically after a file round-trip
    with no session state. Intended to run at rest pose: the parent matrix
    uses the bone's rest position, so a posed rig would record drift."""
    report = ParentReport()
    # The parent matrices read evaluated pose state (pose_bone.matrix
    # includes constraints), so the depsgraph must be current before
    # anything is captured.
    context.view_layer.update()
    bone_by_group, geometry = _rig_maps(arm_obj)

    plan = []  # (obj, bone_name, world_before)
    for gid in sorted(set(bone_by_group) | set(geometry)):
        bone_name = bone_by_group.get(gid)
        if bone_name is None:
            report.missing_groups.append(gid)
            continue
        for obj in geometry.get(gid, []):
            plan.append((obj, bone_name, obj.matrix_world.copy()))

    for bone_name in sorted({name for _, name, _ in plan}):
        pb = arm_obj.pose.bones[bone_name]
        rest = arm_obj.data.bones[bone_name].matrix_local
        delta = rest.inverted() @ pb.matrix
        # A limit clamp usually rotates about the bone head, so the head
        # barely moves — the rotation angle is the sensitive measure.
        off = max((pb.matrix.translation - rest.translation).length,
                  abs(delta.to_quaternion().angle))
        if off >= 1e-5:
            report.posed_bones.append((bone_name, off))
            print("[SWTB relink] bone %s sits off its rest pose while "
                  "relinking (%.4f rad/m) — a constraint rejects the rest "
                  "pose (check that joint's limits vs value_at_rest); "
                  "geometry keeps its place regardless" % (bone_name, off))

    # Whatever the import left over rides the ground bone: the assembly
    # stays one object when the rig is moved, instead of half of it walking
    # away. Same world-preserving parenting as everything else, so nothing
    # shifts by a millimetre when it happens.
    ground = arm_obj.get("RIG_ground_bone")
    if ground and ground in arm_obj.pose.bones:
        files = {obj.get("STEP_file") for obj, _, _ in plan}
        files.discard(None)
        for obj in _leftovers(arm_obj, [o for o, _, _ in plan], files):
            plan.append((obj, ground, obj.matrix_world.copy()))
            report.grounded.append(obj.name)

    for obj, bone_name, _ in plan:
        obj.parent = arm_obj
        obj.parent_type = "BONE"
        obj.parent_bone = bone_name
        obj["RIG_parent_mode"] = "BONE"
        report.bone_parented += 1

    context.view_layer.update()

    for obj, bone_name, world_before in plan:
        p = _bone_parent_matrix(arm_obj, bone_name)
        obj.matrix_parent_inverse = p.inverted()
        # world = P @ parent_inverse @ basis, and parent_inverse is P^-1,
        # so restoring the world transform is exactly basis = world_before.
        obj.matrix_basis = world_before

    context.view_layer.update()

    for obj, _, world_before in plan:
        drift = (obj.matrix_world.translation - world_before.translation).length
        if drift >= _DRIFT_TOL:
            report.violations.append((obj.name, drift))

    return report
