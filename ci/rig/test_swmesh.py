# SPDX-License-Identifier: GPL-3.0-or-later
"""The .swmesh format crosses a language boundary, so the test that means
something is the one where the writer and the reader are the real ones.
The C# side (MeshWriterTests.WritesTheGoldenFileTheConsumerTestReads) emits
a sample into its own build output. This reads it back and checks every
field, because a wire format fails by putting the right bytes in the wrong
slot, which loads fine and looks wrong.

The golden file is skipped when the add-in has not been built, so this
suite still runs on a machine with no .NET.
"""

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from STEPper_NEXT.rig import swmesh  # noqa: E402

# Written by the C# test suite into its own source tree. See the module
# docstring for why the two halves have to meet on a real file.
GOLDEN = os.path.join(
    "C:", os.sep, "PeakDesign", "SW-To-Blender", "sw-addin", "tests",
    "Peak.SwToBlender.Tests", "golden", "sample.swmesh")


@pytest.mark.skipif(not os.path.exists(GOLDEN),
                    reason="add-in not built. No golden .swmesh to read")
def test_reads_the_scene_the_addin_wrote():
    scene = swmesh.load(GOLDEN)

    assert scene.tolerance == pytest.approx(0.000125)
    assert len(scene.materials) == 2
    assert len(scene.definitions) == 1
    assert len(scene.instances) == 1

    grey, red = scene.materials
    assert grey.name == "grey"
    assert red.name == "red"
    assert red.rgba == pytest.approx((1.0, 0.25, 0.125, 0.5))
    assert red.roughness == pytest.approx(0.75)
    assert red.metallic == pytest.approx(0.0)
    assert red.texture is None

    d = scene.definitions[0]
    assert d.id == 3
    assert d.name == "bracket"
    assert d.vertex_count == 4
    assert d.triangle_count == 2
    assert list(d.positions) == pytest.approx(
        [0, 0, 0, 1, 0, 0, 0, 2, 0, 1, 2, 0])
    assert list(d.normals) == pytest.approx([0, 0, 1] * 4)
    assert list(d.uvs) == pytest.approx([0, 0, 1, 0, 0, 1, 1, 1])
    assert list(d.triangles) == [0, 1, 2, 1, 3, 2]
    # Per TRIANGLE, not per vertex: the whole reason the field exists.
    assert list(d.triangle_materials) == [1, 0]

    inst = scene.instances[0]
    assert inst.definition_id == 3
    assert inst.component_id == "c007"
    assert inst.name == "bracket-1"
    # Row-major: the translation is the fourth COLUMN, so elements 3, 7, 11.
    assert [inst.transform[i] for i in (3, 7, 11)] == pytest.approx([0.5, 1.5, 2.5])
    assert scene.definition(3) is d
    assert scene.definition(99) is None


def test_rejects_a_file_that_is_not_swmesh():
    with pytest.raises(swmesh.SwMeshError):
        swmesh.parse(b"not a mesh at all")


def test_rejects_a_future_version():
    header = struct.pack("<III", swmesh.MAGIC, swmesh.VERSION + 1, 0)
    with pytest.raises(swmesh.SwMeshError) as excinfo:
        swmesh.parse(header + b"\x00" * 64)
    assert "version" in str(excinfo.value)


def test_rejects_a_truncated_file():
    # A plausible header promising far more than the file holds: the reader
    # must refuse rather than build half a scene, because the missing half
    # is invisible once it is in the viewport.
    header = struct.pack("<IIIdIII", swmesh.MAGIC, swmesh.VERSION, 0,
                         0.001, 0, 1, 0)
    with pytest.raises(swmesh.SwMeshError):
        swmesh.parse(header + b"\x00" * 8)


def test_rejects_triangles_that_index_missing_vertices():
    body = struct.pack("<IIIdIII", swmesh.MAGIC, swmesh.VERSION, 0,
                       0.001, 0, 1, 0)
    body += struct.pack("<i", 0)                 # definition id
    body += struct.pack("<H", 0)                 # empty name
    body += struct.pack("<II", 3, 1)             # 3 vertices, 1 triangle
    body += struct.pack("<9f", *([0.0] * 9))     # positions
    body += struct.pack("<3i", 0, 1, 7)          # 7 does not exist
    body += struct.pack("<i", 0)                 # triangle material
    with pytest.raises(swmesh.SwMeshError) as excinfo:
        swmesh.parse(body)
    assert "indexes a vertex" in str(excinfo.value)
