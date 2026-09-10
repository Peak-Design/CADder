# STEPper NEXT 2.5.0

The UV release. Blender 5.1 or later. Install the zip for your platform the
same way as the first time.

## CAD Surface is the UV mode to use

CAD Surface takes the UVs from the parametric coordinates of the surface
while the part is tessellated. No solver runs, so the UVs cost almost
nothing and the mode is the fastest of the three. A parametric surface also
maps to a flat sheet exactly, so the islands come out clean: a cylinder
unrolls to its circumference by its height and a plane stays a plane. Unwrap
has to flatten a mesh that is already triangles, which takes longer and
leaves some stretch behind.

## New import options

| Option | What it does | Cost |
|--------|--------------|------|
| **Tris to Quads** | Pairs the tessellation triangles back into quads. No vertex moves and the CAD shading is unchanged. On by default | +2% |
| **Pack UVs** | Arranges the islands into one tile, one tile for each part, or a set number of UDIM tiles | +2% to +6%, per part +86% |
| **Pack margin** | The space around each island. Raise it if a bake bleeds | none |
| **Unwrap method** | Conformal (new default), Angle Based or Minimum Stretch | none |
| **Closed surfaces** | Where a cylinder or torus closes on itself: None, Single seam (new default) or Split faces. Replaces "Split Closed Faces" | none |

## UV fixes

- A cylinder arrived as a thin tall ribbon. CAD Surface UVs now carry the
  proportions of the surface.
- A drilled hole came out as two or three islands. The patches of one
  surface now share one island.
- Two flat faces of the same size could share one folded island. Each face
  now takes a small offset of its own.
- Islands of one part could hold different texel densities. Every island of
  a part now divides by the same number.

## Interface

The STEPper NEXT sidebar tab now sits after Item, Tool and View.
