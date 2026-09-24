# SPDX-License-Identifier: GPL-3.0-or-later
"""Bone widgets, generated from the joint they stand for.

These are made in code rather than loaded from a library, because the useful
ones are MEASURED. A limit arc for +/-30 degrees is a different mesh from one
for +/-90. A stroke rail has to be the stroke's real length. A swing cone has
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

A control shows its freedoms and nothing else (Oscar, 2026-09-24): a curved
double arrow for each turn, a straight double arrow for each slide, and no
mark for a locked direction. Each mark is a flat strip with a little
thickness and flat arrowheads, the same marks the joint models of CADder
Rigging draw, so the control you grab looks like the joint it moves.

SOLID or WIRE, by what the widget is for. A widget that stands for a part you
take hold of (a motion mark, a limit arc, a rail, a point marker) is a real
surface. It reads at a glance, from any angle, without having to hunt for a
line against the model behind it. The two that stay WIRE are the ones
something has to be visible INSIDE: a swing cone with the stud it limits
standing in it, and the ground cross, which is the world rather than a part.

The geometry functions are pure: they take numbers and return
(verts, edges, faces), so they can be checked without Blender. `edges` holds
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
    the moment the view crosses its plane, and a dial you can only read from
    one side of the machine is half a dial. The back is a separate COPY of
    the vertices rather than the same ones wound the other way: two faces on
    one set of vertices are a duplicate face, and Blender's own mesh
    validation deletes those.
    """
    n = len(verts)
    return (list(verts) + list(verts),
            list(faces) + [tuple(n + i for i in reversed(f)) for f in faces])


def limit_arc(delta_min, delta_max, radius=1.0, segments=64, width=0.15):
    """A rotation limit: a solid band over exactly the arc the joint may turn
    through, drawn around the dial it belongs to so the dial's pointer runs
    along it. The band and nothing else: its own two ends already say where
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
    would be obvious, and there is none."""
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
    slide's center gets to, so at either end of the stroke the slide hangs
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


def _merge(*parts):
    """Several solids into one mesh, indices rebased."""
    verts, faces = [], []
    for pv, pf in parts:
        base = len(verts)
        verts.extend(pv)
        faces.extend(tuple(base + i for i in f) for f in pf)
    return verts, faces


def swing_cone(half_angle, length=1.0, segments=24, meridians=8):
    """A ball's swing limit: the cone the stud may lean anywhere inside. The
    mate gives an unsigned swing angle, so the cone is what it means, not a
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


def diamond(size=1.0):
    """A point held on a curve or a face: the geometry owns where it goes, so
    the widget marks the point and claims no axis. A solid octahedron,
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


# ── motion marks ────────────────────────────────────────────────────────────
#
# One mark for each freedom, and no mark for a locked direction (Oscar,
# 2026-09-24): a curved double arrow turns about +Y, and a straight double
# arrow slides along its line. Each mark is a strip with a little thickness
# and flat arrowheads. A curved one is bent the way sheet bends, across its
# thin side. Each one is a closed solid, so it reads from both sides with no
# back copy, and every face points out.

