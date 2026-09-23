# SPDX-License-Identifier: GPL-3.0-or-later
"""Cam contacts: a follower held on the cam's own faces, live.

The exporter's relation probe tables a cam that turns about one fixed
axis. A cam that also translates (free in its plane, on a slide, on a
carrier) has more than one input and no table holds it, so the manifest
carries the cam path's faces instead (coupling kind "cam", SCHEMA.md) and
the rig reads the contact here, in Blender, whatever the cam does:

- a vertex follower is a hidden PRJ bone under the follower's base,
  resting outside the cam along the slide, with a Shrinkwrap in project
  mode that shoots it along the slide onto the cam faces. The follower's
  slide channel is driven from where it lands;
- a roller follower is the same against a copy of the faces pushed out
  by the roller's radius along their normals: the roller's centre keeps
  that distance from the cam. Sharp convex corners get the arc a rolling
  contact sees, so the roller does not sink into them;
- a flat face is a support function: its position is the extreme of the
  cam's points along the face normal, which separates into a table in the
  cam's rotation plus a linear term in its translation, both relative to
  the follower's base. A hidden REL bone under that base copies the cam's
  transform, the table reads REL's local rotation, and a hidden OFF bone
  between the base and the follower carries the translation term.

Everything is bone-local, so a follower mounted on another follower (the
rod in the lifter of the cam-follower samples) reads the cam relative to
its own base, and the up axis the import used makes no difference.
"""

import math

try:
    import bmesh
    import bpy
    from mathutils import Matrix, Vector
except ImportError:
    bmesh = None
    bpy = None
    Matrix = None
    Vector = None

_PREFIX = "CADLINK "
_HELPER_LENGTH_M = 0.02
# Every convex edge turning more than this gets the arc fan on the
# roller's offset copy, so each facet is pushed out by exactly the radius:
# averaged vertex normals put a flat facet's middle closer than the radius
# by r(1 - cos(half the turn)), a tenth of a millimetre on a 37.5 mm roller
# over a coarse profile (live cam-follower2, 2026-09-15).
_SHARP_RAD = math.radians(0.05)
_FAN_STEP_RAD = math.radians(6.0)
# How far outside the cam the projection starts, in cam sizes. The first
# face the ray meets is the one the follower rests on.
_MARGIN_FACTOR = 1.5
_SUPPORT_STEPS = 360


def _v(x):
    return Vector((float(x[0]), float(x[1]), float(x[2])))


def wanted(bp):
    """The follower's plan wants a contact: a prismatic tree joint with a
    cam coupling and a base to hang the helpers off."""
    j = bp.joint
    return (j is not None and j.coupling is not None and j.coupling.kind == "cam"
            and j.type == "prismatic" and j.axis is not None and j.origin is not None
            and bp.parent_group_id is not None)


def _group_diag(manifest, joint_id):
    j = manifest.joint_by_id().get(joint_id)
    if j is None:
        return None
    for g in manifest.rigid_groups:
        if g.id == j.child_group:
            return g.bbox_diag
    return None


def _contact(manifest, coupling, joint):
    """Where the follower touches the cam at rest (metres, manifest frame),
    which side of that point the cam lies on along the slide (s = +1: the
    cam is on the +axis side), and how far outside the cam a projection can
    start. The ray runs through the middle of the cam faces' extent along
    the cam axis: the faces are an extrusion along it, so the height does
    not change where the ray lands, and the middle is where it cannot slip
    past an edge (live cam-follower2, 2026-09-15: the vertex follower sits
    in the cam's bottom plane, on the band's edge; a roller may be taller
    than the cam is thick)."""
    a = _v(joint.axis).normalized()
    p = _v(coupling.follower_point)
    k = _v(coupling.cam_axis).normalized()
    pts = [_v(q) for q in coupling.cam_surface_points]
    if coupling.follower_kind != "flat" and abs(k.dot(a)) < 0.9:
        level = sum(q.dot(k) for q in pts) / len(pts)
        p = p + k * (level - p.dot(k))
    o = _v(coupling.cam_origin)
    s = 1.0 if (o - p).dot(a) >= 0.0 else -1.0
    reach = max((q - p).length for q in pts)
    diag = _group_diag(manifest, coupling.driver_joint) or 0.0
    margin = _MARGIN_FACTOR * max(reach, diag)
    return p, s, margin


