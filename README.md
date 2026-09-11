<h1 align="center">STEPper NEXT</h1>

<p align="center">
  <strong>STEP, IGES and BREP import for Blender, on the OpenCASCADE kernel.</strong><br>
  <a href="../../releases/latest">Download</a> ·
  <a href="#install">Install</a> ·
  <a href="#features">Features</a> ·
  <a href="#material-database">Material database</a> ·
  <a href="https://ko-fi.com/oskarasspalvys">Tip jar</a>
</p>

---

STEPper NEXT imports STEP (`.step` / `.stp`), IGES (`.iges` / `.igs`) and BREP
(`.brep` / `.brp`) files directly into Blender using the OpenCASCADE (OCC)
geometry kernel. The produced mesh is a triangulation of the underlying CAD
surface with smooth normals computed from the analytic shape geometry.

Originally created by **ambi** (Tommi Hyppanen). Now maintained by
**Peak Design** (Oskaras Spalvys).

## Features

**Geometry**

- Direct STEP, IGES and BREP import via OpenCASCADE
- Analytic surface normals give smooth shading across curved surfaces
- Sharp edges marked from the CAD topology, with custom split normals
- Quality presets with unit-aware deflection (a physical 0.8 mm stays 0.8 mm whatever units the file uses), plus relative (adaptive) tessellation
- Corrupted geometry: the addon imports what it can instead of skipping a whole part, and repairs damaged shapes with ShapeFix
- Free edges and sketches optionally imported as curve objects

**Materials & UVs**

- Per-face vertex colors and automatic material creation from STEP color data
- The CAD color is also written to the object color, so a Solid viewport set to Object color matches the file
- Engineering material metadata (AP242/AP214 name, description, density) imported as custom properties, and optionally as named Blender materials
- Material database system for automatic material replacement on import
- UV generation from CAD surfaces (face by face, or joined into larger islands), Blender unwrap, or box projection, with optional real-world UV scale and automatic seams on cylindrical/closed faces

**Workflow**

- Non-blocking background import: Blender stays responsive, Esc cancels
- Viewport drag & drop (single or multiple files) and recursive folder batch import
- Pre-import analyzer with per-machine import-time estimates
- Regenerate parts at a different quality, Prune/Restore hierarchy, mesh cleanup
- Import options remembered between Blender sessions
- Part hierarchy preserved as flat collection, nested collections, parented empties, or collection instances
- Native C++ mesh extraction with multithreaded normal computation, up to 10x faster than v1.x

## Install

STEPper NEXT ships as a Blender **extension** (since v2.3.0). The OpenCASCADE
(OCP) bindings are bundled as a wheel that Blender installs automatically.

1. Download the `.zip` for your platform from the [Releases](../../releases) page.
2. Drag & drop the `.zip` into a Blender window (or use **Edit > Preferences > Get Extensions >** drop-down menu **> Install from Disk...**).
3. Enable it under **Add-ons** if Blender does not enable it for you.

The importer panel will appear in **3D View > Tools panel > STEPper NEXT**.

> **Upgrading from v2.2.x or older (legacy addon):** remove the old
> "STEPper NEXT" entry from **Preferences > Add-ons** and restart Blender
> before installing the extension.

To remove or update: remove the extension from **Preferences > Get Extensions >
Installed** (or Add-ons). To update, install the new `.zip` and Blender
replaces the older version.

### Requirements

- **Blender 5.1** with Python 3.13
- **Windows only:** [Visual Studio C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170) (vc_redist.x64.exe)

### Platform support

| Platform | Status |
|----------|--------|
| **Windows 10+ (64-bit)** | Tested and supported |
| **macOS Apple Silicon (M1/M2/M3/M4)** | Experimental (untested) |
| **Linux (64-bit)** | Experimental (untested) |

> **Note:** macOS and Linux builds are automatically compiled via GitHub Actions but have not been tested yet. If you encounter issues on these platforms, please [open an issue](../../issues).

## Material Database

