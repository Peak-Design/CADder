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
- **One mesh for a part that does not change.** A part that two
  configurations hold the same way is one mesh in Blender. A part with
  another shape, or with another appearance, in the other configuration
  has a mesh of its own.
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

## Bug fixes

- The bone widgets no longer show in the scene when it holds two rigs.
- A lock on one rig no longer stops the build of the rig of another
  assembly.