def prj_rest(manifest, bp, unit_scale):
    """The projection bone's rest: on the slide line through the contact,
    outside the cam by the margin, local +Y pointing at the cam."""
    from . import rig_build
    c = bp.joint.coupling
    p, s, margin = _contact(manifest, c, bp.joint)
    a = _v(bp.joint.axis).normalized()
    head = p - a * (s * margin)
    m = rig_build._frame_matrix(a * s, bp.joint.secondary_axis, head)
    for i in range(3):
        m[i][3] *= unit_scale
    return m, s, margin * unit_scale


def add_helper_bones(edit_bones, eb, bp, plan, manifest, frame, unit_scale,
                     result, helpers_coll):
    """Phase 2 (Edit mode): the hidden bones one follower needs. PRJ for a
    vertex or a roller. REL and OFF for a flat face, OFF taking the
    follower's place under its parent and the follower hanging off OFF."""
    from . import rig_build
    parent = eb.parent
    if parent is None:
        result.warnings.append(
            "joint {}: cam contact skipped, the follower has no parent bone".format(bp.joint.id))
        return
    if bp.slide_name:
        result.warnings.append(
            "joint {}: cam contact skipped, the follower's slide belongs to a loop "
            "solver".format(bp.joint.id))
        return
    if bp.cam_prj_name:
        m, s, margin_bu = prj_rest(manifest, bp, unit_scale)
        hb = edit_bones.new(bp.cam_prj_name)
        hb.head = (0.0, 0.0, 0.0)
        hb.tail = (0.0, 1.0, 0.0)
        hb.matrix = frame @ m
        hb.length = _HELPER_LENGTH_M * unit_scale
        hb.use_connect = False
        hb.parent = parent
        helpers_coll.assign(hb)
        result.cam_prj_names[bp.group.id] = hb.name
        result.cam_prj_params[bp.group.id] = (s, margin_bu)
    if bp.cam_rel_name:
        cam_gid = plan.joint_group.get(bp.joint.coupling.driver_joint)
        cam_bp = plan.bone_by_group.get(cam_gid)
        if cam_bp is None:
            result.warnings.append(
                "joint {}: cam contact skipped, the cam's joint {} has no bone".format(
                    bp.joint.id, bp.joint.coupling.driver_joint))
            return
        rb = edit_bones.new(bp.cam_rel_name)
        rb.head = (0.0, 0.0, 0.0)
        rb.tail = (0.0, 1.0, 0.0)
        rb.matrix = frame @ rig_build._bone_rest_matrix(manifest, cam_bp, unit_scale)
        rb.length = _HELPER_LENGTH_M * unit_scale
        rb.use_connect = False
        rb.parent = parent
        helpers_coll.assign(rb)
        result.cam_rel_names[bp.group.id] = rb.name

        ob = edit_bones.new(bp.cam_off_name)
        ob.head = (0.0, 0.0, 0.0)
        ob.tail = (0.0, 1.0, 0.0)
        ob.matrix = eb.matrix.copy()
        ob.length = _HELPER_LENGTH_M * unit_scale
        ob.use_connect = False
        ob.parent = parent
        helpers_coll.assign(ob)
        eb.parent = ob
        result.cam_off_names[bp.group.id] = ob.name


