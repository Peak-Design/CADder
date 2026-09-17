# SPDX-License-Identifier: GPL-3.0-or-later
"""Writes holes.step: a plate with four small bolt holes and one big one.

The sizes are the point of the fixture. At a 12 mm dial the four 6 mm holes
are small features and the 30 mm bore is not, so a defeature that takes the
lot is as wrong as one that takes none, and the test can tell the
difference. The plate is 100 x 60 x 10 mm, which is the sort of thing the
rule was written for.

Run:  blender -b --factory-startup -P make_holes.py
"""
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
bpy.ops.preferences.addon_enable(module="CADder")

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut                    # noqa: E402
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox,              # noqa: E402
                             BRepPrimAPI_MakeCylinder)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt                      # noqa: E402
from OCP.Interface import Interface_Static                     # noqa: E402
from OCP.STEPControl import (STEPControl_AsIs,                 # noqa: E402
                             STEPControl_Writer)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "holes.step")

#: Millimetres. Four of these go, at a 12 mm dial.
SMALL = 6.0
#: And this one stays.
BIG = 30.0


def hole(x, y, diameter):
    axis = gp_Ax2(gp_Pnt(x, y, -1.0), gp_Dir(0.0, 0.0, 1.0))
    return BRepPrimAPI_MakeCylinder(axis, diameter * 0.5, 12.0).Shape()


def main():
    shape = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 100.0, 60.0, 10.0).Shape()
    for x, y in ((12.0, 12.0), (88.0, 12.0), (12.0, 48.0), (88.0, 48.0)):
        shape = BRepAlgoAPI_Cut(shape, hole(x, y, SMALL)).Shape()
    shape = BRepAlgoAPI_Cut(shape, hole(50.0, 30.0, BIG)).Shape()

    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    status = writer.Write(OUT)
    print("wrote %s (%s)" % (OUT, status))


main()
