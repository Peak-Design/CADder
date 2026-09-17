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
#   - Renamed to CADder, auto-apply scale, skip empty objects (v2.1.3)
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
#   - v2.5.0: the UV release. CAD Surfaces UVs now carry the proportions of
#     the surface. OCC hands back raw parameters, and on a cylinder u is an
#     angle while v is a length, so a drum arrived as a thin tall ribbon.
#     Each direction is scaled by the length of its own derivative, which
#     takes the median anisotropy on a 182 part assembly from 157 to 1.0
#     and the share of surface stretched more than twice as far one way as
#     the other from 55 percent to 0
#   - Patches of one surface share one UV chart. A boolean through a
#     cylinder leaves two or three faces on one surface, and each was
#     fitted to its own box, so a drilled hole came out as three islands
#     with a packing margin between parts of one tube (3,431 islands to
#     2,827, none folded)
#   - Two CAD faces no longer share one folded island. Every face was
#     normalized into its own 0 to 1 box, so two flat faces of the same
#     size wrote the same UVs and Blender read them as one island, folded
#     on itself. Each face takes a small offset of its own (folded islands
#     8 to 0)
#   - Every island of a part now holds the same number of texels for each
#     millimeter of surface. With Normalize UVs on, each face was fitted to
#     the square on its own, so a 5 mm face and a 200 mm face came out the
#     same size (9.2 to 1.0). Where the packer may resize, it averages the
#     island scale first (Box Project 1.2 to 1.0, all parts in one tile
#     17.2 to 1.0)
#   - The UV Map dropdown holds every way to make the UVs: None, CAD
#     Surfaces, CAD Surfaces (Smart), Unwrap (Conformal), Unwrap (Angle
#     Based), Unwrap (Minimum Stretch) and Box Project. The unwrap was fixed
#     at angle based, which can fold a long cylinder on to itself. The old
#     Unwrap mode maps to Unwrap (Angle Based), so a refresh of an older
#     import makes the same map (uv.migrate_settings)
#   - New import option "Pack UVs": None, all parts together, each part on
#     its own, or into a set number of UDIM tiles. The islands used to keep
#     whatever place the UV mode gave them, which on 1000 parts spread them
#     over 147 tiles and filled about 1 percent. A "Pack margin" setting
#     comes with it, and the addon scales the margin down as more parts
#     share a tile
#   - "Split Closed Faces" becomes "Closed surfaces" with three choices:
#     None, Single seam (the default) or Split faces. Single seam cuts a
#     closed region until it is flat, so a hole unrolls into one island and
#     its halves stay joined
#   - New import option "Tris to Quads", on by default. It pairs the
#     tessellation triangles back into quads, which takes 1000 parts from
#     540,000 faces to 285,000 for about 2 percent of the import time. No
#     vertex moves and the CAD shading is unchanged
#   - New UV Map mode "CAD Surfaces (Smart)". A sheet metal part is a
#     plate, a bend and another plate, and none of those boundaries is
#     sharp, so the run is one continuous surface that a press brake
#     flattens into one rectangle. Smart builds the islands one face at a
#     time, the way a paper model is cut out, and keeps a join only if the
#     island does not land on itself and still fits the UV tile
#     (uv.smart_merge). A face that does not fit by a turn, such as a
#     rounded rim or the end ring of a tube, is bent along the island's
#     edge, within the new "Smart distortion" limit. A rounded tube comes
#     out as two islands instead of six. The new option "Join sharp edges"
#     runs a second pass across sharp edges, and Smart cuts an island along
#     its joins where the pieces pack better (uv._split_islands). The option
#     "Optimize island shape" controls that cut
#   - Regenerate pairs the triangles into quads again and unwraps with the
#     method the part was imported with. It left the triangles and always
#     used Conformal
#   - Tris to Quads and Clean Up Meshes keep the CAD shading. Both went
#     through bmesh, which moves custom normals when faces join
#   - Parts with more than one body import whole. Vertices were welded by
#     position across the whole part, so two bodies that touch were welded
#     together and a duplicate filter dropped one body's triangles where
#     they met. Faces were matched with their orientation, so a color
#     label that holds a face turned over meshed the face a second time.
#     And each labeled face was meshed again on its own, which can split
#     its edges differently from its neighbors. A buoyancy module goes
#     from 1,476 edges shared by more than two faces to none, and a gear
#     motor from 146 open edges to 14
#   - Pack UVs and the unwrap modes keep the UVs square when the material
#     has an image texture that is not square. Blender's Pack Islands and
#     Unwrap fit the islands to that image, so on a 2 by 1 texture a square
#     face came out 4 times too narrow. The pack now runs with the
#     materials hidden (main._hide_materials), and Unwrap with
#     correct_aspect off
#   - A background import no longer fails when a part produces no geometry.
#     The worker opened the warning popup, and Blender with no window
#     crashes on that. The worker now sends the list to the session that
#     started it, which shows the popup
#   - New "CADder: UV" sidebar panel. It makes the UV map of the
#     selected parts again with the same settings as the import dialog, so
#     one part can get a treatment its neighbor does not. It replaces the
#     Box Project UVs button, which is now one mode of its dropdown
#   - The CADder sidebar tab now sits after Item, Tool and View. A
#     panel with no header registers in front of every panel that has one,
#     whatever bl_order says, and that pulled the whole tab to the top

