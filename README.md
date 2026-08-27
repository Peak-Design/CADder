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
- Engineering material metadata (AP242/AP214 name, description, density) imported as custom properties, and optionally as named Blender materials
- Material database system for automatic material replacement on import
- UV generation from CAD surfaces, angle-based unwrap, or box projection, with optional real-world UV scale and automatic seams on cylindrical/closed faces

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
file again and puts the arrangement back around it:

- objects stay in the collections you put them in, including a part
  ctrl-dragged into a second collection so it lives in two places at once;
- the import's own collections go back inside whatever collection you moved
  them into;
- objects of your own that you parented to a part are re-attached to it;
- what you hid stays hidden.

Components that have gone from the file, or are new in it, are reported rather
than guessed at: that is the signal that the assembly itself changed, not just
its geometry. The settings a file was imported with are remembered per file, so
a refresh reproduces the import rather than using whatever the dialog holds at
the time.

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
| **CAD Surface** | One island per CAD face, taken from the parametric surface coordinates. Fast, and the default. |
| **Unwrap** | Blender's angle-based unwrap with packed islands; sharp CAD edges act as seams. Slower on large assemblies. |
| **Box Project** | Triplanar projection with a world-unit tile size. |
| **None** | No UV layer. |

**Normalize UVs** is off by default. When it is on, the addon fits the UVs to the 0-1 square. When it is off, the addon scales the UVs to real world scene units instead. The islands stay packed and the addon rescales them together. One shared material then shows its texture at the same physical size on every part. This is what most CAD work needs.

**Split Closed Faces** is on by default. It marks a UV seam along the closure of cylinders, cones and tori. It also marks one across smooth joined face groups that form closed tubes or rings. CAD data has no seam in these places, and an unwrap without one gives badly distorted islands. This does not change the shading.

## Import Defaults

**Remember import settings** is on by default. The addon saves the import dialog options after every import and restores them in your next Blender session. Blender writes them out with its normal preferences save, so keep *Save Preferences on Quit* on. You can also save the preferences by hand. Turn the option off to use the fixed defaults in the preferences instead.

## Staying Up To Date

You install STEPper NEXT from a zip and not from extensions.blender.org, because it ships precompiled binaries. Blender therefore does not update it for you. The addon asks GitHub once a day whether a newer release exists. If there is one, it shows a notice at the top of the **STEPper NEXT** sidebar tab. The notice has a download link for your platform. Install the downloaded zip the same way as the first time, and Blender replaces the old version.

The check sends no information about you or your files, and runs on a background thread so it never delays startup. Turn it off with **Check for updates** in the addon preferences.

## Version History

| Version | Blender | Changes |
|---------|---------|---------|
| 2.4.6   | 5.1     | Fixed engineering materials always being created grey instead of keeping the part's imported colour |
| 2.4.5   | 5.1     | Engineering material import (AP242/AP214 name, description, density) as custom properties and optional named materials; single `UVMap` layer with a UV mode dropdown, real-world UV scaling and automatic seams on closed/cylindrical faces; import options remembered between sessions; parented-empties imports now leave everything at scale 1; recursive folder batch import; multi-file drag & drop fix |
| 2.4.4   | 5.1     | Code-review hardening: multi-level Prune/Restore, detail slider no longer overridden by the quality preset, batch import honours preference defaults, Regenerate/Reload fixed for IGES/BREP, background worker inherits preferences and cannot hang or outlive Blender, long CAD material names, edit-mode guards, tooltip and layout polish |
| 2.4.3   | 5.1     | Non-blocking background import (worker process, live progress, Esc to cancel) with a size threshold; identical output to direct import |
| 2.4.2   | 5.1     | IGES + BREP import, sketch/construction curves import, relative (adaptive) tessellation, pre-import analyzer with import-time estimates |
| 2.4.1   | 5.1     | SurfaceUV + BoxUV layers, viewport drag & drop, folder batch import, state-preserving Regenerate, Prune/Restore hierarchy tools, mesh cleanup |
| 2.4.0   | 5.1     | Quality presets with unit-aware deflection (physical mm in any unit system), collection-instances hierarchy mode, construction-geometry filters, per-instance color overrides, modern import dialog |
| 2.3.0   | 5.1     | Migrated OpenCASCADE bindings from pythonocc-core to OCP (cadquery-ocp-novtk 7.9.3.1.1); converted to Blender extension format with per-platform OCP wheels; added macOS Intel support; native mesh extraction reworked to a serialize handoff |
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
