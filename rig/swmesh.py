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
  materials   name, rgba, roughness, metallic, texture path, and (version
              2) the SolidWorks appearance as JSON, uint32-length-prefixed
  definitions id, name, counts, positions, normals?, uvs?, triangles,
              one material index per triangle
  instances   definition id, component id, name, path (version 3), 4x4
              row-major transform, and (version 3) one byte that says
              whether a second 4x4 follows: where the placement sits
              inside its component
  nodes       (version 3) path, name, component id, 4x4 row-major transform
  sections    (optional, after the nodes) a 4-byte tag, a uint32 byte
              length, and the data. A reader skips a tag it does not know,
              and an older reader stops before the first one, so a section
              adds to the format without a new version number.
              BODY: for each definition in file order, a uint32 count and
              that many int32 first-vertex indices, one for each body

A definition is a part tessellated once. An instance is one placement of
it. The component id is the same one the rig manifest uses, that is what
ties the two files together, and why nothing here has to be matched up by
name or position afterwards.

A node is a subassembly occurrence: a branch of the tree that holds
instances rather than geometry. The path is the occurrence path from the
root ("lifter-1/rod-2"), which says what hangs under what. Several
instances share one component id where a rigid subassembly holds several
parts: the rig moves the subassembly as one body, and the tree still shows
the parts.
"""

import array
import struct
import sys
from dataclasses import dataclass, field
from typing import List, Optional

MAGIC = 0x484D5753          # 'SWMH'
VERSION = 3
READABLE_VERSIONS = (1, 2, 3)
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
    # Version 2: the whole SolidWorks appearance (colours, finish, library
    # values, mapping, decals) as the add-in wrote it. None in version 1.
    appearance_json: Optional[str] = None


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
    # The first vertex of each body, from the BODY section. None when the
    # file has no section, which means one body, or a file from an add-in
    # that did not write it.
    body_starts: Optional[array.array] = None
    # The name of the part that this definition is one material of
    # (split_by_material). None for a whole part.
    part: Optional[str] = None


@dataclass
class Instance:
    definition_id: int
    component_id: str
    name: str
    transform: List[float] = field(default_factory=list)   # 16, row-major
    # The occurrence path from the root, version 3 and later. Empty in an
    # older file, where the assembly tree could only be read from the rig
    # manifest.
    path: str = ""
    # Where this placement sits inside its component, 16 numbers. None for
    # a part that IS its component, which is every part outside a rigid
    # subassembly.
    local: Optional[List[float]] = None


@dataclass
class Node:
    """One subassembly occurrence: a branch of the assembly tree."""
    path: str
    name: str
    component_id: str = ""
    transform: List[float] = field(default_factory=list)   # 16, row-major


@dataclass
class Scene:
    tolerance: float = 0.0
    materials: List[Material] = field(default_factory=list)
    definitions: List[Definition] = field(default_factory=list)
    instances: List[Instance] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)

    def definition(self, def_id):
        for d in self.definitions:
            if d.id == def_id:
                return d
        return None


# Between the path of a part and the material of one piece of it
# (split_by_material). A file name cannot hold this character, so the path
# of a part from the CAD application does not hold it.
PIECE = "|"


def piece_of(path):
    """(the path of the part, the material of the piece). The material is
    "" for a part that is whole."""
    part, _sep, material = (path or "").partition(PIECE)
    return part, material


def split_by_material(scene, whole=()):
    """The scene with one part for each material of a part.

    A part with faces in two or more materials becomes that many pieces.
    Each piece has the triangles of one material, the name of the part
    with the material after it, and a path of its own: the path of the
    part, PIECE and the material. The path is what an update knows a part
    by, so an update finds each piece again and leaves it as it is. The
    component is the same for all the pieces, so they are on one bone and
    move as one.

    A part of one material is not changed. `whole` holds paths of parts to
    leave whole: a part that has no path cannot be told apart from its
    pieces, so it stays whole too. The scene that comes in is not changed.
    """
    import numpy as np

    labels = [m.name or "%02x%02x%02x" % tuple(
        int(max(0.0, min(1.0, float(c))) * 255 + 0.5) for c in m.rgba[:3])
        for m in scene.materials]
    users = {}
    for inst in scene.instances:
        users.setdefault(inst.definition_id, []).append(inst)
    out = Scene(tolerance=scene.tolerance, materials=scene.materials,
                nodes=scene.nodes)
    next_id = max([d.id for d in scene.definitions], default=0) + 1
    pieces = {}
    for d in scene.definitions:
        placed = users.get(d.id, [])
        if d.triangle_materials is None or not d.triangle_count \
                or any(not i.path or i.path in whole for i in placed):
            out.definitions.append(d)
            continue
        mat = np.frombuffer(d.triangle_materials,
                            dtype=np.dtype(d.triangle_materials.typecode))
        kinds, first = np.unique(mat, return_index=True)
        if len(kinds) < 2:
            out.definitions.append(d)
            continue
        tri = np.frombuffer(
            d.triangles, dtype=np.dtype(d.triangles.typecode)).reshape(-1, 3)
        made, taken = [], {}
        # In the order the materials come in the part, which is the order
        # of its faces: the first material keeps the first name.
        for material in [int(kinds[i]) for i in np.argsort(first)]:
            label = labels[material] if 0 <= material < len(labels) \
                else "material %d" % material
            taken[label] = taken.get(label, 0) + 1
            if taken[label] > 1:
                label = "%s %d" % (label, taken[label])
            faces = tri[mat == material]
            used = np.unique(faces)
            remap = np.full(d.vertex_count, -1, dtype=np.int64)
            remap[used] = np.arange(len(used))

            def rows(block, width):
                if block is None:
                    return None
                kept = np.frombuffer(block, dtype=np.dtype(
                    block.typecode)).reshape(-1, width)[used]
                return array.array(block.typecode, kept.tobytes())

            starts = None
            if d.body_starts is not None:
                edges = list(d.body_starts) + [d.vertex_count]
                at = np.searchsorted(used, edges)
                starts = array.array(d.body_starts.typecode, [
                    int(at[i]) for i in range(len(edges) - 1)
                    if at[i + 1] > at[i]])
            made.append((label, Definition(
                id=next_id, name="%s.%s" % (d.name, label),
                vertex_count=len(used), triangle_count=len(faces),
                positions=rows(d.positions, 3), normals=rows(d.normals, 3),
                uvs=rows(d.uvs, 2),
                triangles=array.array(d.triangles.typecode, remap[faces]
                                      .astype(tri.dtype).tobytes()),
                triangle_materials=array.array(
                    d.triangle_materials.typecode, [material] * len(faces)),
                body_starts=starts, part=d.name)))
            out.definitions.append(made[-1][1])
            next_id += 1
        pieces[d.id] = made
    for inst in scene.instances:
        made = pieces.get(inst.definition_id)
        if made is None:
            out.instances.append(inst)
            continue
        for label, definition in made:
            out.instances.append(Instance(
                definition_id=definition.id, component_id=inst.component_id,
                name="%s.%s" % (inst.name or inst.component_id, label),
                transform=inst.transform, path=inst.path + PIECE + label,
                local=inst.local))
    return out


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

    def left(self):
        return len(self._data) - self._at

    def u8(self):
        return self._take(1)[0]

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
    if version not in READABLE_VERSIONS:
        raise SwMeshError("unsupported .swmesh version %d (this build reads %s)"
                          % (version, ", ".join(str(v) for v in READABLE_VERSIONS)))
    flags = r.u32()
    scene = Scene(tolerance=r.f64())
    material_count = r.u32()
    definition_count = r.u32()
    instance_count = r.u32()
    node_count = r.u32() if version >= 3 else 0
    has_normals = bool(flags & FLAG_NORMALS)
    has_uvs = bool(flags & FLAG_UVS)

    for _ in range(material_count):
        name = r.text()
        rgba = (r.f32(), r.f32(), r.f32(), r.f32())
        material = Material(
            name=name, rgba=rgba, roughness=r.f32(), metallic=r.f32(),
            texture=r.text() or None)
        if version >= 2:
            n = r.u32()
            material.appearance_json = r._take(n).decode("utf-8", errors="replace") if n else None
        scene.materials.append(material)

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
        definition_id = r.i32()
        component_id = r.text()
        name = r.text()
        path = r.text() if version >= 3 else ""
        transform = [r.f64() for _ in range(16)]
        local = None
        if version >= 3 and r.u8():
            local = [r.f64() for _ in range(16)]
        scene.instances.append(Instance(
            definition_id=definition_id,
            component_id=component_id,
            name=name,
            transform=transform,
            path=path,
            local=local))

    for _ in range(node_count):
        scene.nodes.append(Node(
            path=r.text(),
            name=r.text(),
            component_id=r.text(),
            transform=[r.f64() for _ in range(16)]))

    _sections(r, scene)
    return scene


def _sections(r, scene):
    """Reads the tagged sections after the node table, and steps over the
    ones this build does not know."""
    while r.left() >= 8:
        tag = bytes(r._take(4))
        length = r.u32()
        if length > r.left():
            raise SwMeshError("section %r runs past the end of the file" % tag)
        end = r._at + length
        if tag == b"BODY":
            for d in scene.definitions:
                count = r.u32()
                if r._at + count * 4 > end:
                    raise SwMeshError("BODY section is shorter than it says")
                d.body_starts = r.block("i", count, 4)
        r._at = end


def load(path) -> Scene:
    with open(path, "rb") as fh:
        return parse(fh.read())
