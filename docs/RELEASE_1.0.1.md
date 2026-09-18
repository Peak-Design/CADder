# CADder 1.0.1

Fixes for the first reports on 1.0.0. Blender 5.1 or later.

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
  engine of issue #2 goes from about 260,000 vertices to 134,000. The
  Custom value in Mesh Quality now goes coarser than Draft.
- The bridge tells Blender where each body of a part starts, so two bodies
  that touch stay two separate bodies.

CADder 1.0.1 also works with Bridge 1.0.0. With the older bridge, the
quality names keep their old meaning, and a part whose bodies touch
arrives unjoined, as it did before.