def _offset(bm, radius, toward):
    """Pushes the faces out by radius along their normals, oriented so the
    face nearest `toward` (the roller's centre) faces it. A sharp convex
    edge is split so each side keeps its own normal, and the two sides are
    bridged by the arc of radius `radius` about the edge: that arc is where
    a rolling contact's centre goes round a corner, and without it the
    averaged-normal offset would let the roller sink into the corner."""
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    bm.normal_update()
    if not bm.faces:
        return
    best = min(bm.faces, key=lambda f: (f.calc_center_median() - toward).length_squared)
    if best.normal.dot(toward - best.calc_center_median()) < 0.0:
        bmesh.ops.reverse_faces(bm, faces=bm.faces)
        bm.normal_update()

    records = []
    sharp = []
    for e in bm.edges:
        if len(e.link_faces) != 2:
            continue
        f1, f2 = e.link_faces
        if f1.normal.angle(f2.normal, 0.0) <= _SHARP_RAD:
            continue
        # Convex: the far face lies behind the near face's plane. A concave
        # corner's two offsets overlap, and the first hit is the right one.
        if f1.normal.dot(f2.calc_center_median() - f1.calc_center_median()) > 0.0:
            continue
        sharp.append(e)
        records.append((f1.index, f2.index, e.verts[0].co.copy(), e.verts[1].co.copy()))
    if sharp:
        bmesh.ops.split_edges(bm, edges=sharp)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        bm.normal_update()

    normals = [v.normal.copy() for v in bm.verts]
    originals = [v.co.copy() for v in bm.verts]
    for v, n in zip(bm.verts, normals):
        v.co = v.co + n * radius

    # The corner verts of every split edge first, while the lookup tables
    # hold: the fans add verts and faces, which outdates them.
    corners = []
    for f1i, f2i, c0, c1 in records:
        ends = []
        for fi in (f1i, f2i):
            f = bm.faces[fi]
            ends.append([min(f.verts, key=lambda v: (originals[v.index] - c).length_squared)
                         for c in (c0, c1)])
        corners.append(ends)
    for ends in corners:
        (a0, a1), (b0, b1) = ends
        angle = max(normals[a0.index].angle(normals[b0.index], 0.0),
                    normals[a1.index].angle(normals[b1.index], 0.0))
        k = max(1, int(math.ceil(angle / _FAN_STEP_RAD)))
        rows = [[a0, a1]]
        for i in range(1, k):
            t = i / k
            row = []
            for va, vb in ((a0, b0), (a1, b1)):
                na, nb = normals[va.index], normals[vb.index]
                n = na.slerp(nb, t) if na.angle(nb, 0.0) > 1e-6 else na
                row.append(bm.verts.new(originals[va.index] + n * radius))
            rows.append(row)
        rows.append([b0, b1])
        for r0, r1 in zip(rows, rows[1:]):
            try:
                bm.faces.new((r0[0], r0[1], r1[1], r1[0]))
            except ValueError:
                pass
    bm.normal_update()


def _make_surface(collection, joint, coupling, frame, unit_scale):
    """The cam faces the follower rides, through the same scene frame as
    the bones. A roller rides the offset copy. Tagged RIG_rig (replaced on
    rebuild) and RIG_helper (invisible to matching and geometry
    parenting), drawn as wire so it does not fight the cam's own mesh."""
    verts = [frame @ (_v(p) * unit_scale) for p in coupling.cam_surface_points]
    bm = bmesh.new()
    bverts = [bm.verts.new(v) for v in verts]
    for t in coupling.cam_surface_triangles:
        try:
            bm.faces.new([bverts[i] for i in t])
        except ValueError:
            pass
    # Adjacent faces were tessellated apart and share their edge points
    # only by value: welding joins the band so corners are corners, not
    # gaps a ray could slip through.
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6 * unit_scale)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    if coupling.follower_kind == "roller":
        centre = frame @ (_v(coupling.follower_point) * unit_scale)
        _offset(bm, float(coupling.follower_radius) * unit_scale, centre)
    data = bpy.data.meshes.new("CADLINK_cam_" + joint.id)
    bm.to_mesh(data)
    bm.free()
    data.update()
    obj = bpy.data.objects.new(data.name, data)
    obj["RIG_rig"] = True
    obj["RIG_helper"] = joint.id
    obj.hide_render = True
    obj.display_type = "WIRE"
    collection.objects.link(obj)
    return obj