#   - rig/ subpackage added (CAD Link): builds a constrained armature
#     from the .rig.json manifest written by the Peak.Cadder SolidWorks
#     add-in (github.com/Peak-Design/CADder-SW-Bridge holds the exporter and
#     the manifest schema) and parents imported STEP geometry to the bones.
#     Registered from main.register(), guarded so a rig fault never costs
#     STEP import. Panel in the 3D View sidebar under "CAD Link".
#     Tests in ci/rig/, headless smoke in ci/rig_smoke.py
#   - rig/: scene-frame detection. A STEP imported with another up axis
#     (e.g. Y-up) rotates the geometry away from the manifest's Z-up frame. Matching now estimates that transform from its own name/path matches
#     (candidate up-axis rotations compete when no anchors exist) and the
#     rig builds through it, landing on the geometry whatever the import
#     orientation. The match report names the detected frame.
#   - rig/: dropped the GRP_ per-group empties. Geometry now parents
#     directly to the bones. Identity lives in the RIG_* object tags, so
#     the middleman bought nothing and cluttered the outliner. Legacy
#     GRP_ empties are cleaned up on the next Build Rig
#   - rig/: the rig is named after the assembly (<step base>_Rig, e.g.
#     hinge_Rig) instead of a fixed SW_Rig, so rigs from several
#     assemblies coexist. Rebuilds still replace the same assembly's rig
#   - rig/: rig placement follows the geometry. The scene frame now
#     carries translation too (CADder imports land at the 3D cursor), and
#     name-anchored frame estimation runs even without occurrence paths. With no frame at all the rig builds at the 3D cursor, never silently
#     at the world origin
#   - rig/: ball-joint swing cones are symmetric about the rest pose. The
#     mate dimension is an unsigned swing angle, and applying its raw
#     0..max range per axis pinned the swing into one quadrant of the
#     socket (and jittered against the one-sided clamps)
#   - rig/: pin_slot joint type (manifest schema addition): spin about bone
#     Y plus slide along bone Z (secondary_axis is the slide direction for
#     this type). IK limits on ball joints in loop chains now use the same
#     symmetric swing cone as the Limit Rotation constraint
#   - rig/: loop closures rebuilt around a real IK end-effector. A second
#     hidden bone rides the driven tip with its tail exactly on the closure
#     point and owns the IK constraint. The old constraint pulled the tip
#     bone's own tail, which sits off the closure point and cannot move in
#     the mechanism plane, so four-bars froze solid. Pairs with the
#     exporter-side fix that cuts each loop just past its driver joint
#   - rig/: manifest file browser filters to *.rig.json (new filtered
#     browse button. The bare path field stays editable)

bl_info = {
    "name": "CADder",
    "author": "ambi, Peak-Design",
    "description": "Import CAD files, link SolidWorks, and rig assemblies",
    "blender": (5, 1, 0),
    "version": (1, 0, 0),
    "location": "3D View > Tools panel > CADder",
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
