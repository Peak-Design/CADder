"""CI smoke test for an assembled STEPper NEXT addon directory.

Usage: python ci/smoke_test.py <assembled_addon_dir>

Validates on the build platform that:
1. every Python module of the addon was actually copied into the package
   (a missing file would otherwise ship a zip that fails to register);
2. the vendored OCP package imports and the STEP reader is reachable;
3. the built stepper_native module loads against its native_libs OCCT
   subset and completes a BinTools serialize handoff round-trip.
"""
import io
import os
import sys

addon_dir = os.path.abspath(sys.argv[1])
sys.path.insert(0, addon_dir)

# 1. All addon files present in the assembled package
REQUIRED_FILES = [
    "__init__.py", "main.py", "importer.py", "import_ui.py", "uv.py",
    "tools.py", "formats.py", "curves.py", "analyzer.py", "background.py",
    "worker.py", "trimesh.py", "nurbs.py", "ocp_utils.py", "stepanalyzer.py",
    "updater.py", "refresh.py",
    "blender_manifest.toml", "LICENSE",
]
missing = [f for f in REQUIRED_FILES
           if not os.path.isfile(os.path.join(addon_dir, f))]
assert not missing, f"assembled package is missing files: {missing}"
print(f"OK: all {len(REQUIRED_FILES)} required files present")

# 2. Vendored OCP bindings
import OCP  # noqa: F401
from OCP.OCP import __version__ as ocp_version
from OCP.STEPCAFControl import STEPCAFControl_Reader  # noqa: F401

print(f"OK: OCP {ocp_version} imports, STEPCAFControl_Reader available")

# 3. Native module handoff
if os.name == "nt":
    os.add_dll_directory(os.path.join(addon_dir, "native_libs"))
import stepper_native

from OCP.BRep import BRep_Builder
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound

box = BRepPrimAPI_MakeBox(1.0, 2.0, 3.0).Shape()
BRepMesh_IncrementalMesh(box, 0.8, False, 0.5, False).Perform()

builder = BRep_Builder()
compound = TopoDS_Compound()
builder.MakeCompound(compound)
ex = TopExp_Explorer(box, TopAbs_FACE)
n_faces = 0
while ex.More():
    builder.Add(compound, TopoDS.Face_s(ex.Current()))
    n_faces += 1
    ex.Next()

buf = io.BytesIO()
BinTools.Write_s(compound, buf, True, False,
                 BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
result = stepper_native.mesh_and_extract(buf.getvalue(), 0.8, 0.5)
(all_verts, all_norms, all_uvs, all_faces,
 face_starts, face_counts, vert_starts, vert_counts,
 failed_mask, undef_mask) = result

assert len(failed_mask) == n_faces, (
    f"native returned {len(failed_mask)} faces, expected {n_faces}")
assert not failed_mask.any(), "native reported failed faces for a plain box"
assert all_faces.shape[0] >= 12, f"expected >=12 triangles, got {all_faces.shape[0]}"

# ABI v2: optional relative-tessellation argument
abi = getattr(stepper_native, "ABI_VERSION", 1)
assert abi >= 2, f"expected native ABI_VERSION >= 2, got {abi}"
result4 = stepper_native.mesh_and_extract(buf.getvalue(), 0.8, 0.5, True)
assert result4[3].shape[0] >= 12, "4-arg (relative) call returned no triangles"

print(f"OK: stepper_native handoff (ABI {abi}): {n_faces} faces, "
      f"{all_faces.shape[0]} triangles, {all_verts.shape[0]} vertices")
print("SMOKE TEST PASSED")
