# CADder 1.0.1

Fixes for the first reports on 1.0.0. Blender 5.1 or later.

## Fixed

- **Parts from SolidWorks are one connected mesh.** They came in as loose
  faces, with two to six times the vertices they needed.
  ([#2](https://github.com/Peak-Design/CADder/issues/2))
- **Empties are sized to the parts under them.** They were 2 m across
  whatever the size of the assembly.
  ([#1](https://github.com/Peak-Design/CADder/issues/1))
- **Subassemblies keep their empties under the rig.** A subassembly that
  moves as one body hangs from its bone with its parts still under it. An
  empty left with nothing in it is removed.
- **A relative import stays relative** when you regenerate or refresh it.

## Changed

- **One set of quality settings everywhere.** The import dialog, the Mesh
  Quality panel and Export Options in SolidWorks all show Quality (Draft,
  Balanced, Fine, Ultra or Custom), Distance, Angle, Relative
  Tessellation and Relative Distance. A name gives the same numbers on
  every route.
- **Artist-Friendly Parameters and Mesh Detail are removed.** The four
  quality names are the simple choice. "Ultra Fine" is now "Ultra".

## Update CADder Bridge too

[CADder Bridge 1.0.1](https://github.com/Peak-Design/CADder-SW-Bridge/releases/latest)
cuts parts to the same numbers as a STEP import, so a Draft send has about
half the vertices it had. CADder 1.0.1 also works with Bridge 1.0.0, but
the new quality settings need Bridge 1.0.1.
