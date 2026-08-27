# SPDX-License-Identifier: GPL-3.0-or-later
"""Bone widgets, generated from the joint they stand for.

These are made in code rather than loaded from a library, because the useful
ones are MEASURED. A limit arc for +/-30 degrees is a different mesh from one
for +/-90; a stroke rail has to be the stroke's real length; a swing cone has
to be the mate's real half angle. A library of static shapes can only give a
generic ring, and a generic ring tells the user nothing they did not already
know. Everything needed to size them is already in the manifest, so the rig
draws its own dial faces.

Frame convention, the same one rig_build._frame_matrix builds every bone
with: local +Y is the joint's DOF axis, and the plane it turns in is spanned
by local X and Z. A positive rotation about +Y carries +Z toward +X, so a
point at angle t is (sin t, 0, cos t) and t = 0 points along +Z. That is why
a limit arc drawn over [delta_min, delta_max] lines up exactly with the
pointer on the dial: the pointer sits at t = 0 when the joint is at rest, and
Blender's own rotation channel is measured the same way.

SOLID or WIRE, by what the widget is for. A widget that stands for a part you
take hold of — a dial, its limit arc, a slide bar, its rail, a ball and its
stud, a screw's wire, a point marker — is a real surface: it reads at a
glance, from any angle, without having to hunt for a line against the model
behind it. The two that stay WIRE are the ones something has to be visible
INSIDE: a swing cone with the stud it limits standing in it, and the ground
cross, which is the world rather than a part.

The geometry functions are pure — they take numbers and return
(verts, edges, faces) — so they can be checked without Blender. `edges` holds
LOOSE edges only, never an edge that a face already owns, because from_pydata
would then build it twice. Only `widget` and `widget_collection` touch bpy.
"""

import math

try:
    import bpy
except ImportError:
    bpy = None


# ── pure geometry ───────────────────────────────────────────────────────────

def _ring_point(t, radius):
    """A point at angle `t` about +Y, in the plane +Y is normal to."""
    return (math.sin(t) * radius, 0.0, math.cos(t) * radius)


def _chain(verts, closed=False):
    """Edges joining a run of vertex indices."""
    edges = [(i, i + 1) for i in range(len(verts) - 1)]
    if closed and len(verts) > 2:
        edges.append((len(verts) - 1, 0))
    return edges


def _band(t0, t1, radius, width, segments, closed=False):
    """A solid annular band from angle t0 to t1, flat in the plane +Y is
    normal to. `width` is the half thickness as a fraction of `radius`."""
    inner = max(0.0, radius * (1.0 - width))
    outer = radius * (1.0 + width)
    n = max(3, int(segments))
    verts, faces = [], []
    steps = n if closed else n - 1
    for i in range(n):
        t = t0 + (t1 - t0) * i / (n if closed else n - 1)
        verts += [_ring_point(t, inner), _ring_point(t, outer)]
    for i in range(steps):
        a, b = 2 * i, 2 * ((i + 1) % n)
        faces.append((a, a + 1, b + 1, b))
    return verts, faces


def _both_sides(verts, faces):
    """A flat widget, drawn from either side of its plane.

    Bone custom shapes are backface culled, so a one-sided dial disappears
    the moment the view crosses its plane — and a dial you can only read from
    one side of the machine is half a dial. The back is a separate COPY of
    the vertices rather than the same ones wound the other way: two faces on
    one set of vertices are a duplicate face, and Blender's own mesh
    validation deletes those.
    """
    n = len(verts)
    return (list(verts) + list(verts),
            list(faces) + [tuple(n + i for i in reversed(f)) for f in faces])


