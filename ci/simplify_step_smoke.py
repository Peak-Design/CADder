# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for leaving small features out of a STEP part.

The direct link asks SolidWorks to do it. A STEP file has no application
behind it, so it is done here, on the boundary representation, before the
shape is tessellated. Four things have to hold:

- the small holes go and the big one stays, which is what the size dial
  means;
- the part is still a closed solid, because a part with a hole in its side
  is worse than a part with its bolt holes in;
- the switch decides it, so a part nobody marked comes back untouched;
- a collection above the part decides for it, the same rule the live link
  follows.

The fixture is built by fixtures/make_holes.py: a 100 x 60 x 10 mm plate
with four 6 mm bolt holes and one 30 mm bore.

Run:  blender -b --factory-startup -P simplify_step_smoke.py
"""

import os
import sys

import bmesh
import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

FIXTURE = os.path.join(HERE, "fixtures", "holes.step")

#: Where the holes are in the fixture and how wide, in metres, which is
#: what the import scales them to. Asking whether a part still has points
#: around a known centre says which holes are left without depending on how
#: the tessellator chose to cut them.
SMALL = [((0.012, 0.012), 0.006), ((0.088, 0.012), 0.006),
         ((0.012, 0.048), 0.006), ((0.088, 0.048), 0.006)]
BIG = ((0.050, 0.030), 0.030)


def open_edges(obj):
    """Edges with anything but two faces on them, once the points a CAD mesh
    carries twice are joined. Zero is a closed solid."""
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
        return sum(1 for e in bm.edges if len(e.link_faces) != 2)
    finally:
        bm.free()


def has_hole(obj, where, diameter):
    """Whether the part still has points around this centre. A hole that has
    gone leaves the face flat there, so nothing is within its radius."""
    (x, y), radius = where, diameter * 0.5
    matrix = obj.matrix_world
    for vertex in obj.data.vertices:
        point = matrix @ vertex.co
        if (point.x - x) ** 2 + (point.y - y) ** 2 <= (radius * 1.2) ** 2:
            return True
    return False


def holes(obj):
    """(small holes left, the big bore is left)."""
    return (sum(1 for where, size in SMALL if has_hole(obj, where, size)),
            has_hole(obj, BIG[0], BIG[1]))


def load():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    bpy.ops.import_scene.occ_import_step(
        filepath=FIXTURE, hierarchy_types="FLAT", up_as="ZPOS")
    parts = [o for o in bpy.context.scene.objects
             if o.type == "MESH" and "STEP_tag" in o]
    assert parts, "the fixture did not import"
    return parts[0]


def main():
    # 1. As it is: five holes, and a closed solid.
    plate = load()
    assert open_edges(plate) == 0, "the fixture is not closed to begin with"
    plain_faces = len(plate.data.polygons)
    assert holes(plate) == (4, True), \
        "the fixture does not have the holes it should: %s" % (holes(plate),)

    # 2. Nobody marked it, so nothing happens. The switch decides, and a
    #    part that says nothing gets the geometry it has always had.
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    plate.select_set(True)
    bpy.context.view_layer.objects.active = plate
    assert bpy.ops.stepper.apply_simplify.poll(), "the operator refused a STEP part"
    bpy.ops.stepper.apply_simplify()
    assert holes(plate) == (4, True), "an unmarked part came back changed"

    # 3. Marked: the four small holes go, the 30 mm bore stays, and the
    #    plate is still closed.
    plate.cad_simplify.enabled = True
    plate.cad_simplify.size = 0.012
    bpy.ops.stepper.apply_simplify()
    small, big = holes(plate)
    faces = len(plate.data.polygons)
    assert small == 0, "%d bolt hole(s) stayed at a 12 mm dial" % small
    assert big, "the 30 mm bore went at a 12 mm dial"
    assert faces < plain_faces, \
        "the mesh did not get smaller: %d faces either way" % faces
    assert open_edges(plate) == 0, \
        "the simplified plate has %d open edge(s)" % open_edges(plate)

    # 4. The size dial means what it says: under the bore, over the bolt
    #    holes, and the bore goes as well.
    plate2 = load()
    plate2.cad_simplify.enabled = True
    plate2.cad_simplify.size = 0.040
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    plate2.select_set(True)
    bpy.context.view_layer.objects.active = plate2
    bpy.ops.stepper.apply_simplify()
    assert holes(plate2) == (0, False), \
        "a 40 mm dial left %s behind" % (holes(plate2),)
    assert open_edges(plate2) == 0, "the plate with no holes is not closed"

    # 5. A collection above the part decides for it, which is the rule the
    #    live link follows and has to be one rule for both.
    plate3 = load()
    group = bpy.data.collections.new("plates")
    bpy.context.scene.collection.children.link(group)
    for holder in list(plate3.users_collection):
        holder.objects.unlink(plate3)
    group.objects.link(plate3)
    group.cad_simplify.enabled = True
    group.cad_simplify.size = 0.012
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    plate3.select_set(True)
    bpy.context.view_layer.objects.active = plate3
    bpy.ops.stepper.apply_simplify(scope="COLLECTION")
    assert holes(plate3) == (0, True), \
        "the collection did not decide for the part: %s" % (holes(plate3),)
    assert open_edges(plate3) == 0, "the part the collection covered is open"

    print("simplify_step_smoke: OK, four bolt holes went and the bore "
          "stayed, %d faces to %d, and the plate is still closed"
          % (plain_faces, faces))


main()
