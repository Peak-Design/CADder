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
#     7.9.3.1.1, pybind11); native module reworked to a BinTools serialize
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
#     UI stays responsive, live progress in the status bar, Esc cancels;
#     identical results to the synchronous path (v2.4.3)
#   - Code-review hardening pass (v2.4.4): fixed multi-level Prune/Restore
#     rebuilding inverted; artist-friendly detail slider being silently
#     overridden by the seeded quality preset; batch import ignoring the
#     preference defaults; Regenerate/Reload using the STEP reader for
#     IGES/BREP files; background worker now inherits the user's addon
#     preferences, kills itself if the parent Blender dies, and can no
#     longer hang on a silent worker exit; long CAD material names no
#     longer abort the import; per-label surface color no longer overridden
#     by curve color; edit-mode guards on all mesh tools; cursor-percentage
#     and tooltip/layout polish throughout; CI fail-fast hardening
#   - v2.4.5: parented-empties imports now leave every object (empties
#     included) at scale 1 with the scale baked into meshes; UV generation
#     unified into a single "UVMap" layer with a mode dropdown (None / CAD
#     Surface / Unwrap / Box Project); "Normalize UVs" toggle (off = UVs
#     scaled to real-world units, with packed unwrap islands uniformly
#     rescaled so 1 UV unit ~= 1 scene unit, for consistent texel
#     density across parts); Unwrap mode = packed angle-based unwrap
#     with CAD sharp edges as seams, also honored by Regenerate;
#     engineering material metadata (AP242/AP214 name/description/density,
#     e.g. from CATIA/NX material assignments) imported as STEP_material*
#     custom properties on every object, plus an "Engineering Materials"
#     option (on by default) assigning one Blender material per part named
#     after the CAD material (feeds the Material Database); "Split Closed
#     Faces" (on by default): UV seams added along the parametric closure
#     of cylinders/cones/tori AND across smooth-joined face groups forming
#     closed tubes/rings (Euler-characteristic test), so unwrapping
#     flattens them cleanly, shading unaffected; import dialog options are
#     remembered across Blender sessions ("Remember import settings"
#     preference, on by default; folder batch import follows them too);
#     Normalize UVs now defaults to off (real-world UV scale); recursive
#     folder batch import; multi-file drag & drop fixed (files list now
#     uses OperatorFileListElement)
#   - v2.4.6: fixed engineering materials always being created grey. The
#     part's imported color was read from mat.diffuse_color (the viewport
#     swatch), which the addon never writes, so it always came back as
#     Blender's default 0.8 grey; it now reads the Principled BSDF's Base
#     Color input, which is where add_material puts the CAD color
#   - Import options: "Group in a collection" puts everything one file
#     creates under a collection named after it, and "Separate solids"
#     gives every body of a multibody part its own object
#   - Imported files panel: lists every STEP file the .blend has imported
#     and refreshes one from disk, keeping the collections, parenting and
#     visibility arranged around it (refresh.py)
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

#   - rig/ subpackage added (SW To Blender): builds a constrained armature
#     from the .rig.json manifest written by the Peak.SwToBlender SolidWorks
#     add-in (github.com/Peak-Design/SW-To-Blender holds the exporter and
#     the manifest schema) and parents imported STEP geometry to the bones.
#     Registered from main.register(), guarded so a rig fault never costs
#     STEP import; panel in the 3D View sidebar under "SW To Blender".
#     Tests in ci/rig/, headless smoke in ci/rig_smoke.py
#   - rig/: scene-frame detection — a STEP imported with another up axis
#     (e.g. Y-up) rotates the geometry away from the manifest's Z-up frame;
#     matching now estimates that transform from its own name/path matches
#     (candidate up-axis rotations compete when no anchors exist) and the
#     rig builds through it, landing on the geometry whatever the import
#     orientation. The match report names the detected frame.
#   - rig/: dropped the GRP_ per-group empties — geometry now parents
#     directly to the bones; identity lives in the RIG_* object tags, so
#     the middleman bought nothing and cluttered the outliner. Legacy
#     GRP_ empties are cleaned up on the next Build Rig
#   - rig/: the rig is named after the assembly (<step base>_Rig, e.g.
#     hinge_Rig) instead of a fixed SW_Rig, so rigs from several
#     assemblies coexist; rebuilds still replace the same assembly's rig
#   - rig/: rig placement follows the geometry — the scene frame now
#     carries translation too (STEPper imports land at the 3D cursor), and
#     name-anchored frame estimation runs even without occurrence paths;
#     with no frame at all the rig builds at the 3D cursor, never silently
#     at the world origin
#   - rig/: ball-joint swing cones are symmetric about the rest pose — the
#     mate dimension is an unsigned swing angle, and applying its raw
#     0..max range per axis pinned the swing into one quadrant of the
#     socket (and jittered against the one-sided clamps)
#   - rig/: pin_slot joint type (manifest schema addition): spin about bone
#     Y plus slide along bone Z (secondary_axis is the slide direction for
#     this type). IK limits on ball joints in loop chains now use the same
#     symmetric swing cone as the Limit Rotation constraint
#   - rig/: loop closures rebuilt around a real IK end-effector — a second
#     hidden bone rides the driven tip with its tail exactly on the closure
#     point and owns the IK constraint. The old constraint pulled the tip
#     bone's own tail, which sits off the closure point and cannot move in
#     the mechanism plane, so four-bars froze solid. Pairs with the
#     exporter-side fix that cuts each loop just past its driver joint
#   - rig/: manifest file browser filters to *.rig.json (new filtered
#     browse button; the bare path field stays editable)

bl_info = {
    "name": "STEPper NEXT",
    "author": "ambi, Peak-Design",
    "description": "STEP OpenCASCADE import",
    "blender": (5, 1, 0),
    "version": (2, 4, 6),
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