def ring_with_pointer(radius=1.0, segments=48, pointer=0.35, width=0.15):
    """A revolute: a solid ring in the plane of rotation, with the rim drawn
    out to a point at rest so which way it turned is visible at a glance. A
    bare ring looks identical at every angle.

    The point is ONE VERTEX OF THE RIM pulled outward, not a triangle laid
    on top: the two quads either side of it stretch into the spike, so it is
    the ring rather than something stuck to it, and there is no seam where
    the two meet.

    That only works because a vertex lands exactly on t = 0. `_band` steps
    from t0, so index 0 is at t0 = 0, which is rest — the angle Blender's
    own rotation channel reads zero at. Off by half a segment and the dial
    would point a few degrees away from where the joint actually sits.

    The ring is centred on the widget's own origin, which is where the
    bone's head is drawn, so it turns about its centre rather than around
    it. `pointer` is how far past the rim the spike reaches, as a fraction
    of the radius.
    """
    verts, faces = _band(0.0, 2.0 * math.pi, radius, width, segments,
                         closed=True)
    # _band lays each step down as (inner, outer): index 1 is the outer rim
    # at t = 0.
    verts[1] = _ring_point(0.0, radius * (1.0 + pointer))
    verts, faces = _both_sides(verts, faces)
    return verts, [], faces


def limit_arc(delta_min, delta_max, radius=1.0, segments=64, width=0.15):
    """A rotation limit: a solid band over exactly the arc the joint may turn
    through, drawn around the dial it belongs to so the dial's pointer runs
    along it. The band and nothing else — its own two ends already say where
    the travel starts and stops."""
    span = delta_max - delta_min
    n = max(3, min(int(segments), int(abs(span) / (math.pi / 48.0)) + 3))
    verts, faces = _both_sides(*_band(delta_min, delta_max, radius, width, n))
    return verts, [], faces


