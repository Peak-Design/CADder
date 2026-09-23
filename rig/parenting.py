# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometry attachment: matched objects -> bones, directly.

An earlier design put one GRP_ empty per rigid group between the bone and
the geometry. Dropped 2026-08-22: the empties bought nothing the RIG_*
object tags do not already provide (identity survives on the objects, not
on the middleman) and they cluttered the outliner. Legacy GRP_ empties are
still recognized and cleaned up by the rig rebuild (they carry RIG_rig).

BONE parenting evaluates at the bone TAIL, not the head, and against the
POSE matrix, so the parent matrix is computed deterministically as
    P = armature.matrix_world @ pose_bone.matrix @ Translation((0, length, 0))
and matrix_parent_inverse / matrix_basis are set so matrix_world is
byte-for-byte preserved. Guessing with ops or leaving Blender to compute
the inverse gives a rig where every part jumps by one bone length.

pose_bone.matrix, NOT bone.matrix_local: the two only agree at rest, and a
freshly built rig is not guaranteed to BE at rest: a limit constraint
whose range excludes the current pose clamps the bone the moment it
evaluates (live corpus 07, 2026-08-23: a mis-signed manifest limit shoved
the leaf bone 40° and the rest-matrix formula dragged the geometry with
it). The rest-matrix formula silently bakes any such clamp into the
geometry. The pose-matrix formula preserves the geometry no matter what
the constraints did, and report.posed_bones says loudly that a bone was
off rest while relinking.