def make_surfaces(collection, arm_obj, manifest, plan, result, frame, unit_scale):
    """Object mode, after the bones exist: one cam surface per vertex or
    roller follower, riding the cam's bone with the same world-preserving
    parenting relinked geometry gets. Returns joint id -> object."""
    out = {}
    for bp in plan.bones:
        if not bp.cam_prj_name or not wanted(bp):
            continue
        j = bp.joint
        cam_gid = plan.joint_group.get(j.coupling.driver_joint)
        cam_bone = result.bone_names.get(cam_gid)
        if cam_bone is None or cam_bone not in arm_obj.data.bones:
            result.warnings.append(
                "joint {}: cam contact skipped, the cam's joint {} has no bone".format(
                    j.id, j.coupling.driver_joint))
            continue
        obj = _make_surface(collection, j, j.coupling, frame, unit_scale)
        obj.parent = arm_obj
        obj.parent_type = "BONE"
        obj.parent_bone = cam_bone
        bone = arm_obj.data.bones[cam_bone]
        # Armature space, as the bones are: the surface stands at the
        # armature's transform, so a rig the user moved keeps its cam on
        # the cam bone when an update builds inside it.
        p = bone.matrix_local @ Matrix.Translation((0.0, bone.length, 0.0))
        obj.matrix_parent_inverse = p.inverted()
        obj.matrix_basis = Matrix.Identity(4)
        out[j.id] = obj
        result.cam_surface_names[j.id] = obj.name
    return out


def _into_cam(coupling):
    """The flat face's normal pointing into the cam, and the face point."""
    n = _v(coupling.follower_normal).normalized()
    p0 = _v(coupling.follower_point)
    side = sum((_v(q) - p0).dot(n) for q in coupling.cam_surface_points)
    return (n if side >= 0.0 else -n), p0


def support_samples(coupling, joint, cam_axis, cam_origin):
    """The flat follower's slide as a table in the cam's rotation about
    cam_axis through cam_origin (manifest frame, metres, unit vectors):
    y(theta) is how far the face moves along the slide when the cam turns
    by theta with no translation, relative to rest. The face sits where the
    cam's extreme point along the face normal is, so the value is a minimum
    over the cam's points. Returns (samples, None) or (None, why)."""
    m, p0 = _into_cam(coupling)
    a = _v(joint.axis).normalized()
    den = a.dot(m)
    if abs(den) < 1e-6:
        return None, "the flat follower slides along its own face"
    k = _v(cam_axis).normalized()
    o = _v(cam_origin)
    rel = [_v(q) - o for q in coupling.cam_surface_points]

    def g(theta):
        r = Matrix.Rotation(theta, 3, k)
        return min((o + r @ q - p0).dot(m) for q in rel)

    g0 = g(0.0)
    samples = []
    for i in range(_SUPPORT_STEPS + 1):
        theta = 2.0 * math.pi * i / _SUPPORT_STEPS
        samples.append((theta, (g(theta) - g0) / den))
    samples[-1] = (2.0 * math.pi, samples[0][1])
    return samples, None


def offset_coefficients(coupling, joint, cam_rest3):
    """The translation term: the cam's location channels (in its own rest
    axes, Blender units) times these give the flat follower's extra slide
    in Blender units. Unit-free and frame-free: a dot product."""
    m, _ = _into_cam(coupling)
    a = _v(joint.axis).normalized()
    den = a.dot(m)
    if abs(den) < 1e-6:
        return None
    c = cam_rest3.transposed() @ m
    return [c[0] / den, c[1] / den, c[2] / den]


def _add_location_driver(arm_obj, pose_bone, source_bone, coef):
    try:
        pose_bone.driver_remove("location", 1)
    except (TypeError, RuntimeError):
        pass
    fcurve = pose_bone.driver_add("location", 1)
    driver = fcurve.driver
    driver.type = "SCRIPTED"
    while driver.variables:
        driver.variables.remove(driver.variables[0])
    for name, ttype in (("x", "LOC_X"), ("y", "LOC_Y"), ("z", "LOC_Z")):
        var = driver.variables.new()
        var.name = name
        var.type = "TRANSFORMS"
        target = var.targets[0]
        target.id = arm_obj
        target.bone_target = source_bone
        target.transform_type = ttype
        target.transform_space = "LOCAL_SPACE"
    driver.expression = "{!r}*x + {!r}*y + {!r}*z".format(
        float(coef[0]), float(coef[1]), float(coef[2]))
    return fcurve