def cylinder(length=1.0, radius=0.5, segments=16):
    """A slide that also turns: a round section says the body may spin about
    the same axis it slides along."""
    half = length * 0.5
    n = max(3, int(segments))
    verts = []
    for sign in (-1.0, 1.0):
        for i in range(n):
            x, _y, z = _ring_point(2.0 * math.pi * i / n, radius)
            verts.append((x, sign * half, z))
    faces = [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
    faces.append(tuple(range(n - 1, -1, -1)))          # the two end caps
    faces.append(tuple(range(n, 2 * n)))
    return verts, [], faces


def cuboid(length=1.0, half_width=0.4):
    """A slide that may NOT turn: a square section has a corner, so any spin
    would be obvious — and there is none."""
    half = length * 0.5
    verts = []
    for sign in (-1.0, 1.0):
        verts += [(-half_width, sign * half, -half_width),
                  (half_width, sign * half, -half_width),
                  (half_width, sign * half, half_width),
                  (-half_width, sign * half, half_width)]
    # Wound so every normal points OUT of the box. Inward normals make a
    # backface-culled widget look inside out: you see the far wall through
    # the near one.
    faces = [(0, 1, 2, 3), (4, 7, 6, 5),
             (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    return verts, [], faces


def stroke_bar(delta_min, delta_max, half_width=0.25, pad=0.0,
               round_section=False, segments=16):
    """A translation limit: the same bar as the slide it stands behind, only
    thinner, running the travel's REAL length along the slide axis.

    `pad` extends it past each limit by half the slide widget's own length.
    Without that the rail stops at the limit VALUE, which is where the
    slide's centre gets to — so at either end of the stroke the slide hangs
    half its length off the rail. Padded, the ends line up.
    """
    lo = min(delta_min, delta_max) - pad
    hi = max(delta_min, delta_max) + pad
    mid = (lo + hi) * 0.5
    if round_section:
        verts, edges, faces = cylinder(hi - lo, half_width, segments)
    else:
        verts, edges, faces = cuboid(hi - lo, half_width)
    return [(x, y + mid, z) for x, y, z in verts], edges, faces


def helix(length=1.0, radius=0.5, turns=2.0, segments=48, thread=0.0):
    """A screw: a slide and a turn that are ONE motion, which is exactly what
    a helix draws. `thread` is the section radius of the wire — 0 leaves it
    a bare line."""
    half = length * 0.5
    path = []
    for i in range(segments + 1):
        f = i / segments
        x, _y, z = _ring_point(2.0 * math.pi * turns * f, radius)
        path.append((x, -half + length * f, z))
    if thread <= 0.0:
        return path, _chain(path), []
    verts, faces = _tube(path, thread, sides=8)
    # The wire's own section bulges past the ends of the path it was swept
    # along, and a slide widget has to be EXACTLY one bone length: the rail
    # behind it is padded by half of that, so a widget that overhangs would
    # leave the rail short at both stops. Squeezed back to the nominal
    # length, which costs a fraction of a percent of the section.
    lo = min(v[1] for v in verts)
    hi = max(v[1] for v in verts)
    if hi - lo > 1e-12:
        k = length / (hi - lo)
        mid = (hi + lo) * 0.5
        verts = [(x, (y - mid) * k, z) for x, y, z in verts]
    return verts, [], faces


def _sphere(radius, rings=8, segments=16, centre=(0.0, 0.0, 0.0)):
    """A solid ball about +Y, wound outward."""
    n = max(3, int(segments))
    r_count = max(2, int(rings))
    cx, cy, cz = centre
    verts, faces = [], []
    top = len(verts)
    verts.append((cx, cy + radius, cz))
    for i in range(1, r_count):
        lat = math.pi * i / r_count
        y, rr = math.cos(lat) * radius, math.sin(lat) * radius
        for j in range(n):
            x, _y, z = _ring_point(2.0 * math.pi * j / n, rr)
            verts.append((cx + x, cy + y, cz + z))
    bottom = len(verts)
    verts.append((cx, cy - radius, cz))

    def ring(i, j):
        return 1 + (i - 1) * n + (j % n)

    for j in range(n):
        faces.append((top, ring(1, j), ring(1, j + 1)))
    for i in range(1, r_count - 1):
        for j in range(n):
            faces.append((ring(i, j), ring(i + 1, j),
                          ring(i + 1, j + 1), ring(i, j + 1)))
    for j in range(n):
        faces.append((bottom, ring(r_count - 1, j + 1), ring(r_count - 1, j)))
    return verts, faces


def _tube(path, radius, sides=8, cap=True):
    """A solid tube swept along a run of points, wound outward.

    The section frame is carried along by parallel transport — rotate the
    previous frame by the smallest rotation taking the old tangent to the
    new one — so the tube does not twist or flip where the path turns.
    """
    n = max(3, int(sides))
    pts = [tuple(p) for p in path]
    if len(pts) < 2:
        return [], []

    def sub(a, b):
        return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    def cross(a, b):
        return (a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0])

    def dot(a, b):
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    def unit(v):
        m = math.sqrt(dot(v, v))
        return (v[0] / m, v[1] / m, v[2] / m) if m > 1e-12 else (0.0, 1.0, 0.0)

    tangents = []
    for i, p in enumerate(pts):
        if i == 0:
            tangents.append(unit(sub(pts[1], p)))
        elif i == len(pts) - 1:
            tangents.append(unit(sub(p, pts[-2])))
        else:
            tangents.append(unit(sub(pts[i + 1], pts[i - 1])))

    seed = (1.0, 0.0, 0.0)
    if abs(dot(seed, tangents[0])) > 0.9:
        seed = (0.0, 0.0, 1.0)
    normal = unit(sub(seed, tuple(c * dot(seed, tangents[0]) for c in tangents[0])))

    verts, faces = [], []
    for i, p in enumerate(pts):
        if i > 0:
            axis = cross(tangents[i - 1], tangents[i])
            s = math.sqrt(dot(axis, axis))
            if s > 1e-12:
                axis = unit(axis)
                angle = math.atan2(s, dot(tangents[i - 1], tangents[i]))
                ca, sa = math.cos(angle), math.sin(angle)
                cr = cross(axis, normal)
                d = dot(axis, normal) * (1.0 - ca)
                normal = unit((normal[0] * ca + cr[0] * sa + axis[0] * d,
                               normal[1] * ca + cr[1] * sa + axis[1] * d,
                               normal[2] * ca + cr[2] * sa + axis[2] * d))
        binormal = unit(cross(tangents[i], normal))
        for j in range(n):
            a = 2.0 * math.pi * j / n
            ca, sa = math.cos(a), math.sin(a)
            verts.append((p[0] + (normal[0] * ca + binormal[0] * sa) * radius,
                          p[1] + (normal[1] * ca + binormal[1] * sa) * radius,
                          p[2] + (normal[2] * ca + binormal[2] * sa) * radius))
    for i in range(len(pts) - 1):
        a, b = i * n, (i + 1) * n
        for j in range(n):
            k = (j + 1) % n
            faces.append((a + j, a + k, b + k, b + j))
    if cap:
        # The side quads run j -> j+1 on the first ring and j+1 -> j on the
        # last, so the caps close them the other way round.
        faces.append(tuple(range(n - 1, -1, -1)))
        faces.append(tuple(range(len(verts) - n, len(verts))))
    return verts, faces


def _merge(*parts):
    """Several solids into one mesh, indices rebased."""
    verts, faces = [], []
    for pv, pf in parts:
        base = len(verts)
        verts.extend(pv)
        faces.extend(tuple(base + i for i in f) for f in pf)
    return verts, faces


def ball_with_stub(radius=0.35, stub=1.0, rings=8, segments=16):
    """A ball joint: the ball itself, with the stud standing out of the
    socket along the child's own direction. `stub` is where the stud ends,
    in the same units as the radius."""
    verts, faces = _merge(
        _sphere(radius, rings, segments),
        _tube([(0.0, 0.0, 0.0), (0.0, max(stub, radius * 1.5), 0.0)],
              radius * 0.42, segments))
    return verts, [], faces


def swing_cone(half_angle, length=1.0, segments=24, meridians=8):
    """A ball's swing limit: the cone the stud may lean anywhere inside. The
    mate gives an unsigned swing angle, so the cone is what it means — not a
    box on two axes. Wire, because the stud it limits lives inside it."""
    a = max(1e-4, min(abs(half_angle), math.pi * 0.98))
    r = math.sin(a) * length
    h = math.cos(a) * length
    verts = [(math.sin(2.0 * math.pi * i / segments) * r, h,
              math.cos(2.0 * math.pi * i / segments) * r)
             for i in range(segments)]
    edges = [(i, (i + 1) % segments) for i in range(segments)]
    apex = len(verts)
    verts.append((0.0, 0.0, 0.0))
    step = max(1, segments // meridians)
    edges += [(apex, i) for i in range(0, segments, step)]
    return verts, edges, []


def disc_with_pointer(radius=1.0, thickness=0.25, segments=48, pointer=0.35,
                      width=0.15):
    """A planar contact: the revolute's dial given thickness.

    A plane joint slides in its plane AND spins about the plane's normal, so
    the dial is exactly the right marker — extruded, it also reads as the
    disc lying on the face, which is what the contact is. Local +Y is the
    plane normal, so the disc lies flat in the plane by construction.
    """
    n = max(3, int(segments))
    inner = radius * (1.0 - width)
    outer = radius * (1.0 + width)
    half = thickness * 0.5
    verts, faces = [], []
    for j in range(n):
        t = 2.0 * math.pi * j / n
        out = radius * (1.0 + pointer) if j == 0 else outer
        for y in (-half, half):
            x, _y, z = _ring_point(t, inner)
            verts.append((x, y, z))
            x, _y, z = _ring_point(t, out)
            verts.append((x, y, z))

    def v(j, top, rim):
        return ((j % n) * 4) + (2 if top else 0) + (1 if rim else 0)

    for j in range(n):
        k = j + 1
        # top and bottom rings
        faces.append((v(j, True, False), v(j, True, True),
                      v(k, True, True), v(k, True, False)))
        faces.append((v(j, False, False), v(k, False, False),
                      v(k, False, True), v(j, False, True)))
        # outer and inner walls
        faces.append((v(j, False, True), v(k, False, True),
                      v(k, True, True), v(j, True, True)))
        faces.append((v(j, False, False), v(j, True, False),
                      v(k, True, False), v(k, False, False)))
    return verts, [], faces


def slot(length=1.0, radius=0.35, segments=16):
    """A pin in a slot: the solid ring is the pin's turn, the rails beside it
    are its travel."""
    verts, edges, faces = ring_with_pointer(radius=radius, segments=segments)
    base = len(verts)
    half = length * 0.5
    verts = list(verts) + [(radius * 0.6, 0.0, -half), (radius * 0.6, 0.0, half),
                           (-radius * 0.6, 0.0, -half), (-radius * 0.6, 0.0, half)]
    edges = list(edges) + [(base, base + 1), (base + 2, base + 3)]
    return verts, edges, faces


def diamond(size=1.0):
    """A point held on a curve or a face: the geometry owns where it goes, so
    the widget marks the point and claims no axis. A solid octahedron —
    which is what a point marker should look like from every side."""
    s = size
    verts = [(0.0, s, 0.0), (s, 0.0, 0.0), (0.0, 0.0, s),
             (-s, 0.0, 0.0), (0.0, 0.0, -s), (0.0, -s, 0.0)]
    faces = [(0, 2, 1), (0, 3, 2), (0, 4, 3), (0, 1, 4),
             (5, 1, 2), (5, 2, 3), (5, 3, 4), (5, 4, 1)]
    return verts, [], faces


def ground_cross(size=1.0):
    """The assembly's own root: axes and the ground it stands on."""
    s = size
    verts = [(-s, 0.0, 0.0), (s, 0.0, 0.0), (0.0, 0.0, -s), (0.0, 0.0, s),
             (0.0, 0.0, 0.0), (0.0, s * 0.6, 0.0),
             (-s, 0.0, -s), (s, 0.0, -s), (s, 0.0, s), (-s, 0.0, s)]
    edges = [(0, 1), (2, 3), (4, 5),
             (6, 7), (7, 8), (8, 9), (9, 6)]
    return verts, edges, []


# ── the bpy layer ───────────────────────────────────────────────────────────

WIDGET_COLLECTION = "SW_widgets"


def widget_collection(scene_collection):
    """A collection for the widget objects, excluded from the view layer.

    They must exist as objects, but they are not scene content: a custom
    shape is drawn by the bone, not by its own object. Excluding keeps them
    out of the viewport, the render and the user's way, while leaving them
    inspectable when something looks wrong.
    """
    col = bpy.data.collections.get(WIDGET_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(WIDGET_COLLECTION)
        col["SWTB_widgets"] = True
    if col.name not in [c.name for c in scene_collection.children]:
        scene_collection.children.link(col)
    return col


def exclude_widgets(view_layer):
    def find(layer_col):
        if layer_col.collection.name == WIDGET_COLLECTION:
            return layer_col
        for child in layer_col.children:
            hit = find(child)
            if hit is not None:
                return hit
        return None

    lc = find(view_layer.layer_collection)
    if lc is not None:
        lc.exclude = True


def widget(collection, name, geometry, cache):
    """One widget object, made once per distinct shape.

    A machine has many joints of the same kind, and every plain revolute
    wants the same ring. The cache key is the caller's name, which carries
    whatever measurements made the shape different.
    """
    existing = cache.get(name)
    if existing is not None:
        return existing

    verts, edges, faces = geometry
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(v) for v in verts],
                     [tuple(e) for e in edges],
                     [tuple(f) for f in faces])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj["SWTB_widget"] = True
    collection.objects.link(obj)
    cache[name] = obj
    return obj