Big-scene pattern: set ALL parents first, ONE view_layer.update(), then set
all matrices: the per-object update Blender does implicitly otherwise is
quadratic in scene size.
"""

import math
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
    # (object name, drift in Blender units). Collected, never raised:
    # aborting mid-scene would leave half the assembly re-parented.
    violations: List[Tuple[str, float]] = field(default_factory=list)
    # Objects the rig drives nothing of, hung off the ground bone so the
    # assembly stays whole when the rig is moved.
    grounded: List[str] = field(default_factory=list)
    # (bone name, metres its head sits from rest at relink time). A bone off
    # rest before anyone posed it means a constraint rejects the rest pose:
    # almost always a manifest limit whose value_at_rest lies outside its
    # own min/max. Geometry is preserved regardless. This is the tell.
    posed_bones: List[Tuple[str, float]] = field(default_factory=list)
    # Tree empties hung on a bone with their parts still under them: a
    # subassembly the rig moves as one body keeps its place in the tree.
    carried: List[str] = field(default_factory=list)
    # Tree empties that held nothing once the parts were on their bones.
    removed_empties: int = 0


def _bone_parent_matrix(arm_obj, bone_name):
    pb = arm_obj.pose.bones[bone_name]
    bone = arm_obj.data.bones[bone_name]
    return (arm_obj.matrix_world
            @ pb.matrix
            @ Matrix.Translation((0.0, bone.length, 0.0)))


def rig_sources(arm_obj):
    """Every manifest this armature holds bones for.

    One, until rigs are joined. A joined armature carries several, and the
    bones say individually which one they came from.
    """
    found = []
    for value in [arm_obj.get("RIG_source")] + list(
            arm_obj.get("RIG_sources") or []):
        if value and value not in found:
            found.append(value)
    for pb in arm_obj.pose.bones:
        value = pb.get("RIG_source")
        if value and value not in found:
            found.append(value)
    return found


def _rig_maps(arm_obj):
    """(source, group id) -> bone name, and the same key -> the objects.

    Group ids restart at g000 for every manifest, so the id alone stops
    naming a bone the moment two assemblies share an armature: half the
    geometry would re-parent to the other assembly's bones. The manifest a
    bone and an object came from is recorded on both, and the pair is what
    identifies a body.

    An untagged object or bone predates the tag, or came from a foreign
    importer. Those keep the old behaviour (matched on the group id alone)
    because there is nothing better to go on, and one rig in one armature is
    still the common case.
    """
    sources = rig_sources(arm_obj)
    bone_by_key = {}
    for pb in arm_obj.pose.bones:
        gid = pb.get("RIG_group")
        if gid and "RIG_helper" not in pb.keys():
            bone_by_key[(pb.get("RIG_source") or None, gid)] = pb.name
    for pb in arm_obj.pose.bones:
        # A screw's TURN is on a bone of its own, below the body's, and
        # that is where its geometry belongs: the body's own bone carries
        # the slide and nothing else (rig_build's spin_names).
        gid = pb.get("RIG_spin")
        if gid:
            bone_by_key[(pb.get("RIG_source") or None, gid)] = pb.name

    geometry = {}
    for obj in bpy.data.objects:
        if obj.get("RIG_group_empty") or obj.get("RIG_rig"):
            continue    # legacy rig empties and the armature itself
        gid = obj.get("RIG_group")
        if not gid:
            continue
        theirs = obj.get("RIG_source") or None
        if sources and theirs and theirs not in sources:
            continue
        geometry.setdefault((theirs, gid), []).append(obj)
    return bone_by_key, geometry


def _bone_for(bone_by_key, key):
    """The bone an object belongs on, allowing for either side being
    untagged: an exact (source, group) match first, then the same group id
    when only ONE bone claims it, which is every rig that has not been
    joined to another."""
    if key in bone_by_key:
        return bone_by_key[key]
    _source, gid = key
    same = [name for (_s, g), name in bone_by_key.items() if g == gid]
    return same[0] if len(same) == 1 else None


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
    nothing: the empties an import made, and any part matching could not
    place.

    They are the reason a rig looked like it half-worked: bone-parented
    geometry follows the armature and everything else stays behind in world
    space, so moving the rig tore the assembly in two. Hanging them off the
    ground bone keeps the machine whole. They simply do not articulate.

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
                or obj.get("CADLINK_widget") or obj.get("RIG_group_empty")):
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
    properties only: bone and object names are display labels that Blender
    rewrites on collision, so it runs identically after a file round-trip
    with no session state. Intended to run at rest pose: the parent matrix
    uses the bone's rest position, so a posed rig would record drift."""
    report = ParentReport()
    # The parent matrices read evaluated pose state (pose_bone.matrix
    # includes constraints), so the depsgraph must be current before
    # anything is captured.
    context.view_layer.update()
    bone_by_key, geometry = _rig_maps(arm_obj)

    plan = []  # (obj, bone_name, world_before)
    for key in sorted(set(bone_by_key) | set(geometry),
                      key=lambda k: (k[0] or "", k[1])):
        bone_name = _bone_for(bone_by_key, key)
        if bone_name is None:
            report.missing_groups.append(key[1])
            continue
        for obj in geometry.get(key, []):
            plan.append((obj, bone_name, obj.matrix_world.copy()))

    for bone_name in sorted({name for _, name, _ in plan}):
        pb = arm_obj.pose.bones[bone_name]
        rest = arm_obj.data.bones[bone_name].matrix_local
        delta = rest.inverted() @ pb.matrix
        # A limit clamp usually rotates about the bone head, so the head
        # barely moves: the rotation angle is the sensitive measure.
        off = max((pb.matrix.translation - rest.translation).length,
                  abs(delta.to_quaternion().angle))
        if off >= 1e-5:
            report.posed_bones.append((bone_name, off))
            print("[CADLink relink] bone %s sits off its rest pose while "
                  "relinking (%.4f rad/m): a constraint rejects the rest "
                  "pose (check that joint's limits against value_at_rest). "
                  "Geometry keeps its place regardless" % (bone_name, off))

    plan, report.carried, doomed = _keep_tree(plan)

    # Whatever the import left over rides the ground bone: the assembly
    # stays one object when the rig is moved, instead of half of it walking
    # away. Same world-preserving parenting as everything else, so nothing
    # shifts by a millimetre when it happens.
    ground = arm_obj.get("RIG_ground_bone")
    if ground and ground in arm_obj.pose.bones:
        files = {obj.get("STEP_file") for obj, _, _ in plan}
        files.discard(None)
        going = {e.name for e in doomed}
        for obj in _leftovers(arm_obj, [o for o, _, _ in plan], files):
            if obj.name in going:
                continue
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

    report.removed_empties = _remove_bare(doomed)
    return report


@dataclass
class PoseOntoReport:
    # (object name, metres it moved) for each object that reached its pose.
    moved: List[Tuple[str, float]] = field(default_factory=list)
    already_ok: int = 0
    # (object name, metres it is still off its pose): the rig does not let
    # it get there.
    held: List[Tuple[str, float]] = field(default_factory=list)


_POSE_T_TOL = 1e-4    # metres
_POSE_R_TOL = 1e-3    # radians


def bone_of(obj, arm_obj):
    """The bone of `arm_obj` this object rides, directly or through the
    tree empties above it, or None."""
    holder = obj
    while holder is not None:
        parent = holder.parent
        if parent is not None and parent == arm_obj \
                and holder.parent_type == "BONE":
            return holder.parent_bone or None
        holder = parent
    return None


