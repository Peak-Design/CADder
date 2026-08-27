# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# Copyright 2021 Tommi Hyppänen
#
# Modified 2026 by Peak-Design:
#   - Updated to pythonocc-core 7.9.3 (OpenCASCADE 7.9.3)
#   - Fixed tessellation race conditions and re-tessellation fallback
#   - Improved corrupt STEP file handling
#   - Added failed parts diagnostics
#   - Added ShapeFix healing and per-face entity recovery for broken shapes
#   - Scoped face recovery to only unmapped entities (prevents geometry bleed)
#   - Migrated to OCP bindings (cadquery-ocp-novtk 7.9.3.1.1 / OCCT 7.9.3);
#     native module now receives faces via BinTools serialize handoff

import sys
from os.path import dirname

file_dirname = dirname(__file__)
if file_dirname not in sys.path:
    sys.path.append(file_dirname)

import importlib
import os
import threading
from collections import defaultdict, OrderedDict
from dataclasses import dataclass, field

import numpy as np

# import trimesh works in dev, but not in deploy
from . import trimesh
from . import nurbs

importlib.reload(trimesh)
import io

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IMeshTools import IMeshTools_Parameters
from OCP.Precision import Precision
from OCP.BRepTools import BRepTools
from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.ShapeFix import ShapeFix_Shape
from OCP.GeomConvert import GeomConvert
from OCP.gp import gp, gp_Dir, gp_Pln, gp_Pnt, gp_Pnt2d, gp_Trsf, gp_Vec, gp_XYZ
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TColStd import TColStd_SequenceOfAsciiString
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDataStd import TDataStd_TreeNode
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import (
    TopAbs_COMPOUND,
    TopAbs_EDGE,
    TopAbs_FACE,
    TopAbs_FORWARD,
    TopAbs_INTERNAL,
    TopAbs_REVERSED,
    TopAbs_SHELL,
    TopAbs_SOLID,
    TopAbs_VERTEX,
    TopAbs_WIRE,
)
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape, TopoDS_Compound, TopoDS
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import (
    XCAFDoc,
    XCAFDoc_DocumentTool,
    XCAFDoc_Material,
    XCAFDoc_ShapeTool,
    XCAFDoc_ColorTool,
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorCurv,
)
from OCP.StepBasic import StepBasic_ProductDefinition
from OCP.StepRepr import (
    StepRepr_ProductDefinitionShape,
    StepRepr_ShapeRepresentationRelationship,
)
from OCP.StepShape import (
    StepShape_AdvancedBrepShapeRepresentation,
    StepShape_ManifoldSolidBrep,
    StepShape_ManifoldSurfaceShapeRepresentation,
    StepShape_ShapeDefinitionRepresentation,
    StepShape_ShellBasedSurfaceModel,
)

from OCP.OCP import __version__ as OCP_VERSION

try:
    from .ocp_utils import ShapeKey, get_label_name
except ImportError:  # standalone use outside the package (test harnesses)
    from ocp_utils import ShapeKey, get_label_name

print("--> STEPper NEXT OpenCASCADE (OCP) version:", OCP_VERSION)

# Native C++ acceleration for mesh extraction (optional).  The module links
# its own plain-named OCCT subset shipped in native_libs/ (the vendored OCP
# wheel's DLLs are name-mangled and not linkable).
if os.name == "nt":
    _native_libs_dir = os.path.join(file_dirname, "native_libs")
    if os.path.isdir(_native_libs_dir):
        os.add_dll_directory(_native_libs_dir)
try:
    from . import stepper_native
    # The OCP-era native module exposes mesh_and_extract (BinTools serialize
    # handoff).  An old pythonocc-era binary lacks it and cannot be used.
    _HAS_NATIVE = hasattr(stepper_native, "mesh_and_extract")
    _NATIVE_ABI = getattr(stepper_native, "ABI_VERSION", 1)
    if _HAS_NATIVE:
        print("--> STEPper NEXT native acceleration: ENABLED")
    else:
        print("--> STEPper NEXT native acceleration: incompatible binary (using Python fallback)")
except ImportError:
    _HAS_NATIVE = False
    _NATIVE_ABI = 0
    print("--> STEPper NEXT native acceleration: not available (using Python fallback)")


def _mesh_shape(shp, lin_def, ang_def, relative=False):
    """Run BRepMesh on a shape.

    relative=True switches OCCT to relative deflection (deflection scales
    with each edge's size) and enables parallel meshing; the absolute path
    keeps the historical single-threaded call for deterministic parity.
    """
    if relative:
        p = IMeshTools_Parameters()
        p.Deflection = lin_def
        p.Angle = ang_def
        p.Relative = True
        p.InParallel = True
        p.MinSize = Precision.Confusion_s()
        BRepMesh_IncrementalMesh(shp, p)
    else:
        BRepMesh_IncrementalMesh(shp, lin_def, False, ang_def, False).Perform()


def _limit_openmp_threads(n):
    """Set the maximum number of OpenMP threads at runtime.

    Loads the platform-specific OpenMP runtime via ctypes and calls
    omp_set_num_threads().  Pass 0 to restore the default (all cores).
    Falls back to setting OMP_NUM_THREADS env var if ctypes fails.
    """
    import ctypes
    import platform as _platform

    _omp_lib = None
    _sys = _platform.system()
    try:
        if _sys == "Windows":
            _omp_lib = ctypes.CDLL("vcomp140.dll")
        elif _sys == "Darwin":
            _omp_lib = ctypes.CDLL("libomp.dylib")
        elif _sys == "Linux":
            _omp_lib = ctypes.CDLL("libgomp.so.1")
    except OSError:
        pass

    if _omp_lib is not None:
        try:
            _omp_lib.omp_set_num_threads(n if n > 0 else (os.cpu_count() or 4))
            return
        except Exception:
            pass

    # Fallback: environment variable
    if n > 0:
        os.environ["OMP_NUM_THREADS"] = str(n)
    else:
        os.environ.pop("OMP_NUM_THREADS", None)