def apply(arm_obj, manifest, plan, result, surfaces, unit_scale):
    """Phase 3 (Pose mode): the constraints and drivers. Returns warnings."""
    from . import drivers as drivers_mod, rig_build
    warnings = []
    pose = arm_obj.pose
    for bp in plan.bones:
        if not (bp.cam_prj_name or bp.cam_rel_name) or not wanted(bp):
            continue
        j = bp.joint
        c = j.coupling
        own = pose.bones.get(result.bone_names.get(bp.group.id, ""))
        if own is None:
            continue

        if bp.cam_prj_name:
            prj = pose.bones.get(result.cam_prj_names.get(bp.group.id, ""))
            obj = surfaces.get(j.id)
            if prj is None or obj is None:
                warnings.append(
                    "joint {}: cam contact skipped, no projection bone or cam "
                    "surface".format(j.id))
                continue
            prj["RIG_helper"] = j.id
            prj.lock_location = [True, True, True]
            prj.lock_rotation = [True, True, True]
            prj.lock_scale = [True, True, True]
            con = prj.constraints.new("SHRINKWRAP")
            con.name = _PREFIX + "On Cam"
            con.target = obj
            con.shrinkwrap_type = "PROJECT"
            con.project_axis = "POS_Y"
            con.project_axis_space = "LOCAL"
            con.project_limit = 0.0
            con.use_project_opposite = False
            con.cull_face = "OFF"
            con.distance = 0.0
            s, margin_bu = result.cam_prj_params[bp.group.id]
            # PRJ rests margin outside the contact with +Y at the cam, so
            # where it lands along its +Y minus the margin is how far the
            # follower moved along its own slide, in the cam's direction.
            fc = drivers_mod._add_driver(arm_obj, own, "location", prj.name, "LOC_Y", 1.0)
            fc.driver.expression = ("a - {!r}".format(margin_bu) if s > 0
                                    else "{!r} - a".format(margin_bu))
            continue

        rel = pose.bones.get(result.cam_rel_names.get(bp.group.id, ""))
        off = pose.bones.get(result.cam_off_names.get(bp.group.id, ""))
        cam_gid = plan.joint_group.get(c.driver_joint)
        cam_bone = result.bone_names.get(cam_gid)
        cam_bp = plan.bone_by_group.get(cam_gid)
        if rel is None or off is None or cam_bone is None or cam_bp is None:
            warnings.append(
                "joint {}: cam contact skipped, no relative or offset bone for the "
                "flat follower".format(j.id))
            continue
        for hb in (rel, off):
            hb["RIG_helper"] = j.id
            hb.lock_location = [True, True, True]
            hb.lock_rotation = [True, True, True]
            hb.lock_scale = [True, True, True]
        con = rel.constraints.new("COPY_TRANSFORMS")
        con.name = _PREFIX + "Cam Pose"
        con.target = arm_obj
        con.subtarget = cam_bone
        con.target_space = "POSE"
        con.owner_space = "POSE"
        con.mix_mode = "REPLACE"
        # REL rests where the cam's bone rests and hangs off the follower's
        # base, so its local channels are the cam's pose relative to that
        # base, in the cam's own rest axes: the table's input and the
        # translation term's inputs.
        cam_rest = rig_build._bone_rest_matrix(manifest, cam_bp, 1.0)
        cam_axis = (cam_rest.col[1].xyz).normalized()
        cam_origin = cam_rest.translation
        samples, why = support_samples(c, j, cam_axis, cam_origin)
        if samples is None:
            warnings.append("joint {}: cam contact skipped, {}".format(j.id, why))
            continue
        drivers_mod.table_driver(arm_obj, own, rel.name, True, False, samples, True,
                                 2.0 * math.pi, unit_scale)
        coef = offset_coefficients(c, j, cam_rest.to_3x3())
        if coef is not None:
            _add_location_driver(arm_obj, off, rel.name, coef)
    return warnings