def _same(a, b, tol=1e-6):
    return all(abs(a[i][j] - b[i][j]) <= tol for i in range(4) for j in range(4))


def _handle_of(arm_obj, name):
    """The bone to pose so that the bone `name` moves: that bone, or the
    bone it follows. The turn of a screw follows the slide of the body's
    own bone. The hidden DEF bone of a ball, or of a cone on a plane,
    follows the handle beside it: same joint, same parent, same rest."""
    pb = arm_obj.pose.bones.get(name)
    if pb is None:
        return None
    if pb.get("RIG_spin") and pb.parent is not None:
        return pb.parent
    joint = pb.get("RIG_joint")
    if joint and pb.get("RIG_group"):
        parent = pb.parent.name if pb.parent is not None else None
        rest = pb.bone.matrix_local
        for other in arm_obj.pose.bones:
            if other.name == pb.name or other.get("RIG_joint") != joint \
                    or other.get("RIG_group"):
                continue
            other_parent = other.parent.name if other.parent is not None else None
            if other_parent == parent and _same(other.bone.matrix_local, rest):
                return other
    return pb


def _depth(pb):
    depth = 0
    parent = pb.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def _pose_unlocked(pb, basis):
    """Puts `basis` on the channels of the bone that are not locked. A
    locked channel keeps its value: it is not the user's to pose, and a
    driver writes it if anything does. This is the pose the user could give
    the bone by hand."""
    loc, rot, _scale = basis.decompose()
    for i in range(3):
        if not pb.lock_location[i]:
            pb.location[i] = loc[i]
    mode = pb.rotation_mode
    if mode == "QUATERNION":
        if not all(pb.lock_rotation):
            pb.rotation_quaternion = rot
    elif mode == "AXIS_ANGLE":
        if not all(pb.lock_rotation):
            axis, angle = rot.to_axis_angle()
            pb.rotation_axis_angle = (angle, axis[0], axis[1], axis[2])
    else:
        euler = rot.to_euler(mode, pb.rotation_euler)
        for i in range(3):
            if not pb.lock_rotation[i]:
                pb.rotation_euler[i] = euler[i]


def _off(now, target, scene_scale):
    """(metres, radians) between two world matrices."""
    metres = (now.translation - target.translation).length * scene_scale
    a = now.to_3x3().normalized().to_quaternion()
    b = target.to_3x3().normalized().to_quaternion()
    turn = a.rotation_difference(b).angle
    return metres, min(turn, 2.0 * math.pi - turn)


def _plain(pb):
    """True for a bone whose pose is its parent's pose, its rest offset
    and its own channels, with nothing else in between: the bones the
    chain solve below can compute by itself."""
    bone = pb.bone
    return (not bone.use_connect and bone.inherit_scale == "FULL"
            and bone.use_inherit_rotation and bone.use_local_location
            and pb.rotation_mode not in ("QUATERNION", "AXIS_ANGLE"))


def _channels(pb):
    """The unlocked channels of a bone, as ("loc" | "rot", axis) pairs."""
    out = [("loc", i) for i in range(3) if not pb.lock_location[i]]
    out += [("rot", i) for i in range(3) if not pb.lock_rotation[i]]
    return out


def _carriers_above(pb, wanted, carrying, solved):
    """The bones above `pb` that must move for it to reach its pose and
    that nothing else places: no pose of their own, no part on them. The
    carrier of a contact the exporter split in two is such a bone. The
    list runs from the top down, and it is empty when `pb` can reach its
    pose alone."""
    chain = []
    parent = pb.parent
    while (parent is not None and parent.name not in wanted
           and parent.name not in carrying and parent.name not in solved
           and _plain(parent) and _channels(parent)):
        chain.append(parent)
        parent = parent.parent
    if not chain or not _plain(pb):
        return []
    chain.reverse()
    return chain


