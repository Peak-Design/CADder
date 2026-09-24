"""The bone widgets are MEASURED, so what is checked here is the
measurements: an arc that spans the limit, a bar as long as the stroke, a
cone at the swing angle. A widget that merely looks like a dial is worse than
none, because it says something definite and wrong.
"""
import math
import os
import sys
import unittest

# The addons directory (the parent of the CADder repo root) makes
# "import CADder.rig" work from any checkout named CADder.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import shapes


def bounds(verts, axis):
    return min(v[axis] for v in verts), max(v[axis] for v in verts)


def angle_of(v):
    """The angle about +Y that shapes._ring_point would have produced."""
    return math.atan2(v[0], v[2])


def at_radius(verts, radius, tol=1e-9):
    return [v for v in verts if abs(math.hypot(v[0], v[2]) - radius) < tol]


def normal_of(verts, face):
    """Newell's normal: the one Blender derives from the winding order."""
    n = [0.0, 0.0, 0.0]
    for i, ai in enumerate(face):
        a, b = verts[ai], verts[face[(i + 1) % len(face)]]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    return tuple(n)


def centroid(verts, face=None):
    picked = verts if face is None else [verts[i] for i in face]
    return tuple(sum(v[i] for v in picked) / len(picked) for i in range(3))


def edge_pairing(faces):
    """Every directed edge of a closed, consistently wound mesh appears
    exactly once, and its reverse exactly once. Reports the strays."""
    seen = {}
    for f in faces:
        for i, a in enumerate(f):
            b = f[(i + 1) % len(f)]
            seen[(a, b)] = seen.get((a, b), 0) + 1
    bad = [e for e, n in seen.items() if n != 1]
    lonely = [e for e in seen if (e[1], e[0]) not in seen]
    return bad, lonely


