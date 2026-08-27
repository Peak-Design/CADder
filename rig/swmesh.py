# SPDX-License-Identifier: GPL-3.0-or-later
"""Reader for .swmesh: the geometry the SolidWorks add-in tessellates and
sends directly, instead of writing a STEP file for OpenCASCADE to re-read.

Deliberately dependency-free and bpy-free: the parse is pure Python and
testable on its own, and native_import.py turns the result into objects.

The layout is little-endian throughout and is written by
Core/MeshWriter.cs. Bulk arrays are read with array.array, which is a
memcpy when the byte order already matches: the point of a binary format
in the first place.

  header      magic 'SWMH', version, flags, tolerance, three counts
  materials   name, rgba, roughness, metallic, texture path
  definitions id, name, counts, positions, normals?, uvs?, triangles,
              one material index per triangle
  instances   definition id, component id, name, 4x4 row-major transform

A definition is a part tessellated once. An instance is one placement of
it. The component id is the same one the rig manifest uses, that is what
ties the two files together, and why nothing here has to be matched up by
name or position afterwards.
"""

import array
import struct
import sys
from dataclasses import dataclass, field
from typing import List, Optional

MAGIC = 0x484D5753          # 'SWMH'
VERSION = 1
FLAG_NORMALS = 1
FLAG_UVS = 2

_LITTLE = sys.byteorder == "little"


class SwMeshError(Exception):
    """The file is not a .swmesh this build can read."""


@dataclass
class Material:
    name: str
    rgba: tuple = (0.8, 0.8, 0.8, 1.0)
    roughness: float = 0.5
    metallic: float = 0.0
    texture: Optional[str] = None


@dataclass
class Definition:
    id: int
    name: str
    vertex_count: int
    triangle_count: int
    positions: array.array                     # 3 floats per vertex, metres
    normals: Optional[array.array] = None      # 3 per vertex
    uvs: Optional[array.array] = None          # 2 per vertex
    triangles: array.array = None              # 3 ints per triangle
    triangle_materials: array.array = None     # 1 int per triangle


@dataclass
class Instance:
    definition_id: int
    component_id: str
    name: str
    transform: List[float] = field(default_factory=list)   # 16, row-major


@dataclass
class Scene:
    tolerance: float = 0.0
    materials: List[Material] = field(default_factory=list)
    definitions: List[Definition] = field(default_factory=list)
    instances: List[Instance] = field(default_factory=list)

    def definition(self, def_id):
        for d in self.definitions:
            if d.id == def_id:
                return d
        return None


class _Reader:
    def __init__(self, data):
        self._data = data
        self._at = 0

    def _take(self, n):
        end = self._at + n
        if end > len(self._data):
            raise SwMeshError("file ends mid-record (want %d bytes at %d of %d)"
                              % (n, self._at, len(self._data)))
        chunk = self._data[self._at:end]
        self._at = end
        return chunk

    def u16(self):
        return struct.unpack_from("<H", self._take(2))[0]

    def u32(self):
        return struct.unpack_from("<I", self._take(4))[0]

    def i32(self):
        return struct.unpack_from("<i", self._take(4))[0]

    def f64(self):
        return struct.unpack_from("<d", self._take(8))[0]

    def f32(self):
        return struct.unpack_from("<f", self._take(4))[0]

    def text(self):
        n = self.u16()
        if n == 0:
            return ""
        return self._take(n).decode("utf-8", errors="replace")

    def block(self, typecode, count, itemsize):
        """A run of fixed-width numbers, straight into a typed buffer."""
        if count == 0:
            return array.array(typecode)
        a = array.array(typecode)
        a.frombytes(self._take(count * itemsize))
        if not _LITTLE:
            a.byteswap()
        return a


def parse(data) -> Scene:
    """Parses a .swmesh image. Raises SwMeshError on anything unreadable:
    a half-built scene is worse than none, because the half that is missing
    is invisible."""
    r = _Reader(data)
    if r.u32() != MAGIC:
        raise SwMeshError("not a .swmesh file")
    version = r.u32()
    if version != VERSION:
        raise SwMeshError("unsupported .swmesh version %d (this build reads %d)"
                          % (version, VERSION))
    flags = r.u32()
    scene = Scene(tolerance=r.f64())
    material_count = r.u32()
    definition_count = r.u32()
    instance_count = r.u32()
    has_normals = bool(flags & FLAG_NORMALS)
    has_uvs = bool(flags & FLAG_UVS)

    for _ in range(material_count):
        name = r.text()
        rgba = (r.f32(), r.f32(), r.f32(), r.f32())
        scene.materials.append(Material(
            name=name, rgba=rgba, roughness=r.f32(), metallic=r.f32(),
            texture=r.text() or None))

    for _ in range(definition_count):
        did = r.i32()
        name = r.text()
        vertex_count = r.u32()
        triangle_count = r.u32()
        positions = r.block("f", vertex_count * 3, 4)
        normals = r.block("f", vertex_count * 3, 4) if has_normals else None
        uvs = r.block("f", vertex_count * 2, 4) if has_uvs else None
        triangles = r.block("i", triangle_count * 3, 4)
        triangle_materials = r.block("i", triangle_count, 4)
        if max(triangles, default=-1) >= vertex_count:
            raise SwMeshError("definition %r indexes a vertex it does not have"
                              % name)
        scene.definitions.append(Definition(
            id=did, name=name, vertex_count=vertex_count,
            triangle_count=triangle_count, positions=positions,
            normals=normals, uvs=uvs, triangles=triangles,
            triangle_materials=triangle_materials))

    for _ in range(instance_count):
        scene.instances.append(Instance(
            definition_id=r.i32(),
            component_id=r.text(),
            name=r.text(),
            transform=[r.f64() for _ in range(16)]))

    return scene


def load(path) -> Scene:
    with open(path, "rb") as fh:
        return parse(fh.read())
