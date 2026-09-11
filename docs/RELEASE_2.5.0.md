# STEPper NEXT 2.5.0

The UV release. Blender 5.1 or later. Install the zip for your platform the
same way as the first time.

## CAD Surfaces is the UV mode to use

CAD Surfaces takes the UVs from the parametric coordinates of the surface
while the part is tessellated. No solver runs, so the UVs cost almost
nothing and the mode is the fastest of them all. A parametric surface also
maps to a flat sheet exactly, so the islands come out clean: a cylinder
unrolls to its circumference by its height and a plane stays a plane. Unwrap
has to flatten a mesh that is already triangles, which takes longer and
leaves some stretch behind.

## New import options

| Option | What it does | Cost |
|--------|--------------|------|
| **UV Map** | One dropdown for every way to make the UVs: None, CAD Surfaces, CAD Surfaces (Smart), Unwrap (Conformal), Unwrap (Angle Based), Unwrap (Minimum Stretch) and Box Project. The old Unwrap mode is now Unwrap (Angle Based) | none |
| **CAD Surfaces (Smart)** | New UV Map mode. Joins faces that meet smoothly into larger islands, bends a face to fit where it has to, and cuts an island where its pieces pack better. A bent sheet metal part comes out as its flat pattern, a rounded tube as two islands | +35% |
| **Smart distortion** | How far Smart can bend a face to join it, 35% by default. 0 turns the bend off | none |
| **Join sharp edges** | Smart joins across sharp edges too, after the smooth ones. A gear motor goes from 1,899 islands to 507. Off by default | +25% |
| **Optimize island shape** | Smart cuts an island along a join where the pieces pack better, such as a V shape or a long arm. On by default. Clear it to keep every island whole | none |
| **Tris to Quads** | Pairs the tessellation triangles back into quads. No vertex moves and the CAD shading is unchanged. On by default | +2% |
| **Pack UVs** | Arranges the islands into one tile, one tile for each part, or a set number of UDIM tiles | +2% to +6%, per part +86% |
| **Pack margin** | The space around each island. Raise it if a bake bleeds | none |
| **Closed surfaces** | Where a cylinder or torus closes on itself, for CAD Surfaces and the unwrap modes: None, Single seam (new default) or Split faces. Replaces "Split Closed Faces" | none |

## Fixes

- A cylinder arrived as a thin tall ribbon. CAD Surfaces UVs now carry the
  proportions of the surface.
- A drilled hole came out as two or three islands. The patches of one
  surface now share one island.
- Two flat faces of the same size could share one folded island. Each face
  now takes a small offset of its own.
- Islands of one part could hold different texel densities. Every island of
  a part now divides by the same number.
- Closed surfaces cut the bend lines of sheet metal parts with holes. A
  plate with a hole has the same topology as a pipe. The addon now also
  checks whether the surface goes all the way round, and cuts only a pipe.
- Clean Up Meshes turned the shading normals and left marks on curved
  faces. It now keeps the shading of the CAD surface.
- A part made of two bodies that touch, such as a foam core in a skin,
  lost triangles where the bodies meet, and faces with a color of their own
  could come out twice, in the wrong color and facing in. Each body now
  stays whole, and each face takes its own color once.
- A background import failed when a part of the file produced no geometry.
  The worker tried to show the warning popup, and a Blender with no window
  crashes when it does that. The worker now sends the warning to your
  session, which shows the popup.

## New UV panel

**STEPper NEXT: UV** in the sidebar makes the UV map of the selected parts
again, with the same settings as the import dialog. One part can then get a
treatment its neighbor does not: a bent bracket as one flat pattern, the
block beside it face by face. It replaces the Box Project UVs button, which
is now one mode of the dropdown.

Box Project runs on the mesh as it is. The other modes read the source CAD
file again and replace the mesh, the same way Regenerate does. The settings
go on to each object, so a later Regenerate or Refresh keeps them.

## Interface

The STEPper NEXT sidebar tab now sits after Item, Tool and View.
