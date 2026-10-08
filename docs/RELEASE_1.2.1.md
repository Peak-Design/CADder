# CADder 1.2.1

Two requests from users. An assembly that you moved in Blender stays
together through Refresh Model, and a part can come in as one object for
each of its materials. Blender 5.1 or later. Install the zip for your
platform the same way as the first time.

CADder 1.2.1 works with CADder Bridge 1.2.0. CADder Bridge has no new
release. See
[Which CADder Bridge works with this CADder](https://github.com/Peak-Design/CADder/blob/main/docs/GUIDE.md#which-cadder-bridge-works-with-this-cadder).

## New

- **Split by Material.** A part with faces in two or more materials can
  come in as that many objects, one for each material. Each object has the
  name of the part and of the material, for example `housing.RED`. Use it
  when faces of a part have their own color, such as graphics on a
  product: you can then give each one a material, link materials and copy
  them between parts. A refresh keeps the objects and the materials you
  gave them, so you do not separate the parts again after each refresh.
  - For a STEP file: select **Split by Material** under **Advanced** in
    the import dialog. A refresh of the file uses what the import chose.
  - For a send from SolidWorks: select **Split by Material** in the **Mesh
    Quality** panel. It applies at the next send and the next Refresh
    Model. The objects of one part are on the same bone, so they move as
    one.
- **An assembly that you moved stays together.** Move the rig of a send,
  or put the rig or the parts under an empty of your own and move the
  empty. Refresh Model keeps the assembly where you put it:
  - A new part in SolidWorks comes in with the others, not at its place
    in SolidWorks.
  - A part that moved in SolidWorks goes to its new place in the assembly
    where it is now.
  - The rig stays under your empty, also when **Build a new rig** makes a
    new one. When you put the parts under an empty and not the rig, the
    rig goes under that empty too, so the empty moves all of it.

## Changes

- With **Keep the rig as it is**, a new part has no bone. It is now
  attached to the rig itself, so it moves with the assembly.
- With **Split by Material**, a part of a STEP file keeps the material of
  each of its faces. The one material of **Engineering Materials** is not
  put on a part that is in pieces.

## Bug fixes

- Refresh Model no longer puts a part that moved in SolidWorks at its
  SolidWorks place when you moved the parts of the assembly in Blender and
  not the rig. The part stood away from the others.
- **Build a new rig** in Refresh Model no longer takes the rig out from
  under an empty that you put it under.