class NativeMeshData:
    """Lightweight mesh data holder returned by native extraction path.

    Holds flat numpy arrays instead of TriData/TriMesh Python objects.
    Compatible with the from_pydata + foreach_set Blender creation path.
    """
    __slots__ = ('verts', 'faces', 'norms', 'uvs',
                 'loop_norms', 'loop_uvs',
                 'tri_colors', 'tri_batches', 'tri_mat_names', 'matrix')

    def __init__(self, verts, faces, norms, uvs,
                 tri_colors, tri_batches, tri_mat_names, matrix):
        self.verts = verts              # (V, 3) float32
        self.faces = faces              # (T, 3) int32
        self.norms = norms              # (V, 3) float32 per-vertex
        self.uvs = uvs                  # (V, 2) float32 per-vertex
        self.loop_norms = None          # (T*3, 3) float32 per-corner, set before fuse_verts
        self.loop_uvs = None            # (T*3, 2) float32 per-corner, set before fuse_verts
        self.tri_colors = tri_colors    # (T, 3) float32, -1 = no color
        self.tri_batches = tri_batches  # (T,) int32
        self.tri_mat_names = tri_mat_names  # list[str|None] len=T
        self.matrix = matrix            # (4, 4) float32

    def fuse_verts(self):
        """Merge duplicate vertices using numpy.

        IMPORTANT: Snapshots per-corner normals into loop_norms BEFORE
        merging, because different OCC faces can have different normals at
        shared vertex positions (hard/sharp edges).
        """
        # Snapshot per-corner normals before any remapping
        # norms is (V, 3) per-vertex, faces is (T, 3) indices into norms
        # Before fusing, each OCC face has its own vertex range, so
        # norms[faces.ravel()] gives correct per-face-corner normals.
        self.loop_norms = self.norms[self.faces.ravel()].reshape(-1, 3).copy()
        # Same snapshot for per-corner UVs: shared vertex positions can carry
        # different UVs per OCC face, so they must be captured pre-fuse too.
        self.loop_uvs = self.uvs[self.faces.ravel()].reshape(-1, 2).copy()

        # Round to avoid floating-point near-misses
        verts_rounded = np.round(self.verts, decimals=6)
        _, first_idx, inverse = np.unique(
            verts_rounded, axis=0, return_index=True, return_inverse=True)
        if len(_) == len(self.verts):
            return  # no duplicates
        # Remap face indices
        self.faces = inverse[self.faces]
        self.verts = self.verts[first_idx]
        self.uvs = self.uvs[first_idx]
        # Keep per-vertex normals aligned with the fused vertex array (the
        # loop_norms snapshot above is what shading actually uses, but a
        # stale-length norms array would silently corrupt any fallback path)
        self.norms = self.norms[first_idx]

    def filter_zero_area(self):
        """Remove triangles where two or more vertices coincide."""
        f = self.faces
        v = self.verts
        p0, p1, p2 = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
        same01 = np.all(p0 == p1, axis=1)
        same12 = np.all(p1 == p2, axis=1)
        same20 = np.all(p2 == p0, axis=1)
        keep = ~(same01 | same12 | same20)
        if keep.all():
            return
        self.faces = self.faces[keep]
        self.tri_colors = self.tri_colors[keep]
        self.tri_batches = self.tri_batches[keep]
        self.tri_mat_names = [self.tri_mat_names[i]
                              for i in range(len(keep)) if keep[i]]
        if self.loop_norms is not None:
            # keep mask is per-tri; loop_norms is per-corner (3 per tri)
            keep3 = np.repeat(keep, 3)
            self.loop_norms = self.loop_norms[keep3]
            if self.loop_uvs is not None:
                self.loop_uvs = self.loop_uvs[keep3]

    def filter_same_face(self):
        """Remove duplicate triangles (same vertex set)."""
        sorted_faces = np.sort(self.faces, axis=1)
        _, unique_idx = np.unique(sorted_faces, axis=0, return_index=True)
        if len(unique_idx) == len(self.faces):
            return
        unique_idx.sort()  # preserve order
        self.faces = self.faces[unique_idx]
        self.tri_colors = self.tri_colors[unique_idx]
        self.tri_batches = self.tri_batches[unique_idx]
        self.tri_mat_names = [self.tri_mat_names[i] for i in unique_idx]
        if self.loop_norms is not None:
            # unique_idx is per-tri; expand to per-corner
            loop_idx = np.repeat(unique_idx * 3, 3) + np.tile([0, 1, 2], len(unique_idx))
            self.loop_norms = self.loop_norms[loop_idx]
            if self.loop_uvs is not None:
                self.loop_uvs = self.loop_uvs[loop_idx]

    def fill_empty_color(self):
        """Replace -1 sentinel colors with pink."""
        mask = self.tri_colors[:, 0] < 0
        if mask.any():
            self.tri_colors[mask] = [1.0, 0.0, 1.0]

    def get_loop_colors(self):
        """Expand per-face colors to per-loop (repeat each 3x)."""
        return np.repeat(self.tri_colors, 3, axis=0)

    def get_loop_norms(self):
        """Return per-loop (per-face-corner) normals.

        Uses loop_norms if available (snapshot taken before fuse_verts).
        Falls back to per-vertex normals expanded via face indices.
        """
        if self.loop_norms is not None:
            return self.loop_norms
        return self.norms[self.faces.ravel()]

    def get_loop_uvs(self):
        """Return per-loop (per-face-corner) UVs (see get_loop_norms)."""
        if self.loop_uvs is not None:
            return self.loop_uvs
        return self.uvs[self.faces.ravel()]

    def get_loop_mat_names(self):
        """Expand per-face mat names to per-loop."""
        result = []
        for mn in self.tri_mat_names:
            result.extend([mn, mn, mn])
        return result


def b_colorname(col):
    return Quantity_Color.StringName_s(
        Quantity_Color.Name_s(col.Red(), col.Green(), col.Blue()))


def b_XYZ(v):
    x = v.XYZ()
    return (x.X(), x.Y(), x.Z())


def b_RGB(c):
    return (c.Red(), c.Green(), c.Blue())


def nurbs_parse(current_face):
    """Get NURBS points for a TopAbs_FACE"""

    nurbs_converter = BRepBuilderAPI_NurbsConvert(current_face)
    nurbs_converter.Perform(current_face)
    result_shape = nurbs_converter.Shape()
    brep_face = BRep_Tool.Surface_s(TopoDS.Face_s(result_shape))
    occ_face = GeomConvert.SurfaceToBSplineSurface_s(brep_face)

    # extract the Control Points of each face
    n_poles_u = occ_face.NbUPoles()
    n_poles_v = occ_face.NbVPoles()

    # cycle over the poles to get their coordinates
    points = []
    for pole_u_direction in range(n_poles_u):
        points.append([])
        for pole_v_direction in range(n_poles_v):
            pos = (pole_u_direction + 1, pole_v_direction + 1)
            coords = occ_face.Pole(*pos)
            np_coords = np.array((coords.X(), coords.Y(), coords.Z()))
            weight = occ_face.Weight(*pos)
            pt = nurbs.NurbsPoint((*np_coords, weight))
            points[-1].append(pt)

    # Get surface data (closed, periodic, degree)
    assert len(points) > 1
    assert len(points[0]) > 1
    nbd = nurbs.NurbsData(points)

    nbd.u_closed = occ_face.IsUClosed()
    nbd.v_closed = occ_face.IsVClosed()
    nbd.u_periodic = occ_face.IsUPeriodic()
    nbd.v_periodic = occ_face.IsVPeriodic()
    nbd.u_degree = occ_face.UDegree()
    nbd.v_degree = occ_face.VDegree()

    return nbd


