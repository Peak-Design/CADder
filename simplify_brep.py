# SPDX-License-Identifier: GPL-3.0-or-later
"""Leaving small features out of a part that came from a STEP file.

The direct link asks SolidWorks to leave them out, because SolidWorks holds
the part. A STEP file holds the part here, as a boundary representation, so
the same decision is made here, on the shape, before it is tessellated.

The rule is the rule the add-in uses, and it is stated once so that the two
routes cannot drift apart. The unit is a small INNER WIRE of a face, which
covers round holes, slots, keyways and small cutouts alike. What sits
behind one is found by walking INWARD from it: cross into the face on the
other side, then keep crossing every edge that is not itself on a marked
wire. A through hole gives one cylinder bounded by two marked wires, a
blind hole gives the cylinder and its bottom, a counterbore gives cylinder,
annulus, cylinder and bottom. The walk gives up if it escapes into the body
or comes back to the face it started from, and the feature then stays.

What happens NEXT is where the two routes differ, and they differ because
the material differs. The add-in has triangles and no modeller, so it
drops the faces and rebuilds the ones that lose a rim. Here there is a real
solid and a real modeller, so OCCT's own Defeaturing removes the faces and
extends the ones around them to close the gap. That is exact, and it is
also why nothing here has to fill a polygon or cap a rim.

Nothing is written back to the STEP file. The shape this returns is a copy
in memory, used for one tessellation.
"""

#: OCCT hands its geometry over in millimetres whatever the file declares,
#: because the reader converts every representation context at transfer
#: (see importer.py). The size dial is in metres, so it is scaled here.
MM_PER_METRE = 1000.0

#: A walk that reaches this many faces has not found a pocket, it has found
#: its way out into the body. Abandon it, so an awkward shape cannot cost
#: the import.
FACE_BUDGET = 24


def _list(shapes):
    """The members of a TopTools_ListOfShape, whichever way this build of
    the bindings lets them be read."""
    try:
        return list(shapes)
    except TypeError:
        pass
    from OCP.TopTools import TopTools_ListIteratorOfListOfShape
    out = []
    walker = TopTools_ListIteratorOfListOfShape(shapes)
    while walker.More():
        out.append(walker.Value())
        walker.Next()
    return out


def _span(shape):
    """How wide a shape is: the WIDEST SIDE of its bounding box, in the
    shape's own units.

    Not the diagonal. A dial that says 12 mm has to mean a 12 mm hole, and
    the diagonal of a circle's box is 1.414 times its diameter whichever way
    the circle faces, so the diagonal would make the dial mean 8.5 mm. The
    widest side is the diameter exactly for a hole drilled along an axis,
    never less than 0.82 of it for one drilled at an angle, and the length
    of a slot rather than its diagonal.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    try:
        BRepBndLib.Add_s(shape, box, True)
        if box.IsVoid():
            return None
        x0, y0, z0, x1, y1, z1 = box.Get()
    except Exception:
        return None
    return max(x1 - x0, y1 - y0, z1 - z0)


def _is_plane(face):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_SurfaceType
    try:
        return (BRepAdaptor_Surface(face).GetType()
                == GeomAbs_SurfaceType.GeomAbs_Plane)
    except Exception:
        return False


def _maps(shape):
    """Faces and edges numbered, and the faces of every edge. Numbers rather
    than shapes, because a TopoDS_Shape is a handle and two handles on one
    face are not equal to each other."""
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopTools import (TopTools_IndexedMapOfShape,
                              TopTools_IndexedDataMapOfShapeListOfShape)
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, faces)
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, edges)
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(
        shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE,
        edge_faces)
    return faces, edges, edge_faces


def _wires(face):
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    found = []
    walker = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_WIRE)
    while walker.More():
        found.append(walker.Current())
        walker.Next()
    return found


def _edges_of(shape, edges):
    """The numbers of a wire's edges."""
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    found = []
    walker = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    while walker.More():
        at = edges.FindIndex(walker.Current())
        if at > 0 and at not in found:
            found.append(at)
        walker.Next()
    return found


def rims(shape, max_extent_m, curved=False):
    """Every small inner wire of the shape, as (face number, edge numbers).

    A wire that is small enough to be a feature. Whether the feature behind
    it can actually go is the walk's business, not this one's.
    """
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS
    faces, edges, _ancestors = _maps(shape)
    limit = max_extent_m * MM_PER_METRE
    found = []
    for i in range(1, faces.Extent() + 1):
        face = TopoDS.Face_s(faces.FindKey(i))
        if not curved and not _is_plane(face):
            continue
        try:
            outer = BRepTools.OuterWire_s(face)
        except Exception:
            continue
        for wire in _wires(face):
            if wire.IsSame(outer):
                continue
            span = _span(wire)
            if span is None or span > limit:
                continue
            numbers = _edges_of(wire, edges)
            if numbers:
                found.append((i, numbers))
    return found


