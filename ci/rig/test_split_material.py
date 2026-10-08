# SPDX-License-Identifier: GPL-3.0-or-later
"""Split by Material for a direct send: one part for each material of a
part (swmesh.split_by_material).

The pieces are what an update knows the parts by, so the names and the
paths here are a contract with every scene that holds them: the path of a
piece is the path of the part, swmesh.PIECE and the name of the material.
"""

import array
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import swmesh  # noqa: E402

GRAY = swmesh.Material(name="gray")
RED = swmesh.Material(name="red", rgba=(0.8, 0.1, 0.1, 1.0))
NAMELESS = swmesh.Material(name="", rgba=(0.0, 0.5, 1.0, 1.0))


def quad(def_id=1, name="arm", materials=(0, 1), body_starts=None):
    """Two triangles on four points, one material each, and two points
    more that only the second triangle of a second body would use."""
    return swmesh.Definition(
        id=def_id, name=name, vertex_count=4, triangle_count=2,
        positions=array.array("f", [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0]),
        normals=array.array("f", [0, 0, 1] * 4),
        uvs=array.array("f", [0, 0, 1, 0, 1, 1, 0, 1]),
        triangles=array.array("i", [0, 1, 2, 0, 2, 3]),
        triangle_materials=array.array("i", list(materials)),
        body_starts=body_starts)


def placed(def_id=1, path="sub-1/arm-1", name="arm-1", cid="c002"):
    return swmesh.Instance(definition_id=def_id, component_id=cid, name=name,
                           transform=[1.0] * 16, path=path, local=[2.0] * 16)


def scene(definitions, instances, materials=(GRAY, RED)):
    return swmesh.Scene(tolerance=0.001, materials=list(materials),
                        definitions=list(definitions),
                        instances=list(instances))


def test_a_part_of_two_materials_is_two_pieces():
    out = swmesh.split_by_material(scene([quad()], [placed()]))
    assert [d.name for d in out.definitions] == ["arm.gray", "arm.red"]
    assert [d.part for d in out.definitions] == ["arm", "arm"]
    assert [i.path for i in out.instances] == [
        "sub-1/arm-1|gray", "sub-1/arm-1|red"]
    assert [i.name for i in out.instances] == ["arm-1.gray", "arm-1.red"]
    # One component, so one bone: the pieces move as one.
    assert {i.component_id for i in out.instances} == {"c002"}
    assert all(i.transform == [1.0] * 16 and i.local == [2.0] * 16
               for i in out.instances)
    assert len({d.id for d in out.definitions}) == 2
    assert [i.definition_id for i in out.instances] == [
        d.id for d in out.definitions]


def test_each_piece_has_its_triangles_and_only_their_points():
    gray, red = swmesh.split_by_material(
        scene([quad()], [placed()])).definitions
    assert (gray.vertex_count, gray.triangle_count) == (3, 1)
    assert list(gray.triangles) == [0, 1, 2]
    assert list(gray.positions) == [0, 0, 0, 1, 0, 0, 1, 1, 0]
    assert list(gray.triangle_materials) == [0]
    # The second triangle uses points 0, 2 and 3 of the part.
    assert (red.vertex_count, red.triangle_count) == (3, 1)
    assert list(red.triangles) == [0, 1, 2]
    assert list(red.positions) == [0, 0, 0, 1, 1, 0, 0, 1, 0]
    assert list(red.uvs) == [0, 0, 1, 1, 0, 1]
    assert list(red.normals) == [0, 0, 1] * 3
    assert list(red.triangle_materials) == [1]


def test_a_part_of_one_material_is_not_changed():
    whole = quad(materials=(1, 1))
    inst = placed()
    out = swmesh.split_by_material(scene([whole], [inst]))
    assert out.definitions == [whole] and out.instances == [inst]
    assert out.definitions[0].part is None


def test_the_scene_that_comes_in_is_not_changed():
    before = scene([quad()], [placed()])
    swmesh.split_by_material(before)
    assert len(before.definitions) == 1 and len(before.instances) == 1
    assert before.instances[0].path == "sub-1/arm-1"
    assert list(before.definitions[0].triangles) == [0, 1, 2, 0, 2, 3]


def test_each_placement_of_a_part_gets_the_same_pieces():
    out = swmesh.split_by_material(scene(
        [quad()], [placed(path="arm-1", name="arm-1"),
                   placed(path="arm-2", name="arm-2", cid="c003")]))
    assert len(out.definitions) == 2
    assert [i.path for i in out.instances] == [
        "arm-1|gray", "arm-1|red", "arm-2|gray", "arm-2|red"]
    ids = [i.definition_id for i in out.instances]
    assert ids[0] == ids[2] and ids[1] == ids[3] and ids[0] != ids[1]


def test_the_first_material_of_the_part_is_the_first_piece():
    out = swmesh.split_by_material(scene([quad(materials=(1, 0))], [placed()]))
    assert [d.name for d in out.definitions] == ["arm.red", "arm.gray"]


def test_a_material_with_no_name_is_named_by_its_color():
    out = swmesh.split_by_material(
        scene([quad()], [placed()], materials=(GRAY, NAMELESS)))
    assert [i.path for i in out.instances] == [
        "sub-1/arm-1|gray", "sub-1/arm-1|0080ff"]


def test_two_materials_of_one_name_get_two_names():
    out = swmesh.split_by_material(
        scene([quad()], [placed()], materials=(GRAY, GRAY)))
    assert [i.path for i in out.instances] == [
        "sub-1/arm-1|gray", "sub-1/arm-1|gray 2"]


def test_a_part_with_no_path_stays_whole():
    # A file from before version 3 has no paths, and a piece is known by
    # its path only.
    out = swmesh.split_by_material(scene([quad()], [placed(path="")]))
    assert len(out.definitions) == 1 and len(out.instances) == 1


def test_a_part_that_the_scene_holds_whole_stays_whole():
    out = swmesh.split_by_material(
        scene([quad(1, "arm"), quad(2, "pin")],
              [placed(1, "arm-1"), placed(2, "pin-1")]),
        whole={"arm-1"})
    assert [i.path for i in out.instances] == [
        "arm-1", "pin-1|gray", "pin-1|red"]


def test_the_bodies_of_a_piece_start_at_its_own_points():
    # Body 1 is point 0 and 1, body 2 is point 2 and 3. The gray piece has
    # points 0, 1 and 2: two of body 1, one of body 2. The red piece has
    # points 0, 2 and 3: one of body 1, two of body 2.
    starts = array.array("i", [0, 2])
    gray, red = swmesh.split_by_material(
        scene([quad(body_starts=starts)], [placed()])).definitions
    assert list(gray.body_starts) == [0, 2]
    assert list(red.body_starts) == [0, 1]


def test_piece_of_reads_a_path():
    assert swmesh.piece_of("sub-1/arm-1|red") == ("sub-1/arm-1", "red")
    assert swmesh.piece_of("sub-1/arm-1") == ("sub-1/arm-1", "")
    assert swmesh.piece_of("") == ("", "")
