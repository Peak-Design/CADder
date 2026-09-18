# CADder 1.0.1

Fixes for the first reports on 1.0.0. Blender 5.1 or later.

## One set of quality settings

The import dialog, the Mesh Quality panel and Export Options in SolidWorks
now show the same five settings, and a name cuts a part the same way on
every route:

- **Quality**: Draft, Balanced, Fine, Ultra or Custom. "Ultra Fine" is
  now "Ultra", as it is in SolidWorks.
- **Distance** and **Angle**: what Custom cuts to. Distance is a real
  length in every place. The Linear value of Mesh Quality was in the
  units of the file.
- **Relative Tessellation** and **Relative Distance**: cut to a share of
  the size of each feature. This is now on the SolidWorks route too.

The Mesh Quality panel has one set of these for parts from a file and
parts from SolidWorks alike. **Artist-Friendly Parameters** and the **Mesh
Detail** number are gone: the four names are the simple way to choose.

## Fixes

- **A part sent from SolidWorks is one connected mesh.** SolidWorks gives
  each face of a part its own copy of the points along its edges, and the
  bridge kept those copies. A part came in as a set of loose faces, with
  two to six times the vertices it needed. The copies are now joined when
  the part arrives, as a STEP import joins them. The shape, the shading
  and the UVs do not change. The CAD edges stay marked: sharp where the
  faces meet at an angle, and a UV seam where the UV map is cut.
  ([#2](https://github.com/Peak-Design/CADder/issues/2))
- **Empties are sized to the assembly.** An import drew every empty 2 m
  across, so a small assembly disappeared under long lines. Each empty is
  now a tenth of the size of the parts under it. This applies to STEP,
  IGES and BREP imports and to a send from SolidWorks.
  ([#1](https://github.com/Peak-Design/CADder/issues/1))
- **A subassembly keeps its empties under the rig.** On a send with
  parented empties, the rig took every part out of its subassembly and
  left the empties behind with nothing in them. A subassembly that moves
  as one body now hangs from its bone with its parts still under it. The
  empty of a subassembly whose parts move apart is removed, because it
  can no longer hold them.

## Update CADder Bridge too

Install CADder Bridge 1.0.1 in SolidWorks as well:

- **Draft, Balanced, Fine and Ultra cut a part as a STEP import does.**
  Each name now gives the same distance from the true surface and the same
  angle as the preset of that name in the import dialog. Draft on the
  engine of issue #2 goes from about 260,000 vertices to 134,000.
- **Custom and Relative Tessellation in Export Options**, with the same
  meaning as in Blender. Rebuild from CAD sends the Mesh Quality settings,
  and the bridge cuts to them.
- The bridge tells Blender where each body of a part starts, so two bodies
  that touch stay two separate bodies.

CADder 1.0.1 also works with Bridge 1.0.0. With the older bridge, the
quality names keep their old meaning, Custom and Relative Tessellation
fall back to Balanced for Rebuild from CAD, and a part whose bodies touch
arrives unjoined, as it did before.
