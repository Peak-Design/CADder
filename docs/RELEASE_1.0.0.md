# CADder 1.0.0

The first release under the new name, and the first with the live link to
SolidWorks. Blender 5.1 or later.

## Coming from STEPper NEXT

STEPper NEXT is now CADder. It is a separate install, not an update:

1. Remove **STEPper NEXT** in **Edit > Preferences > Add-ons**, and restart
   Blender.
2. Install the CADder zip for your platform.
3. Set your preferences again, for example the material database folder.
   Blender stores preferences by add-on name, so they do not carry over.

## SolidWorks to Blender

Install [CADder Bridge](https://github.com/Peak-Design/CADder-SW-Bridge/releases/latest)
in SolidWorks, then tick **SolidWorks Bridge** in the CADder preferences.

- **Send to Blender**, from the SolidWorks ribbon, sends the open assembly
  into Blender: geometry, appearances, decals, the tree and a rig. Blender
  starts if it is not running.
- **Refresh Model** brings the scene up to date part by part. New parts
  arrive, deleted parts go, and every part that stayed keeps its object,
  materials and modifiers. A renamed assembly is still recognized, so a
  new revision letter does not cost you the scene.
- **Rebuild from CAD**, in Blender, asks SolidWorks for the selected parts
  again at another quality. Press F9 to choose what comes back: the
  geometry, the geometry and the poses, the poses only, a refresh of the
  whole assembly, or a full reimport.
- **Match the Blender view** turns the viewport to the angle of the
  SolidWorks view after a send. Off by default.
- The link listens on this computer only, with a token from your own app
  data. A line under the CADder title says whether it is waiting or
  active.

## Rig generation

- The mates become an armature. Components with no freedom between them
  merge into one bone, and what is left becomes a joint: revolute,
  prismatic, cylindrical, ball, planar, pin slot, screw, path, surface or
  free.
- Limit mates become constraints, drawn to the real values: an arc for
  the angle a joint may turn, a rail for its travel.
- Gears, rack and pinion, screws, cams and symmetry drive each other.
  Closed loops hold together, and a universal joint built from its parts
  turns exactly, with no IK.
- A mechanism that can be driven from more than one place asks which, and
  rebuilds for the answer.
- **Join Rigs** puts a subassembly's rig inside a machine's.

## Mesh Quality

One panel at the top of the CADder tab, for every part, whether it came
from the bridge, a STEP file or an IGES file. The button says where it
reads from: Rebuild from CAD, from STEP or from IGES. The default quality
is now Balanced.

- **Triangles to Quads**, now on a send as well as on a file import.
- **Defeature** leaves small holes and cutouts out of the mesh. Set it on
  a part or on a collection. Nothing in your CAD document changes.
- **Clean Up Meshes** moved here, and works on parts from either route.

## UV maps

- **CAD Surfaces (Smart)** now hands the faces that one scale cannot
  flatten, such as a sphere, a torus or a spline, to Blender's own unwrap,
  and keeps the result only where it is better. No more long thin
  ribbons.
- **Unwrap Compound Surfaces** does the same on a send from SolidWorks.

## Fixes

- A send could stop on an appearance whose file wrote its values with a
  comma after them.
- Rebuilding one selected part of a large assembly could rebuild hundreds
  of others. It now asks SolidWorks for that part only, and the CADder
  panels are much faster on a large assembly.
- The Defeature switch went grey after Apply Defeature on a collection.

## Known limits

- A universal joint **mate** turns 1:1. A real one leads and lags within
  a turn, so the timing is approximate. A universal joint built from its
  parts is exact.
- Spherical and cylindrical texture mappings from SolidWorks are
  approximate. Planar and automatic match.
- macOS and Linux builds are made by CI and have not been tested.