The material database lets you define mappings from generic STEP material names (e.g., "GRAY", "BLACK") to authored Blender materials. Once configured, materials are automatically replaced every time you import a STEP file.

![Material Mappings Panel](docs/material_mappings.png)

### Setup

1. Import a STEP file normally. Objects load with generic STEP materials.
2. Assign the Blender materials you want to each part (e.g., replace "GRAY" with "Stainless Steel" etc.).
3. In the **STEPper NEXT: Material DB** sidebar panel, click **New** to create a database. The addon scans the scene and records what each original STEP material was replaced with.
4. Manually assign/tweak material mappings in the mapping table if required.
5. The database is saved as a `.blend` file in the addon's `MaterialDB/` folder.

### Importing with a database

Select a database from the dropdown in the STEP import dialog under **Material DB**. The selected database persists between sessions. When importing, all matching STEP materials are automatically replaced.

### Imported files, and refreshing them

The N-panel lists every STEP file this .blend has imported, with what it holds
and whether the file has changed on disk since. **Refresh from disk** reads the
file again and moves the new CAD data onto the objects you already have.

The objects are kept, not rebuilt. Only the mesh, the material slots and the
CAD placement change, so the work you built on top stays:

- modifiers, constraints, drivers and animation.
- the collections you put an object in, including a part ctrl-dragged into a
  second collection so it lives in two places at once.
- objects of your own that you parented to a part.
- a part you re-parented onto a rig of your own. The file decides the assembly
  structure everywhere else.
- materials you assigned yourself, and your vertex groups.
- what you hid, your custom properties and your object colors.

Placement works the same way. The importer writes down where it put each
object, so a refresh can tell a part that moved in CAD from one you moved in
Blender. A part you moved stays where you put it. A part that moved in CAD
moves. A part that moved in both does both.

A refresh never changes the size of the assembly. The settings a file was
imported with are remembered per file and stamped on the objects, so a refresh
repeats the import instead of using whatever the dialog holds at the time.

Components that have gone from the file, or are new in it, are reported rather
than guessed at. That is the signal that the assembly itself changed, and not
only its geometry.

### Import options worth knowing

- **Group in a collection**: everything a file creates goes under one
  collection named after it, so a second import does not interleave with the
  first.
- **Import curves**: free edges (sketches, construction wires) become POLY
  curve objects in a collection named "Cad Curves". They keep their place in
  the assembly and ride their parent.
- **Separate solids**: one object per body of a multibody part, for files
  that hold several solids, shells or surfaces with no assembly structure to
  tell them apart. Off by default.
- The **material database folder** can be set in preferences. Left empty it
  uses the folder inside the addon, which a reinstall wipes and which cannot be
  shared between machines.

### Panel buttons

| Button | Description |
|--------|-------------|
| **New** | Create a new database from the current scene. Scans all STEP objects and records current material assignments. If the same original material was replaced with different materials on different parts, the most common replacement wins. |
| **Duplicate** | Copy the active database under a new name. Useful for minor variations between projects. |
| **Load** | Reload mappings from the active database file and append its materials into the current file. |
| **Delete** (trash icon) | Delete the active database file. |
| **Update** | Scan the scene for any new original STEP material names not already in the database and add them. **Does not modify existing mappings.** Use this to expand and grow your material database. Does not auto-save. |
| **Save** | Write the current mappings and materials to the database file. |
| **Apply** | Apply the active database mappings to objects in the scene. Works with the **Selection only** checkbox to limit to selected objects. |

### Material mappings table

Each row shows an original STEP material name and a dropdown to pick the replacement Blender material. You can change any mapping and click **Save** to update the database.

### Notes

- Databases are stored in the `MaterialDB/` folder inside the addon directory.
- The active database selection is stored in addon preferences and persists across sessions and files.
- Original STEP material names are stored on each imported object as a `STEP_materials` custom property, so re-applying a different database always works correctly.
- Linked materials (e.g., from the Blender asset browser) are fully supported. A local copy is saved into the database file so it can be loaded in any `.blend` file.

## Engineering Materials (AP242 / AP214)