def _v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _v_mul(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def _v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _v_unit(a):
    m = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    return (a[0] / m, a[1] / m, a[2] / m) if m > 1e-12 else (0.0, 1.0, 0.0)


def _volume(verts, faces):
    """Signed volume by the divergence theorem: positive when the faces
    point out."""
    total = 0.0
    for f in faces:
        a = verts[f[0]]
        for i in range(1, len(f) - 1):
            b, c = verts[f[i]], verts[f[i + 1]]
            total += (a[0] * (b[1] * c[2] - b[2] * c[1])
                      - a[1] * (b[0] * c[2] - b[2] * c[0])
                      + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return total / 6.0


def _outward(verts, faces):
    """A closed solid with every face turned to point out."""
    if _volume(verts, faces) < 0.0:
        faces = [tuple(reversed(f)) for f in faces]
    return verts, faces


def _prism(outline, normal, thickness):
    """A flat solid: a closed outline in the plane of `normal`, extruded
    `thickness` along it and centered on the plane."""
    n = _v_unit(normal)
    h = thickness * 0.5
    k = len(outline)
    verts = ([_v_sub(p, _v_mul(n, h)) for p in outline]
             + [_v_add(p, _v_mul(n, h)) for p in outline])
    faces = [tuple(range(k - 1, -1, -1)), tuple(range(k, 2 * k))]
    faces += [(i, (i + 1) % k, k + (i + 1) % k, k + i) for i in range(k)]
    return _outward(verts, faces)


def _arrow_outline(a, b, across, half_width, head_half, head_length):
    """A straight double arrow from tip a to tip b, flat, `across` the unit
    vector across it in its plane."""
    d = _v_unit(_v_sub(b, a))
    s = _v_unit(across)

    def at(base, along, side):
        return _v_add(_v_add(base, _v_mul(d, along)), _v_mul(s, side))

    return [b,
            at(b, -head_length, -head_half), at(b, -head_length, -half_width),
            at(a, head_length, -half_width), at(a, head_length, -head_half),
            a,
            at(a, head_length, head_half), at(a, head_length, half_width),
            at(b, -head_length, half_width), at(b, -head_length, head_half)]


def _flat_arrow(a, b, normal, half_width, head_half, head_length, thickness):
    """A straight double arrow lying flat in the plane of `normal`."""
    across = _v_cross(normal, _v_sub(b, a))
    return _prism(_arrow_outline(a, b, across, half_width, head_half,
                                 head_length), normal, thickness)


def _ribbon(frames, halves, thickness, closed=False):
    """A strip swept along a path, as a strip of sheet bent to follow it.

    `frames` are (point, out, side): `out` runs across the thickness and
    `side` across the width, both unit vectors. `halves` is the half width
    at each point. A half width of 0 ends the strip in an edge: the tip of
    an arrowhead. Closed, the path runs round with no ends.
    """
    h = thickness * 0.5
    verts, rings = [], []
    for (p, out, side), w in zip(frames, halves):
        o = _v_mul(out, h)
        base = len(verts)
        if w <= 0.0:
            verts += [_v_sub(p, o), _v_add(p, o)]
            rings.append([base, base, base + 1, base + 1])
        else:
            s = _v_mul(side, w)
            verts += [_v_sub(_v_sub(p, s), o), _v_sub(_v_add(p, s), o),
                      _v_add(_v_add(p, s), o), _v_add(_v_sub(p, s), o)]
            rings.append([base, base + 1, base + 2, base + 3])
    pairs = list(zip(rings, rings[1:]))
    if closed:
        pairs.append((rings[-1], rings[0]))
    faces = []
    for a, b in pairs:
        for j in range(4):
            k = (j + 1) % 4
            face = []
            for v in (a[j], a[k], b[k], b[j]):
                if v not in face:          # a tip repeats its vertices
                    face.append(v)
            if len(face) >= 3:
                faces.append(tuple(face))
    if not closed:
        if len(set(rings[0])) == 4:
            faces.append(tuple(reversed(rings[0])))
        if len(set(rings[-1])) == 4:
            faces.append(tuple(rings[-1]))
    return _outward(verts, faces)


def _arc_frames(radius, ts):
    """Frames round +Y at the angles `ts`: the thickness runs out from the
    axis and the width along it, so a strip on them is a band round the
    axis."""
    return [(_ring_point(t, radius), (math.sin(t), 0.0, math.cos(t)),
             (0.0, 1.0, 0.0)) for t in ts]


def _steps(t0, t1, step):
    n = max(2, int(math.ceil(abs(t1 - t0) / step)))
    return [t0 + (t1 - t0) * i / n for i in range(n + 1)]


def turn_arrow(radius=0.45, width=0.15, pointer=0.35, heads=0.33,
               head_length=0.45, gap=0.7, gap_at=math.pi, thickness=0.05,
               segments=48):
    """A turn about +Y: a curved double arrow round the axis, open at angle
    `gap_at`, with its heads pointing into the gap, so it reads as "turns
    both ways".

    The arrow is a band round the axis, a strip bent the way sheet bends:
    its width runs along +Y and its thickness out from the axis (Oscar,
    2026-09-24). Each head narrows to an edge along the circle, so it
    follows the band and does not kink off it.

    `width` and `heads` are half widths along +Y, as fractions of `radius`.
    `head_length` is the angle a head takes, in radians. With `pointer`, a
    flat point stands out from the band at t = 0, where the joint rests,
    and reaches radius * (1 + pointer): nothing else reaches that far.
    """
    r = radius
    step = 2.0 * math.pi / max(8, int(segments))
    t0 = gap_at + gap * 0.5
    t1 = gap_at + 2.0 * math.pi - gap * 0.5
    s0, s1 = t0 + head_length, t1 - head_length
    # The band runs a little into each head, so no face of it lies on the
    # back of a head.
    reach = step * 0.25
    ts = _steps(s0 - reach, s1 + reach, step)
    parts = [_ribbon(_arc_frames(r, ts), [r * width] * len(ts), thickness)]
    for a, b in ((s1, t1), (s0, t0)):
        hts = _steps(a, b, step * 0.5)
        n = len(hts) - 1
        parts.append(_ribbon(_arc_frames(r, hts),
                             [r * heads * (1.0 - i / n) for i in range(n + 1)],
                             thickness))
    if pointer:
        rest = 2.0 * math.pi * round((s0 + s1) / (4.0 * math.pi))
        parts.append(_prism([_ring_point(rest - step, r),
                             _ring_point(rest, r * (1.0 + pointer)),
                             _ring_point(rest + step, r)],
                            (0.0, 1.0, 0.0), thickness))
    verts, faces = _merge(*parts)
    return verts, [], faces


def slide_arrow(length=1.0, half_width=0.07, heads=0.2, head_length=0.22,
                thickness=0.05, normal=(1.0, 0.0, 0.0)):
    """A slide along +Y: one straight double arrow, flat in the plane of
    `normal`, exactly `length` from tip to tip and centered on the
    origin."""
    a, b = (0.0, -length * 0.5, 0.0), (0.0, length * 0.5, 0.0)
    verts, faces = _flat_arrow(a, b, normal, half_width, heads, head_length,
                               thickness)
    return verts, [], faces


def turn_and_slide(radius=0.45, width=0.15, pointer=0.35, length=1.0,
                   half_width=0.07, thickness=0.05):
    """A cylindrical joint: it turns about +Y and slides along it."""
    tv, _te, tf = turn_arrow(radius, width, pointer, thickness=thickness)
    sv, _se, sf = slide_arrow(length, half_width, thickness=thickness)
    verts, faces = _merge((tv, tf), (sv, sf))
    return verts, [], faces


def screw_arrow(length=1.0, radius=0.22, turns=2.0, width=0.1,
                thickness=0.04, heads=0.14, head_length=0.18, segments=96):
    """A screw: a turn and a slide that are one motion, so one strip wound
    round +Y, flat on the cylinder it lies on. Each head narrows to an edge
    along the helix itself, so it follows the thread and does not kink off
    it. Exactly `length` long, as every slide widget is: the rail behind it
    is padded by half of that."""
    half = length * 0.5

    def point(f):
        x, _y, z = _ring_point(2.0 * math.pi * turns * f, radius)
        return (x, -half + length * f, z)

    def frame(f):
        p = point(f)
        out = _v_unit((p[0], 0.0, p[2]))
        df = 1e-4
        tan = _v_unit(_v_sub(point(min(1.0, f + df)), point(max(0.0, f - df))))
        return p, out, _v_unit(_v_cross(out, tan))

    total = math.sqrt((2.0 * math.pi * turns * radius) ** 2 + length ** 2)
    f0 = head_length / total
    f1 = 1.0 - f0
    step = 1.0 / max(8, int(segments))
    reach = step * 0.25
    fs = _steps(f0 - reach, f1 + reach, step)
    parts = [_ribbon([frame(f) for f in fs], [width * 0.5] * len(fs),
                     thickness)]
    for a, b in ((f1, 1.0), (f0, 0.0)):
        hs = _steps(a, b, step * 0.5)
        n = len(hs) - 1
        parts.append(_ribbon([frame(f) for f in hs],
                             [heads * (1.0 - i / n) for i in range(n + 1)],
                             thickness))
    verts, faces = _merge(*parts)
    # A head leans past the ends of the path, and a slide widget has to be
    # exactly `length`: squeezed back, which costs a few percent along the
    # axis and nothing across it.
    lo = min(v[1] for v in verts)
    hi = max(v[1] for v in verts)
    if hi - lo > 1e-12:
        k = length / (hi - lo)
        mid = (hi + lo) * 0.5
        verts = [(x, (y - mid) * k, z) for x, y, z in verts]
    return verts, [], faces


def _loop_band(radius, half_width, thickness, axis, segments=48):
    """A closed band round one of the axes (0, 1 or 2): a turn with no
    ends, bent as the turn arrow is."""
    n = max(8, int(segments))
    frames = []
    for i in range(n):
        t = 2.0 * math.pi * i / n
        c, s = math.cos(t), math.sin(t)
        p, out, side = [0.0] * 3, [0.0] * 3, [0.0] * 3
        p[(axis + 1) % 3], p[(axis + 2) % 3] = c * radius, s * radius
        out[(axis + 1) % 3], out[(axis + 2) % 3] = c, s
        side[axis] = 1.0
        frames.append((tuple(p), tuple(out), tuple(side)))
    return _ribbon(frames, [half_width] * n, thickness, closed=True)


def ball_rings(radius=0.35, width=0.1, thickness=0.03, stub=1.0,
               stub_width=0.05):
    """A ball joint: it turns every way, so three bands at right angles, as
    the rotate gizmo has. A stud stands out along +Y, the child's own
    direction, so the turn shows and a swing cone has something to hold."""
    parts = [_loop_band(radius, radius * width, thickness, axis)
             for axis in (0, 1, 2)]
    a, b = (0.0, radius + thickness, 0.0), (0.0, max(stub, radius * 1.5), 0.0)
    across = (0.0, 0.0, 1.0)
    parts.append(_prism([_v_add(a, _v_mul(across, -stub_width)),
                         _v_add(b, _v_mul(across, -stub_width)),
                         _v_add(b, _v_mul(across, stub_width)),
                         _v_add(a, _v_mul(across, stub_width))],
                        (1.0, 0.0, 0.0), thickness))
    verts, faces = _merge(*parts)
    return verts, [], faces


def plane_arrows(reach=0.56, radius=0.2, width=0.16, pointer=0.35,
                 half_width=0.035, heads=0.11, head_length=0.13,
                 thickness=0.05):
    """A planar joint: it slides anywhere in the plane +Y is normal to and
    turns about +Y. Two crossed arrows lie flat in the plane, on the
    diagonals so the pointer of the turn at t = 0 stays clear of them.

    The two arrows differ a little in thickness: where two flat strips
    cross, faces in one plane would fight over the same pixels.
    """
    tv, _te, tf = turn_arrow(radius, width, pointer, thickness=thickness)
    parts = [(tv, tf)]
    for k, (dx, dz) in enumerate(((1.0, 1.0), (1.0, -1.0))):
        d = _v_unit((dx, 0.0, dz))
        parts.append(_flat_arrow(_v_mul(d, -reach), _v_mul(d, reach),
                                 (0.0, 1.0, 0.0), half_width, heads,
                                 head_length, thickness * (0.8 - 0.1 * k)))
    verts, faces = _merge(*parts)
    return verts, [], faces


def pin_slot_arrows(radius=0.32, width=0.2, length=1.2, half_width=0.05,
                    heads=0.15, head_length=0.16, thickness=0.05):
    """A pin in a slot: it turns about the pin, +Y, and slides along the
    slot, +Z. The slide arrow turns with the pin, so it shows the angle
    itself, and the turn needs no pointer. The turn is open at +X, and the
    slide lies flat across it, its heads clear of the turn."""
    tv, _te, tf = turn_arrow(radius, width, pointer=0.0, gap_at=math.pi / 2.0,
                             thickness=thickness)
    arrow = _flat_arrow((0.0, 0.0, -length * 0.5), (0.0, 0.0, length * 0.5),
                        (0.0, 1.0, 0.0), half_width, heads, head_length,
                        thickness * 0.8)
    verts, faces = _merge((tv, tf), arrow)
    return verts, [], faces


# ── the bpy layer ───────────────────────────────────────────────────────────

WIDGET_COLLECTION = "SW_widgets"


def widget_collection(parent):
    """A collection for the widget objects, excluded from the view layer.

    They must exist as objects, but they are not scene content: a custom
    shape is drawn by the bone, not by its own object. Excluding keeps them
    out of the viewport, the render and the user's way, while leaving them
    inspectable when something looks wrong.

    It goes in `parent`, the collection of the rig. A rig that is removed
    leaves it at the scene root, because other rigs can use its widgets.
    The next build takes it from there. It stayed there as well, so after
    a second send it was in the scene twice, once at the root, and only
    one of the two was excluded (2026-09-23).
    """
    col = bpy.data.collections.get(WIDGET_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(WIDGET_COLLECTION)
        col["CADLINK_widgets"] = True
    if col.name not in [c.name for c in parent.children]:
        parent.children.link(col)
    root = bpy.context.scene.collection
    if parent != root and col.name in [c.name for c in root.children]:
        root.children.unlink(col)
    return col


def exclude_widgets(view_layer):
    """Excludes the widget collection everywhere it is in the view layer.
    Each rig collection holds it, and each place is a layer of its own:
    with two rigs side by side, the widgets showed in the second (two
    configurations of one assembly, 2026-09-24)."""
    def walk(layer_col):
        if layer_col.collection.name == WIDGET_COLLECTION:
            layer_col.exclude = True
            return
        for child in layer_col.children:
            walk(child)

    walk(view_layer.layer_collection)


# Edges that turn more than this are sharp: a band reads round and its
# edges and the flats of an arrow stay crisp (Oscar, 2026-09-24). A mesh
# made from data is shaded smooth all over, and that rounded every edge of
# a flat strip.
SHARP_ANGLE = math.radians(40.0)


def signature(geometry):
    """A short fingerprint of a shape, so a widget whose shape changed
    under the same name is made again."""
    verts, edges, faces = geometry
    total = sum(x + 2.0 * y + 3.0 * z for x, y, z in verts)
    return "%d/%d/%d/%.6f" % (len(verts), len(edges), len(faces), total)


def _mesh(name, geometry):
    verts, edges, faces = geometry
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(v) for v in verts],
                     [tuple(e) for e in edges],
                     [tuple(f) for f in faces])
    mesh.update()
    if faces:
        mesh.shade_smooth()
        mesh.set_sharp_from_angle(angle=SHARP_ANGLE)
    return mesh


def widget(collection, name, geometry, cache):
    """One widget object, made once per distinct shape.

    A machine has many joints of the same kind, and every plain revolute
    wants the same ring. The cache key is the caller's name, which carries
    whatever measurements made the shape different.
    """
    existing = cache.get(name)
    if existing is not None:
        return existing
    # A rebuild reuses the widget the last build left in the collection:
    # the name carries every measurement that shaped it, so an object of
    # that name IS the shape. Without this every rebuild added a fresh
    # SWW_dial.001, .002, ... to the scene (live plunger.sldasm, 2026-09-15,
    # switching the input in the panel). The shape of a kind can change
    # from one version to the next, so the object also keeps a fingerprint
    # of its shape, and a different one gets the new mesh.
    sig = signature(geometry)
    existing = collection.objects.get(name)
    if existing is not None and existing.get("CADLINK_widget"):
        if existing.get("CADLINK_widget_shape") != sig:
            old = existing.data
            existing.data = _mesh(name, geometry)
            existing["CADLINK_widget_shape"] = sig
            if old is not None and old.users == 0:
                bpy.data.meshes.remove(old)
        cache[name] = existing
        return existing

    obj = bpy.data.objects.new(name, _mesh(name, geometry))
    obj["CADLINK_widget"] = True
    obj["CADLINK_widget_shape"] = sig
    collection.objects.link(obj)
    cache[name] = obj
    return obj


def prune(collection):
    """Removes the widgets that no bone wears any more, as the old dials
    after a rebuild with the motion marks (2026-09-24). Only the objects
    this module made, and only when no armature in the file wears them:
    every rig of the file shares the one widget collection."""
    used = {pb.custom_shape.name for ob in bpy.data.objects
            if ob.type == "ARMATURE" and ob.pose is not None
            for pb in ob.pose.bones if pb.custom_shape is not None}
    removed = 0
    for obj in list(collection.objects):
        if obj.get("CADLINK_widget") and obj.name not in used:
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                bpy.data.meshes.remove(data)
            removed += 1
    return removed
