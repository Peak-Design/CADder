# CADder 1.2.0

Configurations. Each configuration of an assembly now comes into Blender
as a send of its own, with its own collection and its own rig, beside the
others. Blender 5.1 or later. Install the zip for your platform the same
way as the first time.

CADder 1.2 works with CADder Bridge 1.2. Update both. See
[Which CADder Bridge works with this CADder](https://github.com/Peak-Design/CADder/blob/main/docs/GUIDE.md#which-cadder-bridge-works-with-this-cadder).

## New

- **Configurations side by side.** A send puts the assembly in a
  collection named after the assembly and its configuration,
  `<assembly>_<configuration>`. In it are the parts,
  `<assembly>_<configuration>_Parts`, and the rig,
  `<assembly>_<configuration>_Rig`. Switch to another configuration in
  SolidWorks and send again, and the scene holds both.
- **Send several configurations at once.** Select **Multiple
  configurations** under **Send to Blender** in the Export Options of
  CADder Bridge. **Send to Blender** then asks which configurations to
  send. SolidWorks shows each one in turn, and shows the configuration you
  had active again at the end.
- **Linked parts.** A part that is already in the scene is not made
  again. A part with the same shape and the same appearance as a part of
  any send in the scene, another configuration or another assembly, uses
  the mesh that is there. A part with another shape or another appearance
  gets a mesh of its own. **Link identical parts** in the Export Options of
  CADder Bridge turns it off.
- **Copies.** Select **Append as a new copy** in the Export Options of
  CADder Bridge, and a send puts the assembly in the scene again, beside
  the send that is there: `<assembly>_<configuration>.001`, with its own
  collection and rig, linked to the parts of the first. **Refresh Model**
  brings the send and its copies up to date.
- **A send replaces only its own configuration.** Other configurations,
  and other assemblies, stay in the scene with their rigs. Up to 1.1 a
  send removed every other send in the scene.
- **Refresh Model asks which configurations to refresh** when Blender
  holds more than one configuration of the document. The configurations
  Blender holds are selected.
- **Rebuild from CAD knows the configuration.** It asks SolidWorks for
  each configuration on its own, and puts each answer only on the parts of
  that configuration. So do Defeature and a pose push from SolidWorks.
- **A list of the rigs.** When the scene holds more than one rig from
  SolidWorks, the **Rig** panel shows a **Rig** list. Choose a rig to set
  the **Mechanism Input** of its mechanisms.

## Changes

- The top collection of a send is `<assembly>_<configuration>`, and no
  longer `<assembly>_Top_Level`. The first send from 1.2 of an assembly
  takes over the send from 1.1 in the scene, with your collections, your
  objects and your locked materials.
- The names of the parts inside the parts collection are the same in each
  configuration, so Blender puts a number on the end of the names in the
  second and later configurations.
- **The bone widgets show the motion.** A control shows the freedoms of its
  joint and nothing else: a curved double arrow for a turn, a band bent
  round the axis, and a straight double arrow for a slide, one flat strip.
  A slider was a box and a cylindrical joint a cylinder. Now a slider is an
  arrow, and a cylindrical joint is a turn and an arrow. A planar joint is
  two crossed arrows and a turn, a pin in a slot a turn and an arrow along
  the slot, a screw an arrow wound round its axis, and a ball three bands
  and a stud. A turn with a limit has a mark where the joint rests, as tall
  as the turn. A turn with no limit has no mark. The yellow limit of a slide
  is a thin rail with a flat stop at each end, and the tip of the arrow
  touches a stop at the limit. Build a rig again to get the new widgets.
  The build replaces the old ones.

## Bug fixes

- The bone widgets no longer show in the scene when it holds two rigs.
- A lock on one rig no longer stops the build of the rig of another
  assembly.
- Refresh Model keeps the rigs of hidden configurations. A refresh of a
  configuration that was excluded or hidden in the view layer took the
  limits and the other constraints off its rig with "Add and Remove Bones",
  and "Build a New Rig" left the rig with no bones. The refresh now shows
  the configuration for its work and hides it again after. A rig build
  that cannot reach its rig stops before it changes anything, and says
  which collection to show.
- A loop that is not flat holds the axis of the joint that closes it. It
  held only the point of that joint, so the two sides could turn apart
  about it, and a spatial linkage came apart as it moved.
- A universal joint built from its parts is driven exactly also when its
  shafts turn on a body that is not fixed. A rig with a movable root left
  it to the solver, and it tore.
- A roller follower rests where the rig was built. Its offset of the cam
  was 51 um short at sharp edges on a coarse profile, and it is now under
  2 um.
- A STEP file with a color out of range imports. SolidWorks writes
  -1 for "no color", and the import stopped with "Color out". The import
  now reads a copy with those colors made neutral, and keeps the other
  colors.
- A part with no mates, free in SolidWorks, gets the widget of a free
  body on its bone.
