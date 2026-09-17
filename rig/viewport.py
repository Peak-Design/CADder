# SPDX-License-Identifier: GPL-3.0-or-later
"""Standing the Blender view where the CAD application is looking from.

A send carries the angle of the CAD graphics view when the user asks for
it. The view reports its orientation as a transform from model space to
view space, whose three columns are the axes of the screen: across, up,
and out of the screen towards the viewer. Those are the axes of a camera,
so the rotation of the Blender view is the same three columns, turned by
whatever the import turned the geometry by (the up axis).

What does NOT travel is the pan and the zoom of the CAD view. Blender
frames what it holds instead, which is what a person expects to see after
an import, and is one thing less to get wrong.
"""

try:
    import bpy
except ImportError:                                    # pragma: no cover
    bpy = None


def rotation(orientation, frame=None):
    """The rotation of the Blender view, as three rows.

    `orientation` is the CAD view's own transform, the 9 or 16 numbers it
    reports, in COLUMN order: the first three are the screen's across axis,
    the next three its up axis, the next three the axis out of the screen.
    `frame` is the 3x3 (or 4x4) the import turned the geometry by, or None
    for the axes as they were. Returns None when there is nothing to read.
    """
    if not orientation or len(orientation) < 9:
        return None
    a = list(orientation)
    # The columns as rows: across, up, out of the screen.
    axes = [[a[0], a[3], a[6]],
            [a[1], a[4], a[7]],
            [a[2], a[5], a[8]]]
    if frame:
        rows = [list(row)[:3] for row in list(frame)[:3]]
        axes = [[sum(rows[i][k] * axis[k] for k in range(3))
                 for i in range(3)] for axis in axes]
    # A rotation takes the camera's own axes to the world's, so the axes
    # above are its columns, which is this transposed.
    return [[axes[0][i], axes[1][i], axes[2][i]] for i in range(3)]


def centre(box, frame=None):
    """The middle of the CAD model's box, where the import put it, or None.

    The box is six numbers, the low corner then the high one, in metres.
    """
    if not box or len(box) < 6:
        return None
    mid = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
    if not frame:
        return mid
    rows = [list(row) for row in list(frame)[:3]]
    return [sum(rows[i][k] * mid[k] for k in range(3))
            + (rows[i][3] if len(rows[i]) > 3 else 0.0) for i in range(3)]


def _parts(context):
    """The CAD parts of the scene, whichever way they came in. What the
    view is framed on: a light or a camera parked far away would otherwise
    decide how close the model is."""
    return [obj for obj in context.view_layer.objects
            if obj.get("RIG_component_id") or "STEP_tag" in obj]


def match(view, frame=None):
    """Turns every 3D view of this Blender to the angle the CAD view is at.

    Returns how many views it moved. Anything it cannot read leaves the
    views alone: a view that did not arrive is not a failure, it is a send
    from a user who did not ask for this.

    The angle is the CAD application's. The distance is not: the CAD pan
    and zoom do not travel, so Blender frames the parts, which is what a
    person expects to see after an import.
    """
    if bpy is None or not view:
        return 0
    from mathutils import Matrix, Vector

    rows = rotation(view.get("orientation"), frame)
    if rows is None:
        return 0
    turn = Matrix(rows).to_quaternion()
    middle = centre(view.get("box"), frame)
    context = bpy.context
    parts = _parts(context)
    held = [obj for obj in context.view_layer.objects if obj.select_get()]
    active = context.view_layer.objects.active
    if parts:
        for obj in held:
            obj.select_set(False)
        for obj in parts:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
    moved = 0
    for window in bpy.context.window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            region_3d = getattr(space, "region_3d", None)
            region = next((r for r in area.regions if r.type == "WINDOW"),
                          None)
            if region_3d is None or region is None:
                continue
            region_3d.view_perspective = "ORTHO"
            region_3d.view_rotation = turn
            if middle is not None:
                region_3d.view_location = Vector(middle)
            # Blender's own framing, so the distance is right for the
            # region as it stands rather than worked out from the lens.
            try:
                with context.temp_override(window=window, area=area,
                                           region=region):
                    if parts:
                        bpy.ops.view3d.view_selected(use_all_regions=False)
                    else:
                        bpy.ops.view3d.view_all(center=False)
            except Exception as exc:                   # noqa: BLE001
                print("[CADLink view] could not frame the model: %s" % exc)
            moved += 1

    if parts:
        for obj in parts:
            try:
                obj.select_set(False)
            except RuntimeError:
                pass
        for obj in held:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
        context.view_layer.objects.active = active
    return moved