def enclosed_volume(verts, faces):
    """Signed volume by the divergence theorem: positive when the winding
    puts the normals OUTSIDE. Works for any closed shape, including an
    annulus whose inner wall faces toward its own axis."""
    total = 0.0
    for f in faces:
        a = verts[f[0]]
        for i in range(1, len(f) - 1):
            b, c = verts[f[i]], verts[f[i + 1]]
            total += (a[0] * (b[1] * c[2] - b[2] * c[1])
                      - a[1] * (b[0] * c[2] - b[2] * c[0])
                      + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return total / 6.0


MARKS = {
    "turn": shapes.turn_arrow(),
    "slide": shapes.slide_arrow(),
    "turn and slide": shapes.turn_and_slide(),
    "screw": shapes.screw_arrow(),
    "ball rings": shapes.ball_rings(),
    "plane": shapes.plane_arrows(),
    "pin in slot": shapes.pin_slot_arrows(),
}

ALL_WIDGETS = tuple(MARKS.values()) + (
    shapes.limit_arc(-0.5, 0.5), shapes.cylinder(), shapes.cuboid(),
    shapes.stroke_bar(-0.1, 0.2), shapes.stroke_bar(-0.1, 0.2,
                                                    round_section=True),
    shapes.swing_cone(0.4), shapes.diamond(), shapes.ground_cross())


class FrameConventionTest(unittest.TestCase):
    """Local +Y is the DOF axis and a positive turn about it carries +Z
    toward +X. Every widget is drawn in that frame, and a limit arc only
    lines up with Blender's own rotation channel because of it."""

    def test_rest_points_along_z(self):
        x, y, z = shapes._ring_point(0.0, 1.0)
        self.assertAlmostEqual(0.0, x)
        self.assertAlmostEqual(0.0, y)
        self.assertAlmostEqual(1.0, z)

    def test_a_positive_turn_moves_toward_x(self):
        x, _y, z = shapes._ring_point(math.pi / 2.0, 1.0)
        self.assertAlmostEqual(1.0, x)
        self.assertAlmostEqual(0.0, z)

    def test_every_widget_is_built_of_valid_geometry(self):
        for geom in ALL_WIDGETS:
            verts, edges, faces = geom
            self.assertTrue(verts)
            self.assertTrue(edges or faces)
            for a, b in edges:
                self.assertLess(a, len(verts))
                self.assertLess(b, len(verts))
                self.assertNotEqual(a, b)
            for face in faces:
                self.assertGreaterEqual(len(face), 3)
                self.assertEqual(len(face), len(set(face)),
                                 "a face repeats a vertex")
                for i in face:
                    self.assertLess(i, len(verts))

    def test_a_face_never_repeats_a_loose_edge(self):
        # from_pydata is given both lists, so an edge a face already owns
        # would be built twice.
        for geom in ALL_WIDGETS:
            _verts, edges, faces = geom
            owned = set()
            for face in faces:
                for i, a in enumerate(face):
                    b = face[(i + 1) % len(face)]
                    owned.add((min(a, b), max(a, b)))
            for a, b in edges:
                self.assertNotIn((min(a, b), max(a, b)), owned)

    def test_the_widgets_you_grab_are_solid_and_the_rest_are_wire(self):
        # A motion mark, an arc and a rail stand IN FOR a part you take
        # hold of, so they read as surfaces...
        for geom in tuple(MARKS.values()) + (
                shapes.limit_arc(-0.5, 0.5), shapes.stroke_bar(-0.1, 0.2),
                shapes.diamond()):
            self.assertTrue(geom[2], "should be solid")
        # ...while these ANNOTATE geometry, and filled would hide it: the
        # swing cone has the stud it limits inside it, and the ground cross
        # is the world, not a part.
        for geom in (shapes.swing_cone(0.4), shapes.ground_cross()):
            self.assertFalse(geom[2], "should be wire")
            self.assertTrue(geom[1])


class NormalsTest(unittest.TestCase):
    """Widgets are backface culled, so which way a face points is the
    difference between a solid bar and one you can see straight through."""

    def test_a_closed_widget_faces_outward(self):
        # Not "away from the center": an annulus's inner wall faces its own
        # axis, which IS outward for that solid. The test that holds for any
        # shape is that the mesh closes up and encloses a positive volume.
        cases = dict(MARKS)
        cases.update({
            "cuboid": shapes.cuboid(2.0, 0.4),
            "cylinder": shapes.cylinder(2.0, 0.4),
            "rail": shapes.stroke_bar(-0.3, 0.2, 0.05, pad=0.1),
            "round rail": shapes.stroke_bar(-0.3, 0.2, 0.05, pad=0.1,
                                            round_section=True),
            "diamond": shapes.diamond(0.4),
        })
        for name, (verts, _edges, faces) in sorted(cases.items()):
            bad, lonely = edge_pairing(faces)
            self.assertFalse(bad, "%s: %d edge(s) used twice the same way"
                             % (name, len(bad)))
            self.assertFalse(lonely, "%s: %d edge(s) with no facing pair. "
                             "the mesh is not closed" % (name, len(lonely)))
            self.assertGreater(enclosed_volume(verts, faces), 0.0,
                               "%s: wound inside out" % name)

    def test_a_flat_widget_is_drawn_from_both_sides(self):
        # A one-sided dial vanishes when the view crosses its plane, and a
        # dial you can read from only one side of the machine is half a dial.
        # A motion mark is a closed solid, so both sides come with it.
        for geom in (shapes.limit_arc(-0.5, 0.5), shapes.turn_arrow()):
            verts, _edges, faces = geom
            up = sum(1 for f in faces if normal_of(verts, f)[1] > 0.0)
            down = sum(1 for f in faces if normal_of(verts, f)[1] < 0.0)
            self.assertTrue(up and up == down,
                            "%d faces up, %d down" % (up, down))

    def test_the_back_of_a_flat_widget_is_its_own_copy(self):
        # Two faces built on ONE set of vertices are a duplicate face, and
        # Blender's mesh validation deletes duplicate faces.
        _verts, _edges, faces = shapes.limit_arc(-0.5, 0.5)
        seen = set()
        for face in faces:
            key = frozenset(face)
            self.assertNotIn(key, seen)
            seen.add(key)


class SlideLengthTest(unittest.TestCase):
    """Every slide widget is EXACTLY one nominal length along the axis. The
    rail behind it is padded by half of that, so a widget that overhung
    would leave the rail short at both stops."""

    def test_each_slide_widget_is_its_nominal_length(self):
        for name, geom in (("slide", shapes.slide_arrow(1.0, 0.07)),
                           ("turn and slide", shapes.turn_and_slide()),
                           ("screw", shapes.screw_arrow(1.0, 0.22, 2.0))):
            low, high = bounds(geom[0], 1)
            self.assertAlmostEqual(1.0, high - low, places=9, msg=name)
            self.assertAlmostEqual(0.0, high + low, places=9,
                                   msg=name + " is not centered on its origin")


class TurnArrowTest(unittest.TestCase):
    """A turn is a curved double arrow round +Y, open at the back, with a
    pointer at rest. Oscar, 2026-09-24: one mark for each freedom."""

    R, W, P = 0.45, 0.15, 0.35      # the revolute widget of rig_build

    def test_the_pointer_marks_the_rest_angle(self):
        verts, _edges, _faces = shapes.turn_arrow(self.R, self.W, self.P)
        # The tip is the farthest vertex, it sits outside the rim, and it
        # must be at t = 0 (+Z), which is where the joint rests.
        tip = max(verts, key=lambda v: v[0] ** 2 + v[2] ** 2)
        self.assertAlmostEqual(0.0, angle_of(tip), places=12)
        self.assertAlmostEqual(self.R * (1.0 + self.P),
                               math.hypot(tip[0], tip[2]), places=12)

    def test_a_vertex_lands_exactly_on_rest(self):
        # Half a segment out and the pointer would sit a few degrees away
        # from where the joint rests.
        for segments in (12, 32, 48, 64):
            verts, _e, _f = shapes.turn_arrow(segments=segments)
            on_rest = [v for v in verts
                       if abs(angle_of(v)) < 1e-12 and v[2] > 0.0]
            self.assertTrue(on_rest, "%d segments: nothing at t=0" % segments)

    def test_nothing_reaches_the_limit_band(self):
        # The limit arc of rig_build starts at 0.61 in the same plane. The
        # heads, the pointer and every other mark that turns stay inside it.
        band = 0.70 - 0.09
        for name in ("turn", "turn and slide", "plane", "pin in slot"):
            verts = MARKS[name][0]
            reach = max(math.hypot(v[0], v[2]) for v in verts)
            self.assertLess(reach, band, name)

    def test_it_is_open_at_the_back_and_its_heads_point_into_the_gap(self):
        gap = 0.7
        verts, _e, _f = shapes.turn_arrow(self.R, self.W, self.P, gap=gap)
        # Nothing within the gap, round t = pi.
        behind = [v for v in verts
                  if abs(abs(angle_of(v)) - math.pi) < gap * 0.5 - 1e-9]
        self.assertFalse(behind)
        # Each head ends in an edge at the gap: the two tips, each across
        # the thickness of the band, and nothing else that far round.
        edge = [v for v in verts
                if abs(abs(angle_of(v)) - (math.pi - gap * 0.5)) < 1e-9]
        self.assertEqual(4, len(edge))
        self.assertEqual({0.0}, {round(v[1], 12) for v in edge})

    def test_the_turn_is_centred_on_the_widget_origin(self):
        # The bone's head is drawn at the widget's origin, so a turn centered
        # anywhere else would ORBIT the joint. The inside of the band is a
        # circle about the origin, running the same way either side of rest.
        th = 0.05
        verts, _e, _f = shapes.turn_arrow(self.R, self.W, self.P,
                                          thickness=th)
        inner = at_radius(verts, self.R - th * 0.5)
        self.assertTrue(inner)
        angles = [angle_of(v) for v in inner]
        self.assertAlmostEqual(-min(angles), max(angles), places=9)

    def test_it_bends_across_its_thin_side(self):
        # Oscar, 2026-09-24: a curved arrow bends the way sheet bends. The
        # band is thin out from the axis and wide along it, so it is a
        # band round the axis and not a washer.
        th, w, heads = 0.05, 0.15, 0.33
        verts, _e, _f = shapes.turn_arrow(self.R, w, pointer=0.0,
                                          heads=heads, thickness=th)
        radii = [math.hypot(v[0], v[2]) for v in verts]
        self.assertAlmostEqual(self.R - th * 0.5, min(radii), places=6)
        self.assertLess(max(radii), self.R + th * 0.5 + 1e-9)
        self.assertAlmostEqual(self.R * heads, max(abs(v[1]) for v in verts),
                               places=9)
        self.assertGreater(self.R * w, th * 0.5)

    def test_a_ball_has_bands_that_bend_the_same_way(self):
        th = 0.03
        verts, _e, _f = shapes.ball_rings(radius=0.35, width=0.1,
                                          thickness=th, stub=1.0)
        for axis in (0, 1, 2):
            others = [i for i in (0, 1, 2) if i != axis]
            band = [v for v in verts if abs(v[axis]) <= 0.035 + 1e-9
                    and abs(math.hypot(v[others[0]], v[others[1]]) - 0.35)
                    <= th * 0.5 + 1e-9]
            self.assertTrue(band, "no band round axis %d" % axis)


class SlideArrowTest(unittest.TestCase):
    def test_one_flat_arrow(self):
        # Oscar, 2026-09-24: one arrow for a slide, not two crossed.
        th = 0.05
        verts, _e, _f = shapes.slide_arrow(1.0, 0.07, heads=0.2,
                                           thickness=th)
        self.assertAlmostEqual(th * 0.5, max(abs(v[0]) for v in verts))
        self.assertAlmostEqual(0.2, max(abs(v[2]) for v in verts))

    def test_the_tips_are_on_the_axis(self):
        verts, _e, _f = shapes.slide_arrow(1.0)
        top = [v for v in verts if abs(v[1] - 0.5) < 1e-12]
        self.assertTrue(top)
        for v in top:
            self.assertLess(math.hypot(v[0], v[2]), 0.05)


class OtherMarksTest(unittest.TestCase):
    def test_a_plane_slides_on_the_diagonals_and_keeps_rest_clear(self):
        verts, _e, _f = shapes.plane_arrows(reach=0.5, radius=0.28,
                                            pointer=0.35)
        far = [v for v in verts if math.hypot(v[0], v[2]) > 0.49]
        self.assertTrue(far)
        for v in far:
            a = abs(angle_of(v)) % (math.pi / 2.0)
            self.assertAlmostEqual(math.pi / 4.0, a, places=9)
        # the pointer of the turn is the only thing on +Z past the turn
        tip = [v for v in verts if abs(angle_of(v)) < 1e-12 and v[2] > 0.3]
        self.assertAlmostEqual(0.28 * 1.35, max(v[2] for v in tip), places=12)

    def test_a_pin_in_a_slot_slides_along_z(self):
        verts, _e, _f = shapes.pin_slot_arrows(length=1.2)
        self.assertAlmostEqual(0.6, max(v[2] for v in verts), places=12)
        self.assertAlmostEqual(-0.6, min(v[2] for v in verts), places=12)
        self.assertLess(max(abs(v[0]) for v in verts), 0.6)

    def test_a_ball_has_three_rings_and_a_stud(self):
        verts, _e, _f = shapes.ball_rings(radius=0.35, stub=1.0)
        self.assertAlmostEqual(1.0, max(v[1] for v in verts), places=12)


class LimitArcTest(unittest.TestCase):
    def test_the_arc_spans_exactly_the_limit(self):
        lo, hi = -0.4, 1.2
        verts, _edges, _faces = shapes.limit_arc(lo, hi, radius=1.0, width=0.09)
        # The inner rim belongs to the band alone: the rest rib stands off
        # the outer one, so it says where the band starts and stops.
        angles = [angle_of(v) for v in at_radius(verts, 1.0 - 0.09)]
        self.assertAlmostEqual(lo, min(angles), places=6)
        self.assertAlmostEqual(hi, max(angles), places=6)

    def test_the_band_is_all_there_is(self):
        # Live ClampRig: a clamp runs [-0.0014, +1.5665]. Nothing may stand off
        # the band: an end mark reads as a stray box stuck to the arc.
        w = 0.1125
        verts, _edges, _faces = shapes.limit_arc(-0.0014, 1.5665, radius=1.0,
                                                 width=w)
        self.assertAlmostEqual(1.0 + w,
                               max(math.hypot(v[0], v[2]) for v in verts),
                               places=9)
        self.assertAlmostEqual(1.0 - w,
                               min(math.hypot(v[0], v[2]) for v in verts),
                               places=9)

    def test_a_wide_limit_gets_more_segments_than_a_narrow_one(self):
        narrow = len(shapes.limit_arc(-0.05, 0.05)[0])
        wide = len(shapes.limit_arc(-math.pi, math.pi)[0])
        self.assertGreater(wide, narrow)


class StrokeBarTest(unittest.TestCase):
    def test_the_bar_is_the_travel_and_rest_sits_inside_it(self):
        lo, hi = -0.30, 0.05          # a ram mostly retracted
        verts, _edges, _faces = shapes.stroke_bar(lo, hi)
        low, high = bounds(verts, 1)
        self.assertAlmostEqual(lo, low, places=9)
        self.assertAlmostEqual(hi, high, places=9)
        # ...and rest is at 0, which is NOT the middle of an uneven stroke.
        self.assertLess(low, 0.0)
        self.assertGreater(high, 0.0)

    def test_the_pad_carries_the_rail_past_each_stop(self):
        # The slide widget is 2 bone-lengths long about its own origin, and
        # the limit clamps that origin. Padded by half the widget, the rail's
        # end meets the slide's end when the slide is hard against the stop.
        lo, hi, pad = -0.85, 0.0, 0.12
        verts, _edges, _faces = shapes.stroke_bar(lo, hi, 0.02, pad=pad)
        low, high = bounds(verts, 1)
        self.assertAlmostEqual(lo - pad, low, places=9)
        self.assertAlmostEqual(hi + pad, high, places=9)
        slide_end_at_stop = lo + pad          # slide origin at lo, +half up
        self.assertAlmostEqual(low + 2.0 * pad, slide_end_at_stop, places=9)

    def test_a_round_rail_is_round_and_a_square_one_is_not(self):
        # Same distinction the slide bar itself draws: round may spin.
        round_v = shapes.stroke_bar(0.0, 1.0, 0.1, round_section=True)[0]
        self.assertEqual({0.1}, {round(math.hypot(v[0], v[2]), 9)
                                 for v in round_v})
        square_v = shapes.stroke_bar(0.0, 1.0, 0.1)[0]
        self.assertNotIn(0.1, {round(math.hypot(v[0], v[2]), 9)
                               for v in square_v})

    def test_it_reads_as_a_bar_along_the_slide_axis(self):
        verts, _edges, _faces = shapes.stroke_bar(0.0, 1.0, 0.05)
        low, high = bounds(verts, 1)
        across = max(max(abs(v[0]) for v in verts),
                     max(abs(v[2]) for v in verts))
        self.assertGreater((high - low) / (2.0 * across), 1.5)


class SlideShapeTest(unittest.TestCase):
    """A round section may spin about the axis it slides along. A square one
    may not. That is the whole distinction being drawn."""

    def test_a_cylinder_is_round_and_a_cuboid_is_not(self):
        cyl, _e, _f = shapes.cylinder(length=2.0, radius=0.5)
        radii = {round(math.hypot(v[0], v[2]), 6) for v in cyl}
        self.assertEqual({0.5}, radii)

        box, _e, _f = shapes.cuboid(length=2.0, half_width=0.5)
        radii = {round(math.hypot(v[0], v[2]), 6) for v in box}
        self.assertEqual(1, len(radii))          # a cube's corners only
        self.assertNotAlmostEqual(0.5, radii.pop())

    def test_both_run_along_the_axis(self):
        for verts, _e, _f in (shapes.cylinder(length=3.0),
                              shapes.cuboid(length=3.0)):
            low, high = bounds(verts, 1)
            self.assertAlmostEqual(-1.5, low)
            self.assertAlmostEqual(1.5, high)


class SwingConeTest(unittest.TestCase):
    def test_the_cone_opens_to_the_swing_angle(self):
        for a in (0.2, 0.6, 1.2):
            verts, _e, _f = shapes.swing_cone(a, length=1.0)
            rim = [v for v in verts if v[1] > 1e-9]
            got = max(math.atan2(math.hypot(v[0], v[2]), v[1]) for v in rim)
            self.assertAlmostEqual(a, got, places=6)

    def test_the_apex_is_the_joint_centre(self):
        verts, _e, _f = shapes.swing_cone(0.5)
        self.assertIn((0.0, 0.0, 0.0), [tuple(v) for v in verts])

    def test_a_degenerate_angle_does_not_explode(self):
        for a in (0.0, -0.3, 10.0):
            verts, edges, _f = shapes.swing_cone(a)
            self.assertTrue(verts and edges)
            self.assertTrue(all(all(math.isfinite(c) for c in v)
                                for v in verts))


class ScrewArrowTest(unittest.TestCase):
    def test_a_screw_turns_as_it_travels(self):
        verts, _e, _f = shapes.screw_arrow(length=2.0, radius=0.5, turns=3.0,
                                           segments=120)
        low, high = bounds(verts, 1)
        self.assertAlmostEqual(-1.0, low)
        self.assertAlmostEqual(1.0, high)
        # The strip is laid down four vertices at a time, along the helix:
        # its angle unwraps through nearly three full circles, less the
        # heads.
        body = verts[:4 * 121:4]
        total = 0.0
        prev = angle_of(body[0])
        for v in body[1:]:
            cur = angle_of(v)
            d = cur - prev
            while d > math.pi:
                d -= 2.0 * math.pi
            while d < -math.pi:
                d += 2.0 * math.pi
            total += d
            prev = cur
        turns = abs(total) / (2.0 * math.pi)
        self.assertGreater(turns, 2.7)
        self.assertLess(turns, 3.0)

    def test_the_heads_follow_the_helix(self):
        # Oscar, 2026-09-24: the heads looked kinked. They narrow along the
        # helix, so every vertex stays on the cylinder the strip lies on.
        r, th, w, heads = 0.22, 0.04, 0.1, 0.14
        verts, _e, _f = shapes.screw_arrow(1.0, r, 2.0, width=w,
                                           thickness=th, heads=heads)
        slack = heads ** 2 / (2.0 * r)        # a flat width on a curve
        for v in verts:
            d = math.hypot(v[0], v[2])
            self.assertGreater(d, r - th * 0.5 - 1e-9)
            self.assertLess(d, r + th * 0.5 + slack)


if __name__ == "__main__":
    unittest.main()
