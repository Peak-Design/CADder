# CADder 1.0.1

Fixes for two reports on 1.0.0. Blender 5.1 or later.

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

## Update CADder Bridge too

Install CADder Bridge 1.0.1 in SolidWorks as well. The new bridge tells
Blender where each body of a part starts, so two bodies that touch stay
two separate bodies. CADder 1.0.1 also works with Bridge 1.0.0. With the
older bridge, a part whose bodies touch arrives unjoined, as it did
before.

A send can still have more vertices than a STEP import of the same
assembly. The bridge tessellates at the quality set in **Export Options**
in SolidWorks, which can be finer than the default of the import dialog.