STEP files can carry the engineering material assigned in the source CAD system (name, description and density) alongside the geometry. Every import stores whatever it finds on each object as `STEP_material`, `STEP_material_desc` and `STEP_material_density` custom properties.

The **Engineering Materials** import option is on by default. It gives each part one Blender material named after the CAD material, such as `AISI 304 Steel`, instead of the color materials. This works with the Material Database above. Map `AISI 304 Steel` to your own steel shader once, and every later import uses it.

> **Not every CAD system writes this data.** SOLIDWORKS does not write engineering material data into STEP in any schema. Its "Export Appearances" option carries only flat per face colors, because STEP cannot hold textures or PBR properties. SOLIDWORKS also flattens the appearance overrides on components and assemblies, so every instance of a part arrives with the part color. CATIA and NX do write material data when you turn on the right export options. A file without material data imports without the custom properties.
>
> **Do you work in SOLIDWORKS?** Our free [NEXT-STEP](https://github.com/Peak-Design/NEXT-STEP-SW) add-in closes both gaps. It writes the material name and the density into the STEP file. It also keeps the color override on each instance. The geometry stays identical to the native SOLIDWORKS output.

## UV Maps

The addon creates one `UVMap` layer. The **UV Map** import option chooses what goes into it.

| Mode | Description |
|------|-------------|
| **None** | No UV layer. |
| **CAD Surfaces** | One island per CAD face, taken from the parametric surface coordinates. The fastest mode, and the default. |
| **CAD Surfaces (Smart)** | Joins CAD faces that meet smoothly into larger islands, bends a face to fit where it has to, and cuts an island where its pieces pack better. It can join across sharp edges too. A bent sheet metal part comes out as its flat pattern, a rounded tube as two islands. See below. |
| **Unwrap (Conformal)** | Blender's unwrap with packed islands. Sharp CAD edges act as seams. Keeps the angles. |
| **Unwrap (Angle Based)** | The same unwrap with the angle based method. It can fold a long cylinder on to itself. |
| **Unwrap (Minimum Stretch)** | The same unwrap with the minimum stretch method. The slowest mode. |
| **Box Project** | Triplanar projection with a world-unit tile size. |

> **Recommended settings for large models:** import with **UV Map** set to
> **CAD Surfaces** and **Pack UVs** set to **None**. CAD Surfaces takes its
> UVs from the CAD surface while the part is meshed, so it adds almost no
> time.
>
> The other UV modes and packing add a lot of time on a large model. For
> example, Smart adds about 35% and packing each part on its own adds about
> 86%. Use them on single parts: select the parts, then click **Apply UVs
> to Selected** in the **STEPper NEXT: UV** panel.

The three unwrap modes are slower on large assemblies. Measured on a 182
part assembly, by share of surface area stretched more than twice as far one
way as the other: Conformal 0%, Angle Based 6%, Minimum Stretch 54%. The
Unwrap mode of 2.4 is Unwrap (Angle Based), and a file imported with it
refreshes the same way.

**Normalize UVs** is off by default. When it is off, the addon scales the UVs to real world scene units. One UV unit is then one scene unit on every island, so one shared material shows its texture at the same physical size on every part. This is what most CAD work needs.

When it is on, the addon fits the UVs to the 0-1 square. Every island of a part is divided by the same number, the largest island of that part. The islands keep their size against each other, so a 5 mm face gets a fifth of the UV length of a 25 mm face and both hold the same number of texels for each millimeter. Measured as the ratio between the densest and the sparsest island of a part: 1.0 in CAD Surfaces and in the unwrap modes.

**Closed surfaces** chooses what happens where a cylinder, cone, sphere or torus closes on itself. It applies to CAD Surfaces and the unwrap modes. CAD data marks no seam there, so an unwrap has nowhere to cut and returns a badly distorted island. None leaves it alone. Single seam, the default, cuts once, so a hole unrolls into one flat island and its two halves stay joined. Split faces cuts every boundary inside the closed region, so a hole made of two half cylinders becomes two islands.

Measured on a 182 part assembly, by share of surface stretched more than five times as far one way as the other: None 45%, Single seam 0.2%, Split faces 0.1%. Single seam reaches that with 2,958 islands against 3,381 for Split faces, so it wastes less texture on island margins. None of these change the shading.

A sheet with holes is not cut. A plate with a hole has the same topology as a length of pipe, so topology alone cannot tell them apart. The addon also walks each boundary loop and watches the surface normal. Round the end of a pipe it turns through a full circle, and that region gets its cut. Round a hole in a plate, or round the outline of a bent plate, it comes back without going round, and the region stays whole.

### CAD Surfaces (Smart)

Smart joins CAD faces that meet smoothly. A sheet metal part is a plate, a
bend and another plate. None of those boundaries is sharp, so the run is one
continuous surface that a press brake flattens into one rectangle.

Smart builds each island the way a paper model is cut out. CAD Surfaces
already gives each face a flat chart of its own. A plane chart is exact, and
a cylinder chart is the surface rolled out, circumference by height. So a
face joins the island without a solver: the addon turns its chart until the
shared edge lies on the island.

Some faces do not fit by a turn. A rounded rim is part of a torus, and its
chart misses the straight edge of a rolled out wall by a few percent. The
flat end of a tube is a ring, and it meets that edge along a circle. Smart
bends these faces instead. It measures each point of the face along the
shared edge and out from it, and sets the point down the same distance
along and out from the edge of the island. A rim becomes a strip along the
wall, and a ring unrolls into a strip. Where the edge of the island is cut,
as at the seam of a rolled out wall, the face is cut at the same place.

**Smart distortion** limits the bend, as an average over the face. The
default is 35%. Squash counts in full, because it lowers the texel density.
Stretch along the shared edge counts half, because an unrolled ring is still
a clean strip that is easy to texture. The ends of a tube 60 mm across with
a 30 mm bore score about 27%. A full disk would have to stretch without
limit at its middle, so it stays an island of its own. Set 0 to turn the
bend off.

Smart starts from the largest face of the part and tries its smooth
neighbors, largest first. A face joins only when all of these are true:

- It fits the island by a turn, or by a bend within the Smart distortion
  limit.
- It does not land on the island. A bent face must not land on itself
  either.
- The island still fits the UV tile.

A face that fails is tried again when another of its neighbors joins the
island, because that edge can fit better. A face that never fits waits for
a later island. The next island starts from the largest face that is left,
until no face is left.

**Join sharp edges** runs a second pass after the smooth one. It joins the
islands of the first pass across sharp edges, with the same tests. A join
across a fold is the same turn as a join across a smooth edge, because each
chart is flat already. A gearbox or a machined part has faces with sharp
edges all round, so without this option most of them stay islands of their
own. The option is off by default, because it adds about 25 percent to a
large import and does not always pack tighter.

Last, Smart cuts an island that has grown into an awkward shape. Every
island is a tree of faces, joined one at a time, so it can be cut along any
join, and each piece keeps its layout. A net shaped like a V, or with a long
arm out at an angle, leaves most of the rectangle round it empty. Smart
tries each join and makes the cut that makes the rectangles of the pieces
smallest, if that saves at least 10 percent. The margin the packer leaves
round each island counts too, so Smart does not cut off small pieces. Then
it tries the pieces again. **Optimize island shape** controls this cut. It
is on by default. Clear it to keep every island as large as it grew.

The tile comes from **Pack UVs**. With **None** or **Each part on its own**,
the tile is the square the part packs into by itself. With **All parts
together**, it is one tile for the whole import, so an island can grow much
longer. **Into UDIM tiles** shares the import over the tiles you ask for. A
tile is never smaller than the longest single face, because the addon never
splits a CAD face.

Smart makes its own cuts, so the **Closed surfaces** setting does not apply
to it. A turn does not change the texel density, and a bend changes it only
within the limit.

Measured results, with no island overlapping itself in any of them.
Coverage is the share of the UV tile the islands fill after Blender packs
each part into its own tile, so higher is better texel density:

| Part | CAD Surfaces | Smart | Smart, Join sharp edges |
|------|--------------|-------|-------------------------|
| Cylinder 60 mm across, both rims rounded | 5 islands, 56% | 3 islands, 57% | 3 islands, 57% |
| The same with a 30 mm bore | 6 islands, 73% | 2 islands, 79% | 2 islands, 79% |
| Plate and boss, rounded all round, with a bore | 31 islands, 58% | 3 islands, 86% | 3 islands, 86% |
| Block with rounded edges | 26 islands, 88% | 4 islands, 75% | 4 islands, 75% |
| Hydraulic part | 138 islands, 58% | 122 islands, 59% | 30 islands, 62% |
| Buoyancy module | 171 islands, 70% | 56 islands, 72% | 40 islands, 72% |
| Gear motor, 10 parts | 2,282 islands, 43% | 1,899 islands, 43% | 507 islands, 51% |
| Machined assembly, 182 parts | 440 islands, 57% | 340 islands, 57% | 151 islands, 53% |
| Sheet metal skid, 1,113 parts | 3,032 islands, 51% | 1,428 islands, 51% | 692 islands, 51% |

Small flat faces pack very tightly on their own. That is why CAD Surfaces
fills the tile best on the block with rounded edges, which is 26 plain
faces, while Smart gives four clean islands.

On the sheet metal part, Smart cuts the flat patterns at a bend. They
measure 3,338 by 977 mm, and the part packs into a 2,419 mm square on its
own. Kept whole, they would force a 3,425 mm tile and fill at most 35
percent of it. Cut, they give about twice the texel density. Set **Pack
UVs** to **All parts together** to keep them whole.

Smart adds its own pass to the import: about 35% on the sheet metal skid,
and about 40% on the machined assembly. Join sharp edges adds about 25% more
on the skid.

### Pack UVs

**Pack UVs** repacks the islands after the UV map is made. Without it the
islands keep the place the UV mode gave them, which spreads a large assembly
over many UDIM tiles and leaves most of the texture empty. Measured on 1000
parts: no packing puts the islands across 147 tiles and fills about 1 percent
of them.

| Mode | Result on 1000 parts | Cost |
|------|----------------------|------|
| **None** | 147 tiles, 1% filled | none |
| **All parts together** | 1 tile, 54% filled | +2% |
| **Into UDIM tiles** (4) | 4 tiles, 68% filled | +3% |
| **Into UDIM tiles** (16) | 16 tiles, 76% filled | +6% |
| **Each part on its own** | one tile for each part, 74% filled | +86% |

Use **All parts together** to merge the parts and texture the import as one
piece. Use **Each part on its own** to give every part its own texture: it is
the slow choice, because the packer has to run once for each part. Use **Into
UDIM tiles** for a middle way, and set how many tiles to spread the parts
over. The parts are shared out by surface area, so each tile carries a
similar amount.

The UVs are always for a square texture. Blender's own Pack Islands and
Unwrap read the image texture in the material and fit the islands to its
proportions, so on a 2 by 1 texture a square face becomes a thin rectangle.
The addon hides the materials from these operators while they run, and puts
them back after. If you pack by hand in the UV Editor, Blender still does
this on a textured part.

**Pack margin** is the space left around each island. Blender puts this
around every island, and a CAD part has about one island per face, so a whole
assembly in one tile is thousands of islands. The addon divides the margin
down as more parts share a tile. Without that correction the default margin
fills only 6 percent of the tile on a 300 part import, against 80 percent
with it. Raise the margin if a bake bleeds between islands.

**Normalize UVs** decides whether the packer may resize the islands. With
it on the islands are scaled to fill the tile. With it off the packer only
arranges them and every island keeps its real world size, so the packed
result can be larger than one tile. UDIM tiles always resize, because their
grid is fixed and an island that overruns one tile lands in the next.

Where the packer may resize, it runs Average Islands Scale first. The packer
applies one factor to the whole group, so on its own it keeps whatever size
difference the islands arrive with. Averaging first gives every island in
the group the same texel density. Box Project needs this most, because it
flattens each face onto one of three planes and a face at an angle to all
three arrives compressed.

### Tris to Quads

**Tris to Quads** is on by default. It pairs the tessellation triangles back
into quads. OCCT tessellates to triangles, so a flat CAD face arrives as thin
triangle pairs that go straight back together. It never joins across a
material, a UV island, a seam or a sharp edge.

This is not a remesh. No vertex moves and none is added or lost, and the
custom split normals from the CAD surface come through, so the shading does
not change. On 1000 parts it costs about 2 percent of the import and takes
the face count from 540,000 to 285,000.

### The UV panel

The **STEPper NEXT: UV** panel in the sidebar makes the UV map of the
selected parts again. It holds the same settings as the import dialog, so
one part can get a treatment its neighbor does not. A bent bracket can be
one flat pattern while the machined block beside it stays face by face.

Box Project reads the mesh and nothing else, so it runs on the parts as they
are. The other modes need the parametric surfaces, so the addon reads the
source CAD file again and replaces the mesh, the same way **Regenerate**
does. Work you did on the mesh itself does not survive that. Transforms,
parenting, modifiers, materials and custom properties do.

The settings go on to each object. A **Regenerate** or a **Refresh from
disk** later makes the UV map you chose in the panel, not the one the import
made.

## Import Defaults

**Remember import settings** is on by default. The addon saves the import dialog options after every import and restores them in your next Blender session. Blender writes them out with its normal preferences save, so keep *Save Preferences on Quit* on. You can also save the preferences by hand. Turn the option off to use the fixed defaults in the preferences instead.

## Staying Up To Date

You install STEPper NEXT from a zip and not from extensions.blender.org, because it ships precompiled binaries. Blender therefore does not update it for you. The addon asks GitHub once a day whether a newer release exists. If there is one, it shows a notice at the top of the **STEPper NEXT** sidebar tab. The notice has a download link for your platform. Install the downloaded zip the same way as the first time, and Blender replaces the old version.

The check sends no information about you or your files, and runs on a background thread so it never delays startup. Turn it off with **Check for updates** in the addon preferences.

## Version History

| Version | Blender | Changes |
|---------|---------|---------|
| 2.5.0   | 5.1     | The UV release. CAD Surfaces UVs now carry the proportions of the surface, so a cylinder no longer arrives as a thin tall ribbon. Patches of one surface share one island, so a drilled hole is one tube and not three. Two CAD faces no longer share one folded island. Every island of a part holds the same number of texels for each millimeter of surface. The UV Map dropdown gains CAD Surfaces (Smart), which unfolds a bent sheet metal part into its flat pattern and a rounded tube into two islands, and one mode for each unwrap method. New import options: Pack UVs with a margin and a UDIM tile count, and Tris to Quads (on by default). A new UV panel in the sidebar makes the UV map of the selected parts again, so one part can get a treatment its neighbor does not. "Split Closed Faces" becomes "Closed surfaces" with a Single seam choice, which is the new default. The sidebar tab now sits after Item, Tool and View |
| 2.4.7   | 5.1     | Imported files panel with Refresh from disk. A refresh keeps your modifiers, collections, parenting, materials and placement, and can no longer change the size of the assembly. The object color now matches the CAD color. New "Group in a collection" and "Separate solids" import options. Material databases can live in a folder of your choosing. Update notice and Ko-fi link in the sidebar. The file cache checks the file on disk, so re-exporting over the same path no longer imports old geometry |
| 2.4.6   | 5.1     | Fixed engineering materials always being created gray instead of keeping the part's imported color |
| 2.4.5   | 5.1     | Engineering material import (AP242/AP214 name, description, density) as custom properties and optional named materials. Single `UVMap` layer with a UV mode dropdown, real-world UV scaling and automatic seams on closed/cylindrical faces. Import options remembered between sessions. Parented-empties imports now leave everything at scale 1. Recursive folder batch import. Multi-file drag & drop fix |
| 2.4.4   | 5.1     | Code-review hardening: multi-level Prune/Restore, detail slider no longer overridden by the quality preset, batch import honours preference defaults, Regenerate/Reload fixed for IGES/BREP, background worker inherits preferences and cannot hang or outlive Blender, long CAD material names, edit-mode guards, tooltip and layout polish |
| 2.4.3   | 5.1     | Non-blocking background import (worker process, live progress, Esc to cancel) with a size threshold. Identical output to direct import |
| 2.4.2   | 5.1     | IGES + BREP import, sketch/construction curves import, relative (adaptive) tessellation, pre-import analyzer with import-time estimates |
| 2.4.1   | 5.1     | SurfaceUV + BoxUV layers, viewport drag & drop, folder batch import, state-preserving Regenerate, Prune/Restore hierarchy tools, mesh cleanup |
| 2.4.0   | 5.1     | Quality presets with unit-aware deflection (physical mm in any unit system), collection-instances hierarchy mode, construction-geometry filters, per-instance color overrides, modern import dialog |
| 2.3.0   | 5.1     | Migrated OpenCASCADE bindings from pythonocc-core to OCP (cadquery-ocp-novtk 7.9.3.1.1). Converted to Blender extension format with per-platform OCP wheels. Added macOS Intel support. Native mesh extraction reworked to a serialize handoff |
| 2.2.0   | 5.1     | Material database system for automatic material replacement, fixed apply-scale on instanced/multi-user meshes |
| 2.1.3   | 5.1     | Renamed to STEPper NEXT, auto-apply scale, skip empty objects, preferences now persist across sessions |
| 2.1.x   | 5.1     | Multithreaded normal computation, performance optimizations, crash fixes for corrupt STEP files |
| 2.1.0   | 5.1     | Updated to pythonocc-core 7.9.3 / Python 3.13, native C++ mesh extraction |
| 2.0.0   | 5.0     | Ported to Blender 5.0 API, added import diagnostics and failed parts reporting |
| 1.1.8   | 4.2.1   | Last release by ambi |

## Support

This addon is free and open source under the GPL v3 license.

**ambi**, original creator: https://ambient.gumroad.com/l/stepper

**Peak Design**, current maintainer. Tips welcome:

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/oskarasspalvys)