def _solve(linear, rhs):
    """x for linear @ x = rhs, by elimination with pivoting. None when the
    system is singular."""
    n = len(rhs)
    a = [list(linear[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-18:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(n):
            if r != col:
                f = a[r][col] / a[col][col]
                if f:
                    for c in range(col, n + 1):
                        a[r][c] -= f * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def _solve_chain(chain, target):
    """Poses the bones of `chain` (top down, the last one the bone with
    the pose `target`, in armature space) so that the last one reaches it.
    Only their unlocked channels change. A small least-squares solve over
    those channels, from the pose the bones have now. The constraints of
    the bones are not part of it: the depsgraph applies them afterwards,
    and the check that follows reports what they would not allow."""
    from mathutils import Euler, Vector
    top = chain[0].parent
    above = top.matrix.copy() if top is not None else Matrix.Identity(4)
    links = []
    for pb in chain:
        rest = pb.bone.matrix_local
        if pb.parent is not None:
            rest = pb.parent.bone.matrix_local.inverted() @ rest
        links.append(rest)
    params = []
    start = []
    for k, pb in enumerate(chain):
        for kind, axis in _channels(pb):
            params.append((k, kind, axis))
            start.append(pb.location[axis] if kind == "loc"
                         else pb.rotation_euler[axis])
    if not params:
        return
    length = max(sum(pb.bone.length for pb in chain), 1e-6)

    def forward(values):
        loc = [list(pb.location) for pb in chain]
        rot = [list(pb.rotation_euler) for pb in chain]
        for (k, kind, axis), v in zip(params, values):
            (loc if kind == "loc" else rot)[k][axis] = v
        m = above
        for k, pb in enumerate(chain):
            basis = Matrix.LocRotScale(
                Vector(loc[k]), Euler(rot[k], pb.rotation_mode), pb.scale)
            m = m @ links[k] @ basis
        return m

    def residual(values):
        m = forward(values)
        out = [m[i][j] - target[i][j] for i in range(3) for j in range(3)]
        out += [(m[i][3] - target[i][3]) / length for i in range(3)]
        return out

    x = list(start)
    r = residual(x)
    err = sum(v * v for v in r)
    damping = 1e-3
    for _ in range(100):
        if err < 1e-20:
            break
        jac = []
        for i in range(len(x)):
            nudged = list(x)
            nudged[i] += 1e-7
            jac.append([(a - b) / 1e-7 for a, b in zip(residual(nudged), r)])
        n = len(x)
        normal = [[sum(jac[i][e] * jac[j][e] for e in range(len(r)))
                   + (damping if i == j else 0.0) for j in range(n)]
                  for i in range(n)]
        grad = [-sum(jac[i][e] * r[e] for e in range(len(r))) for i in range(n)]
        step = _solve(normal, grad)
        if step is None:
            break
        trial = [a + b for a, b in zip(x, step)]
        tr = residual(trial)
        terr = sum(v * v for v in tr)
        if terr < err:
            x, r, err = trial, tr, terr
            damping = max(damping * 0.3, 1e-12)
        else:
            damping *= 10.0
            if damping > 1e8:
                break
    for (k, kind, axis), v in zip(params, x):
        if kind == "loc":
            chain[k].location[axis] = v
        else:
            chain[k].rotation_euler[axis] = v


def pose_onto(context, arm_obj, targets, scene_scale=1.0) -> PoseOntoReport:
    """Poses the rig so that each object in `targets` (object -> world
    matrix) lands on its matrix. The rig itself is not changed: its bones,
    their rest, its constraints, its animation and its place all stay.

    This is how a CAD pose reaches a part on a rig. Building the rig again
    from a manifest in which only the transforms had changed put every
    bone back where its joint was at the export: link 2 of an arm whose
    link 1 had turned then turned about where its pin used to be. A bone
    that is posed moves its joints with it, and its limits stay measured
    from the rest they were written for.

    Each bone takes the move of the first of its objects: an object rides
    its bone rigidly, so where the object goes is where the bone goes.
    Parents are posed before their children, one level at a time, because
    a child's pose is read against where its parent now is. Only the
    unlocked channels change, and the constraints and drivers of the rig
    then do what they always do. A bone that no part rides (the carrier of
    a contact the exporter split in two) is posed with the bone below it.
    A bone that carries parts the reply did not name stays where it is. An
    object the rig cannot put on its matrix is in `held`, with how far off
    it stays."""
    report = PoseOntoReport()
    context.view_layer.update()
    world = arm_obj.matrix_world.copy()
    to_rig = world.inverted()
    start = {obj: obj.matrix_world.copy() for obj in targets}

    handles = {}

    def handle_of(bone):
        if bone not in handles:
            handles[bone] = _handle_of(arm_obj, bone)
        return handles[bone]

    carrying = set()
    for obj in bpy.data.objects:
        bone = bone_of(obj, arm_obj) if obj.get("RIG_component_id") else None
        if bone is not None and bone in arm_obj.pose.bones:
            handle = handle_of(bone)
            if handle is not None:
                carrying.add(handle.name)

    wanted = {}
    for obj in sorted(targets, key=lambda o: o.name):
        bone = bone_of(obj, arm_obj)
        if bone is None or bone not in arm_obj.pose.bones:
            continue
        handle = handle_of(bone)
        if handle is None or handle.name in wanted:
            continue
        move = targets[obj] @ start[obj].inverted()
        wanted[handle.name] = (to_rig @ move @ world
                               @ arm_obj.pose.bones[bone].matrix)

    levels = {}
    for name in wanted:
        levels.setdefault(_depth(arm_obj.pose.bones[name]), []).append(name)
    solved = set()
    for depth in sorted(levels):
        stale = False
        for name in sorted(levels[depth]):
            pb = arm_obj.pose.bones[name]
            chain = _carriers_above(pb, wanted, carrying, solved)
            if stale:
                # A carrier this level already solved can be above this
                # bone, and its pose is read below.
                context.view_layer.update()
                stale = False
            if chain:
                _solve_chain(chain + [pb], wanted[name])
                solved.update(c.name for c in chain)
                stale = True
                continue
            basis = arm_obj.convert_space(
                pose_bone=pb, matrix=wanted[name],
                from_space="POSE", to_space="LOCAL")
            _pose_unlocked(pb, basis)
        # The next level reads its parents' new pose.
        context.view_layer.update()

    for obj, target in targets.items():
        if bone_of(obj, arm_obj) is None:
            continue
        now = obj.matrix_world
        metres, turn = _off(now, target, scene_scale)
        if metres > _POSE_T_TOL or turn > _POSE_R_TOL:
            report.held.append((obj.name, metres))
            continue
        went, spun = _off(now, start[obj], scene_scale)
        if went > 1e-9 or spun > 1e-9:
            report.moved.append((obj.name, went))
        else:
            report.already_ok += 1
    return report


def _is_tree_empty(obj):
    """An empty an import made to hold part of the assembly tree. Not a
    part sent as a collection instance, and not anything of the rig's."""
    return (obj.type == "EMPTY" and obj.instance_collection is None
            and not (obj.get("RIG_rig") or obj.get("RIG_helper")
                     or obj.get("CADLINK_widget") or obj.get("RIG_group_empty")))


def _keep_tree(plan):
    """Keeps the assembly tree where the rig allows it.

    An empty whose parts all ride one bone is a subassembly that moves as
    one body, usually a rigid one. That empty goes on the bone, and its parts
    stay under it, so the tree survives the rig. An empty whose parts ride
    different bones cannot hold them: each part goes to its own bone, as
    before. That empty then holds nothing. When the bridge made it, it is
    returned to be removed. The empties of a STEP import stay, because
    matching reads a part's place in the assembly from the chain of empties
    above it (matching._occurrence_path). They ride the ground bone as
    leftovers, as before.

    Returns (the plan with the carried parts swapped for their empties,
    the names of the carried empties, the empties to check afterwards).
    """
    bone_of = {obj.name: bone for obj, bone, _ in plan}
    bones = {}          # empty name -> the bones of the parts under it
    empties = {}
    for obj, bone, _ in plan:
        parent = obj.parent
        while parent is not None and _is_tree_empty(parent):
            empties[parent.name] = parent
            bones.setdefault(parent.name, set()).add(bone)
            parent = parent.parent

    def carried(empty):
        return len(bones.get(empty.name, ())) == 1

    kept = []
    tops = {}
    for obj, bone, world in plan:
        # The highest empty above this part that rides one bone, when the
        # part's own parent is one.
        top = None
        parent = obj.parent
        while parent is not None and _is_tree_empty(parent) and carried(parent):
            top = parent
            parent = parent.parent
        if top is None:
            kept.append((obj, bone, world))
        elif top.name not in tops:
            tops[top.name] = (top, next(iter(bones[top.name])))
    for top, bone in tops.values():
        kept.append((top, bone, top.matrix_world.copy()))

    inside = set()
    for top, _ in tops.values():
        inside.update(c.name for c in top.children_recursive)
    doomed = [e for name, e in empties.items()
              if name not in tops and name not in inside
              and e.get("SWMESH_path") is not None]
    return kept, sorted(tops), doomed


def _remove_bare(empties):
    """Removes the tree empties that hold nothing now, deepest first, so a
    branch that held only empty branches goes too. An empty that still
    holds anything, a part or an object of the user's, stays."""
    removed = 0
    left = list(empties)
    while True:
        bare = []
        for e in left:
            try:
                if not e.children:
                    bare.append(e)
            except ReferenceError:
                continue
        if not bare:
            return removed
        for e in bare:
            left.remove(e)
            bpy.data.objects.remove(e, do_unlink=True)
            removed += 1