def plan(shape, max_extent_m, curved=False, log=None):
    """The faces a shape could be sent without, by number, and the count of
    features that were left alone.

    Returns (face numbers, removed, declined, faces map).
    """
    faces, edges, ancestors = _maps(shape)
    marked = rims(shape, max_extent_m, curved)
    if not marked:
        return set(), 0, 0, faces

    walls = set()
    for _face, numbers in marked:
        walls.update(numbers)

    def across(edge_number, face_number):
        """The faces on the other side of an edge from this one."""
        try:
            neighbours = _list(ancestors.FindFromKey(edges.FindKey(edge_number)))
        except Exception:
            return []
        out = []
        for neighbour in neighbours:
            at = faces.FindIndex(neighbour)
            if at > 0 and at != face_number:
                out.append(at)
        return out

    gone, removed, declined = set(), 0, 0
    claimed = set()
    # A through hole has a rim at each end, and both find the same region.
    # It is one feature, so it is counted once.
    already = set()
    for seed, numbers in marked:
        key = frozenset(numbers)
        if key in claimed:
            continue
        claimed.add(key)
        region, queue, why = set(), [], None
        for number in numbers:
            for face_number in across(number, seed):
                if face_number not in region:
                    region.add(face_number)
                    queue.append(face_number)
        if not region:
            why = "the wire has nothing behind it"
        while queue and why is None:
            if len(region) > FACE_BUDGET:
                why = "the walk passed %d faces without closing" % FACE_BUDGET
                break
            here = queue.pop()
            for number in _edges_of(faces.FindKey(here), edges):
                if number in walls:
                    continue                      # a wall of the pocket
                for face_number in across(number, here):
                    if face_number == seed:
                        # Round the OUTSIDE of the body rather than into a
                        # pocket. Without this a part with few enough faces
                        # to stay inside the budget came back as one feature
                        # holding every face it had.
                        why = "the walk came back to the face it started from"
                        break
                    if face_number in region:
                        continue
                    if len(region) >= FACE_BUDGET:
                        why = ("the walk passed %d faces without closing"
                               % FACE_BUDGET)
                        break
                    region.add(face_number)
                    queue.append(face_number)
                if why is not None:
                    break
        if why is not None:
            declined += 1
            if log is not None:
                log("simplify: a feature stays, %s" % why)
            continue
        shape_key = frozenset(region)
        if shape_key in already:
            continue
        already.add(shape_key)
        removed += 1
        gone.update(region)
    return gone, removed, declined, faces


def apply(shape, max_extent_m, curved=False, log=None):
    """
    The shape without its small features, as (shape, history, removed,
    declined). The shape is None when it keeps them.

    None is not a failure to report as such. The contract is "simplified" or
    "left alone", so a shape OCCT will not rebuild simply travels as it is.

    The history says what became of each face, which is what lets the colors
    of the old faces follow on to the new ones (carry_colors).
    """
    if shape is None or not (max_extent_m > 0.0):
        return None, None, 0, 0
    try:
        gone, removed, declined, faces = plan(shape, max_extent_m, curved, log)
    except Exception as exc:                        # noqa: BLE001
        if log is not None:
            log("simplify: the shape could not be read: %s" % exc)
        return None, None, 0, 0
    if not gone:
        return None, None, 0, declined

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
    from OCP.TopTools import TopTools_ListOfShape
    drop = TopTools_ListOfShape()
    for number in sorted(gone):
        drop.Append(faces.FindKey(number))
    algo = BRepAlgoAPI_Defeaturing()
    algo.SetShape(shape)
    algo.AddFacesToRemove(drop)
    try:
        algo.SetRunParallel(True)
    except Exception:
        pass
    # The history is what carries the face colors over, so it is worth the
    # little it costs.
    try:
        algo.SetToFillHistory(True)
    except Exception:
        pass
    history = None
    try:
        algo.Build()
        done = algo.IsDone()
        result = algo.Shape() if done else None
        if done:
            history = algo.History()
    except Exception as exc:                        # noqa: BLE001
        if log is not None:
            log("simplify: OCCT would not rebuild the shape: %s" % exc)
        return None, None, 0, removed + declined
    if not done or result is None or result.IsNull():
        if log is not None:
            log("simplify: OCCT would not rebuild the shape, so it is sent "
                "as it is")
        return None, None, 0, removed + declined

    # A rebuild that lost the solid is not a simplification of it. Cheap to
    # check and the one thing that would be expensive to notice later.
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape
    after = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(result, TopAbs_ShapeEnum.TopAbs_FACE, after)
    if after.Extent() < 1 or after.Extent() >= faces.Extent():
        if log is not None:
            log("simplify: the rebuilt shape has %d face(s) against %d, so "
                "the part is sent as it is" % (after.Extent(), faces.Extent()))
        return None, None, 0, removed + declined
    if log is not None:
        log("simplify: %d feature(s) removed, %d left alone, %d face(s) to %d"
            % (removed, declined, faces.Extent(), after.Extent()))
    return result, history, removed, declined


def carry_colors(reader, old, new, history):
    """Gives the rebuilt shape the colors the old one had.

    A STEP file can color a whole part, a body inside it, or one face, and
    the reader holds those against the shapes it read. The rebuilt shape is
    made of NEW faces, so without this a simplified part would arrive in
    whatever color a part with nothing said about it gets.

    The history says what became of each shape: gone, changed into others,
    or left as it was. Each one that survives is entered again under its new
    name with the color it had.
    """
    if reader is None or new is None:
        return
    from .ocp_utils import ShapeKey as key_of

    def color_of(shape):
        return (reader.face_colors.get(key_of(shape)),
                reader.face_color_priority.get(key_of(shape), 0))

    color, priority = color_of(old)
    if color is not None:
        reader.face_colors[key_of(new)] = color
    reader.face_color_priority[key_of(new)] = priority

    carried = []
    for sub_shape in reader.sub_shapes.get(key_of(old), []) or []:
        try:
            if history is not None and history.IsRemoved(sub_shape):
                continue
            changed = _list(history.Modified(sub_shape)) if history else []
        except Exception:                           # noqa: BLE001
            changed = []
        color, priority = color_of(sub_shape)
        for now in (changed or [sub_shape]):
            carried.append(now)
            if color is not None:
                reader.face_colors[key_of(now)] = color
            reader.face_color_priority[key_of(now)] = priority
    if carried:
        reader.sub_shapes[key_of(new)] = carried