## For Developers

The OpenCASCADE (OCP) bindings come from the [cadquery-ocp-novtk](https://pypi.org/project/cadquery-ocp-novtk/) wheels in `wheels/`, one per platform. `blender_manifest.toml` lists them, and Blender installs the matching one at extension install time. A small native mesh extraction module (`native/`) ships per platform with its own plain named OCCT subset in `native_libs/`. It talks to the importer through a BinTools serialize handoff, so it does not depend on the Python bindings. GitHub Actions builds the per platform extension zips with `.github/workflows/release.yml`. The workflow narrows the manifest to one platform and wheel per zip with `ci/make_platform_manifest.py`, then runs `ci/smoke_test.py` before it packages the addon.

The legacy addon path in `scripts/addons` still works through the retained `bl_info`. Extract the Windows wheel into the addon folder so `import OCP` resolves: `python -m zipfile -e wheels/cadquery_ocp_novtk-*-win_amd64.whl .`. The extracted `OCP/` and `cadquery_ocp_novtk.libs/` folders are gitignored.

Parity testing: `blender -b --factory-startup --python ci/parity_harness.py -- <file.step> <out.json> ['<operator_kwargs_json>']` writes a deterministic scene snapshot for diffing. The snapshot holds objects, mesh counts, materials, transforms and collections. Reference snapshots live in `ci/baselines/`, with the self contained `mat_ap242.step` and `mat_ap214.step` fixtures that test engineering material import. The other baselines point at local corpus files by absolute path. Treat those as a change detector and not as a portable test suite.

Note: on Windows the addon must not sit under a path longer than about 250 characters. The bundled OpenCASCADE DLLs fail to load if it does. The default Blender paths are fine.

## License

This program is free software under the [GNU General Public License v3](https://www.gnu.org/licenses/gpl-3.0.html).

Copyright 2021 Tommi Hyppanen
Modified 2026 by Peak-Design
