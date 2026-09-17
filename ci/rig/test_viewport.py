# SPDX-License-Identifier: GPL-3.0-or-later
"""Standing the Blender view where the CAD application is looking from.

The CAD view reports the axes of the screen. The rotation of a view is
those axes as its columns, turned by whatever the import turned the
geometry by, so this is the arithmetic that decides whether the angle
arrives right way up.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig.viewport import centre, rotation  # noqa: E402

# A SolidWorks isometric view, as IModelView reported it (map0, 2026-09-15).
ISO = [0.7071067811865462, -0.40820405591135517, 0.5773815451999839, 0,
       0.8165408118857499, 0.5772877120855426, -0.7071067811865488,
       -0.4082040559113537, 0.5773815451999817, 0, 0, 0, 1, 0, 0, 0]

# Y up, which is what a SolidWorks model usually is: its Y becomes Z.
Y_UP = [[1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, -1.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0]]


def close(a, b, slack=1e-9):
    return all(abs(x - y) <= slack for x, y in zip(a, b))


def test_nothing_to_read_moves_nothing():
    assert rotation(None) is None
    assert rotation([]) is None
    assert rotation([1, 0, 0, 0, 1, 0]) is None
    assert centre(None) is None


def test_a_view_that_looks_down_z_is_no_rotation_at_all():
    rows = rotation([1, 0, 0, 0, 1, 0, 0, 0, 1])
    assert rows == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


def test_the_columns_are_the_axes_of_the_screen():
    # The CAD numbers run down the columns: the screen's across axis is
    # the first of every three. That is the reading the render comparison
    # of 2026-09-15 confirmed against SolidWorks' own picture.
    rows = rotation(ISO)
    across = [rows[i][0] for i in range(3)]
    up = [rows[i][1] for i in range(3)]
    out = [rows[i][2] for i in range(3)]
    assert close(across, [ISO[0], ISO[3], ISO[6]])
    assert close(up, [ISO[1], ISO[4], ISO[7]])
    assert close(out, [ISO[2], ISO[5], ISO[8]])


def test_what_comes_out_is_a_rotation_and_nothing_else():
    # Orthonormal, right handed. A view matrix that is not turns the model
    # inside out, and Blender takes it without a word.
    rows = rotation(ISO, Y_UP)
    cols = [[rows[i][j] for i in range(3)] for j in range(3)]
    for i in range(3):
        assert abs(sum(c * c for c in cols[i]) - 1.0) < 1e-9
        for j in range(i + 1, 3):
            assert abs(sum(cols[i][k] * cols[j][k] for k in range(3))) < 1e-9
    det = (cols[0][0] * (cols[1][1] * cols[2][2] - cols[1][2] * cols[2][1])
           - cols[0][1] * (cols[1][0] * cols[2][2] - cols[1][2] * cols[2][0])
           + cols[0][2] * (cols[1][0] * cols[2][1] - cols[1][1] * cols[2][0]))
    assert abs(det - 1.0) < 1e-9


def test_the_import_turns_the_view_with_the_geometry():
    # With Y up, the CAD Y axis becomes Blender's Z, so a view looking
    # along the CAD Z axis looks along Blender's -Y.
    rows = rotation([1, 0, 0, 0, 1, 0, 0, 0, 1], Y_UP)
    out = [rows[i][2] for i in range(3)]
    up = [rows[i][1] for i in range(3)]
    assert close(out, [0.0, -1.0, 0.0])
    assert close(up, [0.0, 0.0, 1.0])


def test_the_middle_of_the_box_lands_where_the_import_put_it():
    box = [-1.0, -2.0, -3.0, 1.0, 4.0, 5.0]
    assert close(centre(box), [0.0, 1.0, 1.0])
    assert close(centre(box, Y_UP), [0.0, -1.0, 1.0])
