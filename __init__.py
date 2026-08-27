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
#   - Ported to Blender 5.0 API (v2.0.0)
#   - Fixed error in importing files with only single part with tree hierarchy option enabled
#   - Added failed parts popup and import diagnostics
#   - Updated to pythonocc-core 7.9.3 / Python 3.13 for Blender 5.1 (v2.1.0)
#   - Fixed tessellation race conditions and corrupt STEP handling
#   - Added ShapeFix healing for shapes with corrupted/missing geometry
#   - Fixed crash: validate face triangulations before native C++ extraction
#   - Renamed to STEPper NEXT, auto-apply scale, skip empty objects (v2.1.3)
#   - Material database system, multi-user scale fix (v2.2.0)
#   - Migrated OCC bindings from pythonocc-core to OCP (cadquery-ocp-novtk
#     7.9.3.1.1, pybind11). Native module reworked to a BinTools serialize
#     handoff, decoupling it from the Python bindings' ABI (v2.3.0)
#   - Modern import dialog with quality presets and unit-aware deflection
#     (physical mm regardless of file units), collection-instances hierarchy
#     mode, construction-geometry filters, per-instance color overrides,
#     import-dialog defaults in preferences (v2.4.0)
#   - SurfaceUV/BoxUV layers, viewport drag & drop, folder batch import,
#     state-preserving Regenerate, Prune/Restore hierarchy, mesh cleanup
#     (v2.4.1)
#   - IGES and BREP import, free-edge curves import, relative (adaptive)
#     tessellation with parallel meshing, pre-import file analyzer with
#     per-machine time estimates (v2.4.2)
#   - Non-blocking background import via a headless-Blender worker process:
#     UI stays responsive, live progress in the status bar, Esc cancels. Identical results to the synchronous path (v2.4.3)
#   - Code-review hardening pass (v2.4.4): fixed multi-level Prune/Restore
#     rebuilding inverted. Artist-friendly detail slider being silently
#     overridden by the seeded quality preset. Batch import ignoring the
#     preference defaults. Regenerate/Reload using the STEP reader for
#     IGES/BREP files. Background worker now inherits the user's addon
#     preferences, kills itself if the parent Blender dies, and can no
#     longer hang on a silent worker exit. Long CAD material names no
#     longer abort the import. Per-label surface color no longer overridden
#     by curve color. Edit-mode guards on all mesh tools. Cursor-percentage
#     and tooltip/layout polish throughout. CI fail-fast hardening
#   - v2.4.5: parented-empties imports now leave every object (empties
#     included) at scale 1 with the scale baked into meshes. UV generation
#     unified into a single "UVMap" layer with a mode dropdown (None / CAD
#     Surface / Unwrap / Box Project). "Normalize UVs" toggle (off = UVs
#     scaled to real-world units, with packed unwrap islands uniformly
#     rescaled so 1 UV unit ~= 1 scene unit, for consistent texel
#     density across parts). Unwrap mode = packed angle-based unwrap
#     with CAD sharp edges as seams, also honored by Regenerate. Engineering material metadata (AP242/AP214 name/description/density,
#     e.g. from CATIA/NX material assignments) imported as STEP_material*
#     custom properties on every object, plus an "Engineering Materials"
#     option (on by default) assigning one Blender material per part named
#     after the CAD material (feeds the Material Database). "Split Closed
#     Faces" (on by default): UV seams added along the parametric closure
#     of cylinders/cones/tori AND across smooth-joined face groups forming
#     closed tubes/rings (Euler-characteristic test), so unwrapping
#     flattens them cleanly, shading unaffected. Import dialog options are
#     remembered across Blender sessions ("Remember import settings"
#     preference, on by default. Folder batch import follows them too).
#     Normalize UVs now defaults to off (real-world UV scale). Recursive
#     folder batch import. Multi-file drag & drop fixed (files list now
#     uses OperatorFileListElement)
#   - v2.4.6: fixed engineering materials always being created gray. The
#     part's imported color was read from mat.diffuse_color (the viewport
#     swatch), which the addon never writes, so it always came back as
#     Blender's default 0.8 gray. It now reads the Principled BSDF's Base
#     Color input, which is where add_material puts the CAD color
#   - v2.4.7: Imported files panel, listing every STEP file the .blend has
#     imported and refreshing one from disk. A refresh keeps the objects
#     and moves only the mesh, the material slots and the CAD placement on
#     to them, so modifiers, constraints, animation, collections,
#     parenting, vertex groups, materials you assigned and the placement
#     you gave them all survive. It can no longer change the size either:
#     a file imported in the background, or at a custom scale, came back a
#     thousand times smaller, because the settings it was imported with
#     were recorded on a worker scene that is deleted after the append
#     (refresh.py)
#   - The object color now carries the CAD color, so a Solid viewport
#     set to Object color matches the file without a material preview
#   - Import options: "Group in a collection" puts everything one file
#     creates under a collection named after it, and "Separate solids"
#     gives every body of a multibody part its own object
#   - The file cache now checks the size and modification time on disk, so
#     re-exporting over the same path no longer imports yesterday's
#     geometry
#   - Material databases can live in a folder of your choosing, so a
#     reinstall cannot wipe them and a team can share one library
#   - A part name that is not valid UTF-8 (a degree sign, for example) no
#     longer aborts the whole import
#   - Update notice in the sidebar with a download link for your platform,
#     plus a Ko-fi link, and all user facing copy rewritten in Simplified
#     Technical English

bl_info = {
    "name": "STEPper NEXT",
    "author": "ambi, Peak-Design",
    "description": "STEP OpenCASCADE import",
    "blender": (5, 1, 0),
    "version": (2, 4, 7),
    "location": "3D View > Tools panel > STEPper NEXT",
    "category": "Import",
}

INSIDE_BLENDER = True
try:
    import bpy
except ModuleNotFoundError:
    print("Stepper not running inside Blender.")
    INSIDE_BLENDER = False


if INSIDE_BLENDER:
    # Normally don't do import star, but here it's basically a file concatenation
    # File concatenation is because the test framework breaks on __init__.py import bpy
    from .main import *  # noqa: F403
