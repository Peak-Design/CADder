# CADder 1.2.2

UV islands now stand upright to the world, so a texture with a direction
runs the same way on every face. This release also adds Blender's Smart UV
Project as a UV mode. Blender 5.1 or later. Install the zip for your platform
the same way as the first time.

CADder 1.2.2 works with CADder Bridge 1.2.0. CADder Bridge has no new
release. See
[Which CADder Bridge works with this CADder](https://github.com/Peak-Design/CADder/blob/main/docs/GUIDE.md#which-cadder-bridge-works-with-this-cadder).

## New

- **Upright UV islands.** **CAD Surfaces** and **CAD Surfaces (Smart)** now
  turn each island so that world Z points up the UV tile. Wood grain, brushed
  metal and text then run the same way on every island.
  - A face that lies flat uses world Y.
  - A cylinder that lies on its side has its axis across the tile.
  - An island that came in mirrored is put the right way round.
- **Rotate to Fit.** Select this option to let **Pack UVs** turn the islands
  to fill the tile, as in earlier versions. Use it when the texture has no
  direction and texture space is more important. The option is in the import
  dialog and in the **CADder: UV** panel. For a send from SolidWorks, set it
  in the panel before the send.
- **Smart UV Project.** The **UV Map** list now has Blender's Smart UV
  Project. It cuts the mesh where two faces meet at more than the **Angle
  Limit** (66 degrees by default), and not on the CAD faces. It reads only
  the mesh, so it also works on a mesh from another source.

## Changes

- **New imports and sends have upright islands by default.** Upright islands
  fill the UV tile less tightly than turned islands. To get the earlier
  layout, select **Rotate to Fit**.
- Your existing scenes keep their UV maps:
  - A STEP file that you imported before 1.2.2 keeps its layout through
    **Refresh from Disk** and **Regenerate**.
  - **Refresh Model** does not change the UVs of a part that did not change
    in SolidWorks, including UVs that you made by hand. A new or changed part
    follows the **Rotate to Fit** setting.
- Parts that share one mesh also share one UV map, so the first placement of
  the part sets which way is up.
- The Engineering Materials tooltip and the guide now point SOLIDWORKS users
  to **Export STEP+** in CADder Bridge.

## Bug fixes

- **Refresh Model** no longer unwraps the compound faces of a part that did
  not change. This replaced the UVs of those faces, including UVs that you
  made by hand.