def force_ascii(i_file):
    from pathlib import Path

    print("Attempting to format STEP file as ASCII 7-bit")
    p = Path(i_file)
    print(p.stat().st_size // 1024, "kB")
    import tempfile

    with tempfile.NamedTemporaryFile("w", encoding="ASCII") as fo:
        temp_name = fo.name
        print(temp_name)
        with p.open("rb") as f:
            while il := f.readline():
                fo.write(il.decode("ASCII"))
    print("done ASCII conversion.")
    return temp_name


# TODO: proper parametrization
def equalize_2d_points(pts):
    """Equalize aspect ratio of 2D point dimensions"""
    x_a, x_b = 1.0, 0.0
    y_a, y_b = 1.0, 0.0

    for i, uv in enumerate(pts):
        if uv[0] < x_a:
            x_a = uv[0]
        if uv[0] > x_b:
            x_b = uv[0]
        if uv[1] < y_a:
            y_a = uv[1]
        if uv[1] > y_b:
            y_b = uv[1]

    rx = abs(x_b - x_a)
    ry = abs(y_b - y_a)
    if rx != 0.0 and ry != 0.0:
        ratio = rx / ry
    else:
        ratio = 1.0

    ratio1 = 1 / ratio
    for i, uv in enumerate(pts):
        pts[i] = (pts[i][0] * ratio1, pts[i][1])

    return pts


@dataclass
class ShapeTreeNode:
    """
    A node for the OpenCASCADE CAD data ShapeTree
    """

    parent: int
    index: int
    tag: int
    name: str
    children: list[int] = field(default_factory=list)
    local_transform: np.ndarray = field(default_factory=np.eye(4, dtype=np.float32))
    global_transform: np.ndarray = field(default_factory=np.eye(4, dtype=np.float32))
    shape: TopoDS_Shape = None
    color_override: tuple = None  # (r, g, b) instance color from component label
    material: dict = None  # engineering material metadata (AP214/AP242)

    def __init__(self, parent, index, tag, name):
        self.parent = parent
        self.index = index
        self.tag = tag
        self.name = name
        self.children = []
        self.local_transform = np.eye(4, dtype=np.float32)
        self.global_transform = np.eye(4, dtype=np.float32)
        self.shape = None
        self.color_override = None
        self.material = None

    def get_values(self):
        """
        parent, index, tag, name
        """
        return (
            self.parent,
            self.index,
            self.tag,
            self.name,
            self.shape,
            self.local_transform,
            self.global_transform,
        )

    def set_shape(self, shape):
        if shape:
            if not isinstance(shape, TopoDS_Shape):
                raise ValueError("Input shape is not OpenCASCADE TopoDS_Shape")
            self.shape = shape
        else:
            self.shape = None


class ShapeTree:
    """
    Intermediary data structure to partially abstract OpenCASCADE away from the rest of the program
    """

    def __init__(self):
        self.nodes = []

        # Root node has special values
        self.nodes.append(ShapeTreeNode(-1, 0, -1, "root"))

    def get_root_id(self):
        return 0

    def get_max_id(self):
        return len(self.nodes) - 1

    def add(self, parent, label) -> ShapeTreeNode:
        loc = len(self.nodes)
        name = get_label_name(label)
        node = ShapeTreeNode(parent, loc, label.Tag(), name)
        self.nodes[parent].children.append(loc)
        self.nodes.append(node)
        return self.nodes[-1]

    def get_shapes(self):
        # return {i.shape: i.index for i in self.nodes if i.shape}
        return [(i.shape, i.index) for i in self.nodes]

    def print_transforms(self):
        for i in self.nodes:
            print(i.local_transform)


class ReadSTEP:
    # Per-import UV mode, set by load_step before tessellation:
    # None  -> normalize each face's UVs to the 0-1 square (default)
    # float -> scale UVs so 1 UV unit == 1 scene unit (value = scene units
    #          per file unit); texel density then matches across parts
    uv_world_scale = None

    def __init__(self, filename, skip_name_prefixes=()):
        # Construction-geometry filter: case-insensitive node-name prefixes
        # to skip during the assembly walk (e.g. "Sketches", "Axes").
        self.skip_name_prefixes = tuple(
            p.strip().lower() for p in skip_name_prefixes if p.strip())
        self._filter_key = frozenset(self.skip_name_prefixes)
        self.filtered_labels = []  # [(label, name)] of skipped subtrees
        self.read_file(filename)

    def _is_filtered(self, name):
        if not self.skip_name_prefixes:
            return False
        lname = name.lower()
        return any(lname.startswith(p) for p in self.skip_name_prefixes)

    def query_color(self, label, overwrite=False):
        # default color = pink
        c = Quantity_Color(1.0, 0.0, 1.0, Quantity_TOC_RGB)
        colorset = False
        colortype = None

        # Query each color type into its own object: GetColor_s mutates the
        # passed color, so sharing one would make the last successful query
        # win (a label with both surface and curve colors imported with the
        # CURVE color applied to its faces).
        cg = Quantity_Color(1.0, 0.0, 1.0, Quantity_TOC_RGB)
        cs = Quantity_Color(1.0, 0.0, 1.0, Quantity_TOC_RGB)
        cc = Quantity_Color(1.0, 0.0, 1.0, Quantity_TOC_RGB)
        c_gen = XCAFDoc_ColorTool.GetColor_s(label, XCAFDoc_ColorGen, cg)
        c_surf = XCAFDoc_ColorTool.GetColor_s(label, XCAFDoc_ColorSurf, cs)
        c_curv = XCAFDoc_ColorTool.GetColor_s(label, XCAFDoc_ColorCurv, cc)
        if c_gen or c_surf or c_curv:
            colorset = True
            # Meshes want the surface color first, then the generic color;
            # the curve color only when nothing else is defined.
            if c_surf:
                c, colortype = cs, 2
            elif c_gen:
                c, colortype = cg, 1
            else:
                c, colortype = cc, 3

        return c, colortype, colorset

    def query_material(self, label):
        """Engineering material metadata (AP214/AP242) linked to a label.

        The link is a TDataStd_TreeNode with the XCAF MaterialRef GUID
        pointing at a material label. Read via the XCAFDoc_Material
        attribute directly, because the MaterialTool.GetMaterial_s out-parameters
        are handle references that OCP's bindings cannot write back.
        Returns {"name", "description", "density"} or None.
        """
        # OCP wart: FindAttribute(guid, TDataStd_TreeNode) CRASHES (access
        # violation in TKLCAF) when the attribute is absent, so always check
        # IsAttribute first and only fetch on a confirmed hit.
        guid = XCAFDoc.MaterialRefGUID_s()
        if not label.IsAttribute(guid):
            return None
        node = TDataStd_TreeNode()
        if not label.FindAttribute(guid, node):
            return None
        father = node.Father()
        if father is None:
            return None
        mlabel = father.Label()
        if not mlabel.IsAttribute(XCAFDoc_Material.GetID_s()):
            return None
        mat = XCAFDoc_Material()
        if not mlabel.FindAttribute(XCAFDoc_Material.GetID_s(), mat):
            return None
        name = mat.GetName()
        desc = mat.GetDescription()
        info = {
            "name": name.ToCString() if name is not None else "",
            "description": desc.ToCString() if desc is not None else "",
            "density": float(mat.GetDensity()),
        }
        return info if info["name"] else None

    def print_all_colors(self):
        tcol = Quantity_Color(1.0, 0.0, 1.0, Quantity_TOC_RGB)
        clabs = TDF_LabelSequence()
        self.color_tool.GetColors(clabs)
        for i in range(clabs.Length()):
            res = XCAFDoc_ColorTool.GetColor_s(clabs.Value(i + 1), tcol)
            if res:
                print(b_colorname(tcol))

    def label_matrix(self, lab):
        trsf = XCAFDoc_ShapeTool.GetLocation_s(lab).Transformation()
        matrix = np.eye(4, dtype=np.float32)
        for row in range(1, 4):
            for col in range(1, 5):
                matrix[row - 1, col - 1] = trsf.Value(row, col)
        # print(matrix)
        return matrix

    def explore_partial(self, shp, te_type):
        c_set = set([])
        ex = TopExp_Explorer(shp, te_type)
        # Todo: use label->tag
        while ex.More():
            c = ShapeKey(ex.Current())
            if c not in c_set:
                c_set.add(c)
            ex.Next()
        return len(c_set)

    def explore_shape(self, shp):
        return (
            self.explore_partial(shp, TopAbs_COMPOUND),
            self.explore_partial(shp, TopAbs_SOLID),
            self.explore_partial(shp, TopAbs_SHELL),
            self.explore_partial(shp, TopAbs_FACE),
            self.explore_partial(shp, TopAbs_WIRE),
            self.explore_partial(shp, TopAbs_EDGE),
            self.explore_partial(shp, TopAbs_VERTEX),
        )

    def shape_info(self, shp):
        st = XCAFDoc_ShapeTool
        lab = self.shape_label[ShapeKey(shp)]
        vals = (
            st.IsAssembly_s(lab),
            st.IsFree_s(lab),
            st.IsShape_s(lab),
            st.IsCompound_s(lab),
            st.IsComponent_s(lab),
            st.IsSimpleShape_s(lab),
            shp.Locked(),
        )

        lookup = ["A", "F", "S", "C", "T", "s", "L"]
        res = "".join([lookup[i] for i, v in enumerate(vals) if v])

        # res += f", C:{shp.NbChildren()}"

        res += ", C:{} So:{} Sh:{} F:{} Wi:{} E:{} V:{}".format(*self.explore_shape(shp))

        return " " + res + " "

    def transfer_with_units(self, filename):
        print("Init transfer with units")

        # Init new doc and reader
        doc = TDocStd_Document(TCollection_ExtendedString("STEP"))
        step_reader = STEPCAFControl_Reader()
        step_reader.SetColorMode(True)
        step_reader.SetNameMode(True)
        step_reader.SetMatMode(True)
        step_reader.SetLayerMode(True)

        print("DataExchange: Reading STEP")

        # Single ReadFile: the XCAF reader wraps a STEPControl_Reader
        # that we can use for unit detection via ChangeReader().
        status = step_reader.ReadFile(self.filename)
        if status != IFSelect_RetDone:
            raise AssertionError("Error: can't read file. File possibly damaged.")

        print("STEP read into memory")

        # Check the STEP data model for unresolved references.
        # ChangeReader() stays outside the try: the units query below needs
        # basic_reader, so a swallowed failure here must not turn into a
        # NameError there.
        basic_reader = step_reader.ChangeReader()
        has_data_failures = False
        try:
            step_model = basic_reader.StepModel()
            if step_model is not None:
                global_check = step_model.GlobalCheck(True)
                if global_check.HasFailed():
                    has_data_failures = True
                    nb_fails = global_check.NbFails()
                    print(f"WARNING: STEP file has {nb_fails} data model failure(s).")
                    print("This may indicate unresolved references.")
        except Exception as e:
            print(f"STEP model pre-check failed: {e}")

        # Read units from the same reader (no second ReadFile needed)
        ulen_names = TColStd_SequenceOfAsciiString()
        uang_names = TColStd_SequenceOfAsciiString()
        usld_names = TColStd_SequenceOfAsciiString()
        basic_reader.FileUnits(ulen_names, uang_names, usld_names)

        # default is MM
        scale = 0.001

        if ulen_names.Length() > 0:
            scaleval = ulen_names.Value(1).ToCString().lower()

            # INCH, MM, FT, MI, M, KM, MIL, CM
            # UM, UIN ??

            scales = {
                "millimeter": 0.001,
                "millimetre": 0.001,
                "centimeter": 0.01,
                "centimetre": 0.01,
                "kilometer": 1000.0,
                "kilometre": 1000.0,
                "meter": 1.0,
                "metre": 1.0,
                "inch": 0.0254,
                "foot": 0.3048,
                "mile": 1609.34,
                "mil": 0.0254 * 0.001,
            }

            if scaleval in scales:
                scale = scales[scaleval]
            else:
                print("ERROR: Undefined scale:", scaleval)

            print("Scale from file (meters per unit):", scaleval, scale)

        else:
            print("Using default scale (millimeters)")

        self.scale = scale

        print("DataExchange: Transferring")

        # Try transfer with default surface curve mode first (preserves more
        # geometry).  Only fall back to mode 0 (skip surface curves) when the
        # transfer crashes; this can happen on files with unresolved refs.
        # See: https://dev.opencascade.org/content/loading-step-file-crashes-edgeloop
        transfer_ok = False
        if has_data_failures:
            try:
                transfer_result = step_reader.Transfer(doc)
                transfer_ok = bool(transfer_result)
                if transfer_ok:
                    print("DataExchange: Transfer done")
                else:
                    print("DataExchange: Transfer returned failure, retrying in safe mode...")
            except Exception as e:
                print(f"DataExchange: Transfer failed ({e}), retrying in safe mode...")

            if not transfer_ok:
                # Retry with mode 0: discard surface curves from file
                Interface_Static.SetIVal_s("read.surfacecurve.mode", 0)
                doc = TDocStd_Document(TCollection_ExtendedString("STEP"))
                step_reader = STEPCAFControl_Reader()
                step_reader.SetColorMode(True)
                step_reader.SetNameMode(True)
                step_reader.SetMatMode(True)
                step_reader.SetLayerMode(True)
                step_reader.ReadFile(self.filename)
                transfer_result = step_reader.Transfer(doc)
                if not transfer_result:
                    print("DataExchange: Safe mode transfer also FAILED.")
                else:
                    print("DataExchange: Transfer done (safe mode)")
                # Reset to default for any subsequent imports
                Interface_Static.SetIVal_s("read.surfacecurve.mode", 1)
        else:
            transfer_result = step_reader.Transfer(doc)
            if not transfer_result:
                print("Dataexchange transfer FAILED.")
            else:
                print("DataExchange: Transfer done")

        self.doc = doc
        self._xcaf_reader = step_reader

    def transfer_simple(self, fname):
        # see stepanalyzer.py for license details
        print("Init simple transfer")

        # Create the application, empty document and shape_tool
        doc = TDocStd_Document(TCollection_ExtendedString("STEP"))
        app = XCAFApp_Application.GetApplication_s()
        app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)

        # Read file and return populated doc
        step_reader = STEPCAFControl_Reader()
        step_reader.SetColorMode(True)
        step_reader.SetLayerMode(True)
        step_reader.SetNameMode(True)
        step_reader.SetMatMode(True)
        status = step_reader.ReadFile(fname)
        if status == IFSelect_RetDone:
            step_reader.Transfer(doc)
        self.scale = 0.001

        self.doc = doc

    def init_reader(self, filename):
        if not os.path.isfile(filename):
            raise FileNotFoundError("%s not found." % filename)

        # self.filename = force_ascii(filename)
        self.filename = filename

        self.transfer_with_units(self.filename)
        # self.transfer_simple(self.filename)

        self.shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(self.doc.Main())
        self.color_tool = XCAFDoc_DocumentTool.ColorTool_s(self.doc.Main())

        # material_tool = XCAFDoc_DocumentTool_MaterialTool(doc.Main())
        # layer_tool = XCAFDoc_DocumentTool_LayerTool(doc.Main())

        # use OrderedDict and make sure the order is maintained through the entire pipeline

        self.shape_label = {}
        self.sub_shapes = OrderedDict()

        self.face_colors = {}
        self.face_color_priority = {}
        self.tag_info = {}
        self.skipped_shapes = set([])
        self.import_problems = {"Triangulation": 0, "Undefined normals": 0, "Empty shape": 0}
        self.failed_parts = []  # List of names for parts that produced no geometry
        self.recovered_parts = []  # List of names for parts recovered via per-face transfer
        self._lock = threading.Lock()  # Protects import_problems, skipped_shapes, recovered_parts
        self._pre_tessellated = False
        self._recovery_compounds = None  # Lazy-built recovery compound
        self._recovery_keepalive = None  # Keeps entity wrappers (and ids) stable

    def read_file(self, filename):
        """Returns list of tuples (topods_shape, label, color)
        Use OCAF.
        """

        self.init_reader(filename)

        # output_shapes = {}
        # outliers = defaultdict(set)

        def _cprio(lab, shape):
            "Get label color"
            tc, ctype, ok = self.query_color(lab)
            self.face_colors[ShapeKey(shape)] = tc if ok else None
            if ok:
                return ctype
            else:
                return 0

        def _get_sub_shapes(lab, level, tree, leaf_id):

            master_leaf = tree.nodes[leaf_id]
            if XCAFDoc_ShapeTool.IsAssembly_s(lab):
                # Read contained shapes
                l_c = TDF_LabelSequence()
                XCAFDoc_ShapeTool.GetComponents_s(lab, l_c)
                for i in range(l_c.Length()):
                    label = l_c.Value(i + 1)
                    if XCAFDoc_ShapeTool.IsReference_s(label):
                        label_reference = TDF_Label()
                        XCAFDoc_ShapeTool.GetReferredShape_s(label, label_reference)

                        ref_name = get_label_name(label_reference) or get_label_name(label)
                        if self._is_filtered(ref_name):
                            self.filtered_labels.append((label_reference, ref_name))
                            continue

                        label_transform = self.label_matrix(label)
                        node = tree.add(master_leaf.index, label_reference)
                        new_leaf = tree.nodes[node.index]
                        new_leaf.local_transform = label_transform
                        new_leaf.global_transform = master_leaf.global_transform @ label_transform

                        # Instance color override: a color attached to the
                        # component reference label applies to this occurrence
                        # (unless faces/sub-shapes carry their own colors).
                        oc, _, o_ok = self.query_color(label)
                        if o_ok:
                            new_leaf.color_override = (oc.Red(), oc.Green(), oc.Blue())

                        # Engineering material on the component reference
                        # (rare; usually it sits on the referred product,
                        # picked up in the simple-shape branch below)
                        new_leaf.material = self.query_material(label)

                        _get_sub_shapes(label_reference, level + 1, tree, node.index)
                    else:
                        # TODO: process rest of the data
                        pass

            elif XCAFDoc_ShapeTool.IsSimpleShape_s(lab):
                # TODO: self.shape_label stops being unique when shapes aren't transformed
                shape = XCAFDoc_ShapeTool.GetShape_s(lab)
                master_leaf.set_shape(shape)
                # Engineering material from the product label (before the
                # shared-shape early return so every occurrence carries it)
                if master_leaf.material is None:
                    master_leaf.material = self.query_material(lab)
                key = ShapeKey(shape)
                if key in self.shape_label:
                    # Shape already in
                    return

                self.shape_label[key] = lab

                self.face_color_priority[key] = _cprio(lab, shape)

                l_subss = TDF_LabelSequence()
                XCAFDoc_ShapeTool.GetSubShapes_s(lab, l_subss)
                self.sub_shapes[key] = []
                for i in range(l_subss.Length()):
                    lab_subs = l_subss.Value(i + 1)
                    shape_sub = XCAFDoc_ShapeTool.GetShape_s(lab_subs)
                    self.shape_label[ShapeKey(shape_sub)] = lab_subs
                    self.sub_shapes[key].append(shape_sub)
                    self.face_color_priority[ShapeKey(shape_sub)] = _cprio(lab_subs, shape_sub)
                # Color priority is the same as CAD assistant material tree display
            else:
                print("DataExchange error: Item is neither assembly or a simple shape")

        def _get_shapes():
            # self.shape_tool.UpdateAssemblies()

            labels = TDF_LabelSequence()
            self.shape_tool.GetFreeShapes(labels)

            tree = ShapeTree()
            for i in range(labels.Length()):
                print("DataExchange: Reading shape ({}/{})".format(i + 1, labels.Length()))

                root_item = labels.Value(i + 1)
                node = tree.add(tree.get_root_id(), root_item)
                _get_sub_shapes(root_item, 0, tree, node.index)

            return tree

        tree = _get_shapes()
        self.tree = tree

    def pre_tessellate_all(self, lin_def=0.8, ang_def=0.5):
        """Pre-tessellate all unique shapes using thread pool for parallelism.

        OpenCASCADE SWIG bindings release the GIL during C++ calls, so
        threading provides real speedup for CPU-bound tessellation.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        shapes = list(self.sub_shapes.keys())
        if not shapes:
            self._pre_tessellated = True
            return

        def _tessellate_group(key):
            iter_shapes = [key.shape] + self.sub_shapes.get(key, [])
            for shp in iter_shapes:
                BRepTools.Clean_s(shp)
                ex = TopExp_Explorer(shp, TopAbs_FACE)
                if ex.More():
                    _mesh_shape(shp, lin_def, ang_def)

        n_workers = min(len(shapes), os.cpu_count() or 4)

        if n_workers > 1 and len(shapes) > 1:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(_tessellate_group, s): s for s in shapes}
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        print(f"Warning: pre-tessellation failed for a shape: {e}")
        else:
            for s in shapes:
                _tessellate_group(s)

        self._pre_tessellated = True

    def triangulate_face(self, face, tform):
        location = TopLoc_Location()
        facing = BRep_Tool.Triangulation_s(face, location)
        if facing is None:
            with self._lock:
                self.import_problems["Triangulation"] += 1
            return None

        reversed_face = (face.Orientation() == TopAbs_REVERSED)
        not_forward = (face.Orientation() != TopAbs_FORWARD)
        itform = tform.Inverted()

        d_nbnodes = facing.NbNodes()
        d_nbtriangles = facing.NbTriangles()

        # Surface-based normal computation (analytic, seamless across faces)
        surface = BRepAdaptor_Surface(face)
        prop = BRepLProp_SLProps(surface, 2, gp.Resolution_s())

        has_uvs = facing.HasUVNodes()

        # Calculate UV bounds for edge-avoidance shrink
        Umin = Umax = Vmin = Vmax = 0.0
        if has_uvs:
            for t in range(1, d_nbnodes + 1):
                uv = facing.UVNode(t)
                u, v = uv.X(), uv.Y()
                if t == 1:
                    Umin, Umax, Vmin, Vmax = u, u, v, v
                else:
                    if u < Umin: Umin = u
                    if u > Umax: Umax = u
                    if v < Vmin: Vmin = v
                    if v > Vmax: Vmax = v
        Ucenter = (Umin + Umax) * 0.5
        Vcenter = (Vmin + Vmax) * 0.5

        verts = [None] * d_nbnodes
        norms = [None] * d_nbnodes
        uvs = [None] * d_nbnodes
        undef_normals = False

        for t in range(1, d_nbnodes + 1):
            pt = facing.Node(t)
            verts[t - 1] = b_XYZ(pt)

            if has_uvs:
                uv = facing.UVNode(t)
                u, v = uv.X(), uv.Y()
                uvs[t - 1] = (u, v)
            else:
                u, v = 0.0, 0.0
                uvs[t - 1] = (0.0, 0.0)

            # Shrink UV 0.1% toward center to avoid undefined normals at edges
            prop.SetParameters((u - Ucenter) * 0.999 + Ucenter,
                               (v - Vcenter) * 0.999 + Vcenter)
            if prop.IsNormalDefined():
                normal = prop.Normal().Transformed(itform)
                nn = np.array(b_XYZ(normal), dtype=np.float32)
                if reversed_face:
                    nn = -nn
            else:
                nn = np.array((0.0, 0.0, 1.0), dtype=np.float32)
                undef_normals = True
            norms[t - 1] = nn

        # Normalize UVs, preserving aspect ratio (matches the native path's
        # per-face normalization, including real-world mode, see
        # _recompute_face_normals)
        if has_uvs:
            span = max(Umax - Umin, Vmax - Vmin)
            if span > 1e-12:
                if self.uv_world_scale is not None:
                    va = np.asarray(verts, dtype=np.float32)
                    ext = float((va.max(axis=0) - va.min(axis=0)).max())
                    s = (ext * self.uv_world_scale) / span
                else:
                    s = 1.0 / span
            else:
                s = 1.0
            uvs = [((u - Umin) * s, (v - Vmin) * s) for (u, v) in uvs]

        # Read all triangles
        tris = [None] * d_nbtriangles
        for t in range(1, d_nbtriangles + 1):
            T1, T2, T3 = facing.Triangle(t).Get()
            if not_forward:
                T1, T2 = T2, T1
            tris[t - 1] = (T1 - 1, T2 - 1, T3 - 1)

        if undef_normals:
            with self._lock:
                self.import_problems["Undefined normals"] += 1

        tri_data = []
        for t in tris:
            # Degenerate triangles (repeated node index: collapsed seams,
            # corrupt geometry) would fail TriData's assertions and discard
            # the whole face; skip just the bad triangle instead, matching
            # the native path (which drops them in filter_zero_area).
            if t[0] == t[1] or t[1] == t[2] or t[2] == t[0]:
                continue
            tri_data.append(trimesh.TriData(
                t, [norms[i] for i in t], [uvs[i] for i in t],
                None, None, None, None))

        return trimesh.TriMesh(verts=verts, tris=tri_data)

    def _build_recovery_compound(self):
        """Build per-shape recovery compounds for parts that failed to transfer.

        For each empty shape, we need to recover exactly the faces belonging
        to that shape, not faces from other parts.  The approach:

        1. Index all AdvancedFace entities by hash for hierarchy matching.
        2. Build a ProductDefinition → Representation mapping by navigating:
           ShapeDefinitionRepresentation → ProductDefinitionShape → PD, and
           ShapeRepresentationRelationship to resolve generic → specific reprs.
        3. For each failed representation (not in the TransientProcess),
           traverse its hierarchy to collect its AdvancedFace entity indices.
        4. Transfer each repr's faces into a separate compound.
        5. Map each compound by ProductDefinition wrapper id so that
           _get_recovery_compound(shape) can match via EntityFromShapeResult.

        Results are cached in self._recovery_compounds (dict: id(PD) → compound).
        """
        if self._recovery_compounds is not None:
            return  # already built

        self._recovery_compounds = {}
        if getattr(self, "_xcaf_reader", None) is None:
            return  # STEP-only feature (IGES/BREP readers have no STEP model)
        try:
            basic_reader = self._xcaf_reader.ChangeReader()
            tr = basic_reader.WS().TransferReader()
            tp = tr.TransientProcess()
            xcaf_model = basic_reader.StepModel()
            ne = xcaf_model.NbEntities()

            # Single pass over the model.  The entity wrapper list doubles as
            # a keep-alive: under pybind11 the same underlying transient
            # always resolves to the same live wrapper, so id(ent) is a
            # stable key while this list exists.  Stored on self because
            # _get_recovery_compound() matches EntityFromShapeResult wrappers
            # against these ids later.
            ents = [xcaf_model.Value(i) for i in range(1, ne + 1)]
            self._recovery_keepalive = ents
            type_names = [
                ent.DynamicType().Name() if ent is not None else ""
                for ent in ents
            ]

            # Index all AdvancedFace entities (id → 1-based model index)
            af_id_to_idx = {}
            for i, tname in enumerate(type_names):
                if tname == 'StepShape_AdvancedFace':
                    af_id_to_idx[id(ents[i])] = i + 1

            if not af_id_to_idx:
                return

            # Which entities actually transferred?  OCP's per-entity lookup
            # methods (MapIndex/IsBound/Find) are broken in 7.9.3.1: they
            # always miss and are ~250µs per call, so enumerate the mapped
            # entities once and compare wrapper identity instead.
            mapped = [tp.Mapped(i) for i in range(1, tp.NbMapped() + 1)]
            mapped_ids = {id(m) for m in mapped}

            def _collect_faces_from_shell(shell, face_set):
                """Collect AdvancedFace model indices from a ConnectedFaceSet."""
                for m in range(1, shell.NbCfsFaces() + 1):
                    idx = af_id_to_idx.get(id(shell.CfsFacesValue(m)))
                    if idx is not None:
                        face_set.add(idx)

            # --- Build ProductDefinition → Representation mapping ---

            # Resolve generic ShapeRepresentation → specific MSSR/ABSR
            # via ShapeRepresentationRelationship
            generic_to_specific = {}
            for ent, tname in zip(ents, type_names):
                if tname != 'StepRepr_ShapeRepresentationRelationship':
                    continue
                rep1, rep2 = ent.Rep1(), ent.Rep2()
                t1 = rep1.DynamicType().Name() if rep1 else ""
                t2 = rep2.DynamicType().Name() if rep2 else ""
                if 'ManifoldSurface' in t2 or 'AdvancedBrep' in t2:
                    generic_to_specific[id(rep1)] = rep2
                elif 'ManifoldSurface' in t1 or 'AdvancedBrep' in t1:
                    generic_to_specific[id(rep2)] = rep1

            # SDR → PDS → PD, giving us PD entity id → repr entity
            pd_id_to_repr_ent = {}
            for ent, tname in zip(ents, type_names):
                if tname != 'StepShape_ShapeDefinitionRepresentation':
                    continue
                used_repr = ent.UsedRepresentation()
                if used_repr is None:
                    continue
                actual_repr = generic_to_specific.get(id(used_repr), used_repr)
                defn = ent.Definition()
                if defn is None:
                    continue
                try:
                    pds = defn.Value()
                    pd = pds.Definition().Value()
                    pd_id_to_repr_ent[id(pd)] = actual_repr
                except Exception:
                    pass

            # --- Collect faces per failed representation ---
            repr_num_to_faces = {}  # repr model index → set of face indices
            repr_id_to_num = {}     # repr wrapper id → repr model index

            for i, (ent, tname) in enumerate(zip(ents, type_names)):
                if ent is None or id(ent) in mapped_ids:
                    continue
                faces = set()

                if tname == 'StepShape_ManifoldSurfaceShapeRepresentation':
                    mssr = ent
                    for j in range(1, mssr.NbItems() + 1):
                        item = mssr.ItemsValue(j)
                        if item.DynamicType().Name() != 'StepShape_ShellBasedSurfaceModel':
                            continue
                        sbsm = item
                        for k in range(1, sbsm.NbSbsmBoundary() + 1):
                            shell_select = sbsm.SbsmBoundaryValue(k)
                            shell = None
                            try:
                                shell = shell_select.ClosedShell()
                            except Exception:
                                pass
                            if shell is None:
                                try:
                                    shell = shell_select.OpenShell()
                                except Exception:
                                    pass
                            if shell is not None:
                                _collect_faces_from_shell(shell, faces)

                elif tname == 'StepShape_AdvancedBrepShapeRepresentation':
                    absr = ent
                    for j in range(1, absr.NbItems() + 1):
                        item = absr.ItemsValue(j)
                        if item.DynamicType().Name() == 'StepShape_ManifoldSolidBrep':
                            msb = item
                            outer = msb.Outer()
                            if outer is not None:
                                _collect_faces_from_shell(outer, faces)

                if faces:
                    repr_num_to_faces[i + 1] = faces
                    repr_id_to_num[id(ent)] = i + 1

            if not repr_num_to_faces:
                return

            # --- Build PD id → repr model index (only for failed reprs) ---
            pd_id_to_repr_num = {}
            for pd_id, repr_ent in pd_id_to_repr_ent.items():
                repr_num = repr_id_to_num.get(id(repr_ent))
                if repr_num is not None:
                    pd_id_to_repr_num[pd_id] = repr_num

            # --- Transfer faces per repr into compounds ---
            # Reuse the existing reader's model (avoids re-reading STEP file)
            import time as _time
            t_rec_start = _time.time()

            reader = basic_reader
            recovery_model = xcaf_model
            repr_num_to_compound = {}

            for repr_num, face_indices in repr_num_to_faces.items():
                # Try transferring the representation entity directly first
                # (one call instead of thousands of per-face calls)
                repr_ent = recovery_model.Value(repr_num)
                builder = BRep_Builder()
                compound = TopoDS_Compound()
                builder.MakeCompound(compound)
                ok = 0

                repr_ok = False
                try:
                    if reader.TransferEntity(repr_ent):
                        ns = reader.NbShapes()
                        if ns > 0:
                            builder.Add(compound, reader.Shape(ns))
                            ok = ns
                            repr_ok = True
                            print(f"[recovered repr#{repr_num} as whole: {ns} shapes]", end="", flush=True)
                except Exception:
                    pass

                if not repr_ok:
                    # Fallback: transfer individual faces
                    print(f"[recovering {len(face_indices)} faces]", end="", flush=True)
                    for idx in face_indices:
                        try:
                            if reader.TransferEntity(recovery_model.Value(idx)):
                                ns = reader.NbShapes()
                                if ns > 0:
                                    builder.Add(compound, reader.Shape(ns))
                                    ok += 1
                        except Exception:
                            pass
                    print(f"[{ok}/{len(face_indices)} OK]", end="", flush=True)

                if ok > 0:
                    repr_num_to_compound[repr_num] = compound

            print(f"[recovery transfer: {_time.time() - t_rec_start:.1f}s]", end="", flush=True)

            # --- Build final PD id → compound map ---
            for pd_id, repr_num in pd_id_to_repr_num.items():
                compound = repr_num_to_compound.get(repr_num)
                if compound is not None:
                    self._recovery_compounds[pd_id] = compound

            # Store TransferReader reference for shape→entity lookup
            self._transfer_reader = tr

        except Exception as e:
            print(f"[recovery failed: {e}]", end="", flush=True)

    def _get_recovery_compound(self, shape):
        """Return the recovery compound for a specific empty shape, or None.

        Uses EntityFromShapeResult to find the STEP ProductDefinition entity
        for the shape, then looks up the pre-built compound for that PD.
        """
        self._build_recovery_compound()
        if not self._recovery_compounds:
            return None

        # Find the ProductDefinition entity for this shape
        tr = getattr(self, '_transfer_reader', None)
        if tr is None:
            return None

        for mode in [-1, 1]:
            try:
                ent = tr.EntityFromShapeResult(shape, mode)
                if ent is not None:
                    # Wrapper identity matches self._recovery_keepalive keys
                    compound = self._recovery_compounds.get(id(ent))
                    if compound is not None:
                        return compound
            except Exception:
                pass

        return None

    def _heal_shape(self, shp):
        """Attempt to repair a shape using ShapeFix.

        Returns the healed shape, or the original if healing fails or makes
        things worse.
        """
        try:
            fixer = ShapeFix_Shape(shp)
            fixer.Perform()
            healed = fixer.Shape()
            # Only use healed shape if it actually has faces
            ex = TopExp_Explorer(healed, TopAbs_FACE)
            if ex.More():
                return healed
        except Exception:
            pass
        return shp

    def _tessellate_shape(self, shp, lin_def, ang_def, part_name="",
                          skip_faulty=False, relative=False):
        """Tessellate a shape, retrying with relaxed tolerances and shape
        healing if the first attempt produces no triangulation.

        When skip_faulty is True, skip all healing/recovery retries, just
        tessellate once and return whatever we get.
        """
        def _has_triangulation(s):
            loc = TopLoc_Location()
            ex = TopExp_Explorer(s, TopAbs_FACE)
            while ex.More():
                face = TopoDS.Face_s(ex.Current())
                if BRep_Tool.Triangulation_s(face, loc) is not None:
                    return True
                ex.Next()
            return False

        BRepTools.Clean_s(shp)
        _mesh_shape(shp, lin_def, ang_def, relative)

        if _has_triangulation(shp) or skip_faulty:
            return shp

        # Retry 1: heal shape then tessellate
        healed = self._heal_shape(shp)
        if healed is not shp:
            BRepTools.Clean_s(healed)
            _mesh_shape(healed, lin_def, ang_def, relative)
            if _has_triangulation(healed):
                print(f"\n  [healed] {part_name}", end="", flush=True)
                return healed
            # Healing changed the shape but it still won't mesh, so give the
            # relaxed-tolerance retry a chance on the healed geometry.
            shp = healed

        # Retry 2: relax tolerances significantly
        BRepTools.Clean_s(shp)
        _mesh_shape(shp, lin_def * 4.0, ang_def * 2.0, relative)
        print(f"\n  [relaxed-tess] {part_name}", end="", flush=True)
        return shp

    def build_trimesh(self, shape, lin_def=0.8, ang_def=0.5, hacks=set([]),
                      part_name="", fallback_color=None, relative=False):
        out_mesh = trimesh.TriMesh()
        out_mesh.matrix = np.eye(4, dtype=np.float32)

        _part_name = part_name
        skip_faulty = "skip_solids" in hacks

        # Check if the main shape has any faces at all
        all_faces = TopExp_Explorer(shape, TopAbs_FACE)
        all_subs_empty = not all_faces.More()
        if all_subs_empty:
            for ss in self.sub_shapes.get(ShapeKey(shape), []):
                ex_ss = TopExp_Explorer(ss, TopAbs_FACE)
                if ex_ss.More():
                    all_subs_empty = False
                    break

        # If shape is empty and skip_faulty is on, skip entirely: no
        # healing, no recovery compound, nothing that could produce corrupt
        # geometry and crash the native module.
        if all_subs_empty and skip_faulty:
            with self._lock:
                self.skipped_shapes.add(_part_name or "unknown")
            return out_mesh

        # Attempt per-face entity recovery for empty shapes (only when
        # skip_faulty is off; recovery can produce corrupt triangulations)
        recovered_shape = None
        if all_subs_empty and not skip_faulty:
            recovered_shape = self._get_recovery_compound(shape)
            if recovered_shape is not None:
                # Use recovered compound as the sole shape; discard sub_shapes
                # since we no longer have XCAF label associations.
                self.face_colors[ShapeKey(recovered_shape)] = self.face_colors.get(ShapeKey(shape))
                self.face_color_priority[ShapeKey(recovered_shape)] = self.face_color_priority.get(ShapeKey(shape), 0)
                with self._lock:
                    self.recovered_parts.append(_part_name or "unknown")

        iter_shapes = [shape] + self.sub_shapes.get(ShapeKey(shape), [])
        if recovered_shape is not None:
            iter_shapes = [recovered_shape]
        iter_shapes.sort(key=lambda x: x.Checked())

        # ── Phase 1: Collect all faces with metadata ──────────────────
        # Each entry: (face_obj, trf_obj, col_rgb_or_None, col_name, batch_id)
        collected_faces = []
        # Track face dedup: last occurrence of each OCC face wins
        face_dedup = OrderedDict()
        batch = 0

        for shp_i, shp in enumerate(iter_shapes):
            col = self.face_colors.get(ShapeKey(shp))
            if col is not None:
                col_rgb = b_RGB(col)
                col_name = b_colorname(col)
            elif fallback_color is not None:
                # Instance color override from the component reference label,
                # applies where no face/sub-shape color is present.
                col_rgb = fallback_color
                col_name = Quantity_Color.StringName_s(
                    Quantity_Color.Name_s(*fallback_color))
            else:
                col_rgb = None
                col_name = ""

            ex = TopExp_Explorer(shp, TopAbs_FACE)
            if not ex.More():
                if not skip_faulty:
                    healed = self._heal_shape(shp)
                    if healed is not shp:
                        shp = healed
                        ex = TopExp_Explorer(shp, TopAbs_FACE)
                if not ex.More():
                    with self._lock:
                        self.import_problems["Empty shape"] += 1
                    continue

            if not self._pre_tessellated:
                shp = self._tessellate_shape(
                    shp, lin_def, ang_def,
                    part_name=_part_name, skip_faulty=skip_faulty,
                    relative=relative)
                ex = TopExp_Explorer(shp, TopAbs_FACE)
            else:
                test_loc = TopLoc_Location()
                test_face = TopoDS.Face_s(ex.Current())
                if BRep_Tool.Triangulation_s(test_face, test_loc) is None:
                    shp = self._tessellate_shape(
                        shp, lin_def, ang_def,
                        part_name=_part_name, skip_faulty=skip_faulty,
                        relative=relative)
                    ex = TopExp_Explorer(shp, TopAbs_FACE)
                    print(f"\n  [re-tess] {_part_name}", end="", flush=True)
            trf = shp.Location().Transformation()

            while ex.More():
                face = TopoDS.Face_s(ex.Current())
                idx = len(collected_faces)
                collected_faces.append((face, trf, col_rgb, col_name, batch))
                # Dedup: record last index for each face object
                face_dedup[ShapeKey(face)] = idx
                ex.Next()
                batch += 1

        if not collected_faces:
            return out_mesh

        # ── Phase 2: Extract triangulations ───────────────────────────
        if _HAS_NATIVE:
            result = self._build_trimesh_native(
                collected_faces, face_dedup, out_mesh, lin_def, ang_def,
                relative)
        else:
            result = self._build_trimesh_python(
                collected_faces, face_dedup, out_mesh)

        return result

    def _build_trimesh_native(self, collected_faces, face_dedup, out_mesh,
                              lin_def=0.8, ang_def=0.5, relative=False):
        """Use native C++ module to extract all face triangulations at once.

        The deduped faces are packed into a compound and handed to the C++
        module as BinTools-serialized bytes (triangulation included).  The
        module deserializes with its own OCCT copy, meshes any face still
        missing triangulation, extracts everything and returns flat numpy
        arrays.  BinTools round-trips preserve compound insertion order, so
        results align with the meta/face_refs lists built here.

        Returns a NativeMeshData object (not TriMesh) that holds flat numpy
        arrays ready for from_pydata + foreach_set in Blender.
        """
        dedup_indices = set(face_dedup.values())

        meta = []  # parallel list: (col_rgb, col_name, batch)
        face_refs = []  # parallel list: (face_obj, trf_obj) for normal re-extraction

        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)

        test_loc = TopLoc_Location()
        for i in sorted(dedup_indices):
            face, trf, col_rgb, col_name, batch_id = collected_faces[i]
            # Verify face has valid, non-empty triangulation before handing
            # it to the native module (mirrors the Python fallback path).
            tri = BRep_Tool.Triangulation_s(face, test_loc)
            if tri is None or tri.NbTriangles() == 0 or tri.NbNodes() == 0:
                with self._lock:
                    self.import_problems["Triangulation"] += 1
                continue
            builder.Add(compound, face)
            meta.append((col_rgb, col_name, batch_id))
            face_refs.append((face, trf))

        if not face_refs:
            return out_mesh

        buf = io.BytesIO()
        BinTools.Write_s(compound, buf, True, False,
                         BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
        try:
            if _NATIVE_ABI >= 2:
                result = stepper_native.mesh_and_extract(
                    buf.getvalue(), lin_def, ang_def, relative)
            else:
                result = stepper_native.mesh_and_extract(
                    buf.getvalue(), lin_def, ang_def)
        except Exception as e:
            print(f"\n  [native extraction failed ({e}); Python fallback]",
                  end="", flush=True)
            return self._build_trimesh_python(
                collected_faces, face_dedup, out_mesh)
        (all_verts, all_norms, all_uvs, all_faces,
         face_starts, face_counts, vert_starts, vert_counts,
         failed_mask, undef_mask) = result

        if len(failed_mask) != len(face_refs):
            print("\n  [native face-count mismatch; Python fallback]",
                  end="", flush=True)
            return self._build_trimesh_python(
                collected_faces, face_dedup, out_mesh)

        # Count problems
        n_failed = int(failed_mask.sum())
        n_undef = int(undef_mask.sum())
        if n_failed > 0:
            with self._lock:
                self.import_problems["Triangulation"] += n_failed
        if n_undef > 0:
            with self._lock:
                self.import_problems["Undefined normals"] += n_undef

        # Re-extract normals from the analytic surface (BRepLProp_SLProps)
        # to match the proven v2.0.0 path.  The native C++ module and
        # facing.Normal() produce discrete triangulation normals that have
        # floating-point noise at OCC face boundaries, causing visible seams.
        # Parallelized with threads: OCC SWIG releases the GIL.
        _gp_res = gp.Resolution_s()

        def _recompute_face_normals(j):
            face, trf = face_refs[j]
            vs = int(vert_starts[j])
            vc = int(vert_counts[j])
            if vc == 0:
                return
            reversed_face = (face.Orientation() == TopAbs_REVERSED)

            # Extract 3x3 rotation from inverse transform once per face
            itform = trf.Inverted()
            rot = np.array([
                [itform.Value(1, 1), itform.Value(1, 2), itform.Value(1, 3)],
                [itform.Value(2, 1), itform.Value(2, 2), itform.Value(2, 3)],
                [itform.Value(3, 1), itform.Value(3, 2), itform.Value(3, 3)],
            ], dtype=np.float32)

            surface = BRepAdaptor_Surface(face)
            prop = BRepLProp_SLProps(surface, 2, _gp_res)

            # Use UVs already extracted by native module (skip SWIG calls)
            face_uvs = all_uvs[vs:vs + vc]
            u_vals = face_uvs[:, 0]
            v_vals = face_uvs[:, 1]
            Ucenter = (float(u_vals.min()) + float(u_vals.max())) * 0.5
            Vcenter = (float(v_vals.min()) + float(v_vals.max())) * 0.5

            # Pre-compute shrunk UVs in numpy (avoids per-vertex Python math)
            u_shrunk = (u_vals - Ucenter) * 0.999 + Ucenter
            v_shrunk = (v_vals - Vcenter) * 0.999 + Vcenter

            # Check once at face center whether normals are defined
            prop.SetParameters(float(Ucenter), float(Vcenter))
            face_has_normals = prop.IsNormalDefined()

            norms_local = np.empty((vc, 3), dtype=np.float32)
            if face_has_normals:
                # Fast path: skip per-vertex IsNormalDefined check
                for i in range(vc):
                    prop.SetParameters(float(u_shrunk[i]), float(v_shrunk[i]))
                    nd = prop.Normal()
                    norms_local[i] = (nd.X(), nd.Y(), nd.Z())
            else:
                # Rare path: check each vertex individually
                for i in range(vc):
                    prop.SetParameters(float(u_shrunk[i]), float(v_shrunk[i]))
                    if prop.IsNormalDefined():
                        nd = prop.Normal()
                        norms_local[i] = (nd.X(), nd.Y(), nd.Z())
                    else:
                        norms_local[i] = (0.0, 0.0, 1.0)

            # Bulk transform + flip with numpy (replaces per-vertex SWIG calls)
            norms_out = norms_local @ rot.T
            if reversed_face:
                norms_out = -norms_out
            all_norms[vs:vs + vc] = norms_out

            # Normalize this face's UVs, preserving aspect ratio (raw OCC
            # parameter space has arbitrary per-face scaling).  Done after
            # the raw values were used for SetParameters above.
            # Default: fit to a 0-1 box.  World mode (uv_world_scale set):
            # scale so 1 UV unit == 1 scene unit, using the face's 3D extent
            # as the reference, so texel density then matches across parts.
            u_min = float(u_vals.min())
            v_min = float(v_vals.min())
            span = max(float(u_vals.max()) - u_min, float(v_vals.max()) - v_min)
            if span > 1e-12:
                if self.uv_world_scale is not None:
                    fv = all_verts[vs:vs + vc]
                    ext = float((fv.max(axis=0) - fv.min(axis=0)).max())
                    s = (ext * self.uv_world_scale) / span
                else:
                    s = 1.0 / span
            else:
                s = 1.0
            all_uvs[vs:vs + vc, 0] = (u_vals - u_min) * s
            all_uvs[vs:vs + vc, 1] = (v_vals - v_min) * s

        valid_indices = [j for j in range(len(face_refs))
                         if not failed_mask[j]]
        from concurrent.futures import ThreadPoolExecutor, as_completed
        n_workers = min(len(valid_indices), os.cpu_count() or 4)
        if n_workers > 1 and len(valid_indices) > 1:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = [pool.submit(_recompute_face_normals, j)
                           for j in valid_indices]
                for f in as_completed(futures):
                    f.result()  # propagate exceptions
        else:
            for j in valid_indices:
                _recompute_face_normals(j)

        # Build combined flat arrays: accumulate all faces with global indices
        # Each OCC face has its own local vertex set; we concatenate them all.
        # all_verts/all_norms/all_uvs are already concatenated by the C++ module
        # all_faces has global indices into all_verts
        #
        # We need per-triangle: color, mat_name, batch
        n_total_tris = int(all_faces.shape[0])
        n_total_verts = int(all_verts.shape[0])

        # Per-triangle color: (T, 3) float32, -1.0 sentinel = no color
        tri_colors = np.full((n_total_tris, 3), -1.0, dtype=np.float32)
        # Per-triangle batch id
        tri_batches = np.zeros(n_total_tris, dtype=np.int32)
        # Per-triangle material name (Python list, strings)
        tri_mat_names = [None] * n_total_tris

        for j in range(len(meta)):
            if failed_mask[j]:
                continue
            col_rgb, col_name, batch_id = meta[j]
            fs = int(face_starts[j])
            fc = int(face_counts[j])
            if fc == 0:
                continue

            tri_batches[fs:fs + fc] = batch_id
            if col_rgb is not None:
                tri_colors[fs:fs + fc] = col_rgb
                mat_name = col_name if col_name else None
                tri_mat_names[fs:fs + fc] = [mat_name] * fc

        return NativeMeshData(
            verts=all_verts,
            faces=all_faces,
            norms=all_norms,
            uvs=all_uvs,
            tri_colors=tri_colors,
            tri_batches=tri_batches,
            tri_mat_names=tri_mat_names,
            matrix=np.eye(4, dtype=np.float32),
        )

    def _build_trimesh_python(self, collected_faces, face_dedup, out_mesh):
        """Python fallback: use triangulate_face per face."""
        dedup_indices = set(face_dedup.values())

        for i in sorted(dedup_indices):
            face, trf, col_rgb, col_name, batch_id = collected_faces[i]
            try:
                mesh = self.triangulate_face(face, trf)
            except Exception:
                mesh = None

            if mesh:
                mesh.set_batch(batch_id)
                if col_rgb is not None:
                    mesh.colorize(col_rgb)
                    mesh.set_material_name(col_name)
                if len(mesh.verts) > 0:
                    out_mesh.add_mesh(mesh)

        return out_mesh

    def build_nurbs(self, shape):
        iter_shapes = [shape]
        nbs = []
        for shp_i, shp in enumerate(iter_shapes):
            ex = TopExp_Explorer(shp, TopAbs_FACE)
            if not ex.More():
                with self._lock:
                    self.import_problems["Empty shape"] += 1
                return []

            while ex.More():
                pt = nurbs_parse(TopoDS.Face_s(ex.Current()))
                nbs.append(pt)
                ex.Next()

        return nbs
