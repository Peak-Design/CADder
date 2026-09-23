# CADder 1.1.0

Lock Materials and Lock Geometry, a Material Database that is easier to
work with, and a Refresh Model that keeps your rig. Blender 5.1 or later.
Install the zip for your platform the same way as the first time.

## New

- **Lock Materials.** Select parts and lock their materials. The material
  database, Refresh Model, Send to Blender, Regenerate and Refresh from
  Disk then leave them as they are.
- **Lock Geometry.** Select parts and click the lock beside Rebuild from
  CAD. Rebuild from CAD, Refresh Model, Send to Blender, Regenerate and
  Refresh from Disk then keep their meshes and your changes to them. The
  parts still move to new poses.
- **An easier Material Database list.** The eye hides the entries that no
  part of the scene uses. Each entry has a button that selects its parts,
  and a trash that removes it. Save shows an asterisk while the list has
  changes that are not saved.
- **Load keeps the materials of the database in the file.** Pick them for
  the entry of another material, with no search through your libraries.
- **Refresh Model keeps your rig.** The parts go back on the SolidWorks
  pose before they are bound again. Your handles, mechanism inputs,
  drivers and bones of your own stay on the rig.
- **Refresh Model is offered only when it can work.** The button in
  SolidWorks stays gray until a running Blender holds a scene of the
  document, from a send or from a saved file opened again.
- **The SolidWorks Bridge is in the Windows version only.** SolidWorks
  runs only on Windows, so the macOS and Linux versions show none of the
  link.
- **A background import lands at the 3D cursor,** the same as an import
  in the foreground, and each STEP import is one undo step.

## Improvements and bug fixes

- **Automatic rig engine:** improvements and bug fixes. More mechanisms
  move as they do in SolidWorks: balls, universal joints, rack and pinion,
  screws, cams, paths, mirrored pairs and closed loops. Join Rigs is more
  reliable.
- **Refresh Model and Rebuild from CAD:** improvements and bug fixes to
  how parts are found, placed and kept, also for collection instances,
  copies made in Blender, subassemblies and a changed Unit Scale.
- **STEP, IGES and BREP import:** improvements and bug fixes. More damaged
  files import, and Separate Solids keeps surface bodies and face colors.
- **Refresh from Disk:** improvements and bug fixes. Defeatured parts,
  your copies of a part and your moves stay.
- **Background import:** improvements and bug fixes.
- **Mesh Quality, Regenerate and UVs:** improvements and bug fixes.
- **Materials and appearances:** improvements and bug fixes to decals,
  textures and the material database.
- **Live link:** a steadier connection to SolidWorks while it is busy.
- **Update check:** it does not run while online access is off in the
  Blender preferences.

## Update CADder Bridge too

Install [CADder Bridge 1.1.0](https://github.com/Peak-Design/CADder-SW-Bridge/releases/latest)
in SolidWorks with this release. It has the SolidWorks half of the rig
engine improvements. The new rule for when Refresh Model is offered needs
both halves at 1.1.0.
