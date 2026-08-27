"""A STEP file with real assembly structure, for the import modes that only
differ when there IS one: nesting for the tree and empties modes, and a
product used TWICE so the collection-instance mode has something to
instance.

    machine
      +- base            (one product, once)
      +- arm_asm         (subassembly)
      |    +- arm
      |    +- pad        (shared product, instance 1)
      +- pad             (shared product, instance 2)

Every part sits somewhere different so a test can tell which one moved.

    blender -b --factory-startup -P make_assembly.py -- <out.step>
"""
import sys

OUT = sys.argv[-1]

ADDON = (r"C:\Users\Oscar\AppData\Roaming\Blender Foundation\Blender\5.1"
         r"\scripts\addons")
sys.path.insert(0, ADDON)
import bpy
bpy.ops.preferences.addon_enable(module="STEPper_NEXT")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
from OCP.TopLoc import TopLoc_Location
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.TDataStd import TDataStd_Name
from OCP.IFSelect import IFSelect_RetDone


def at(x, y, z):
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(x, y, z))
    return TopLoc_Location(t)


doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())


def part(name, dx, dy, dz):
    label = tool.AddShape(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), dx, dy, dz)
                          .Shape(), False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    return label


def assembly(name):
    label = tool.NewShape()
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    return label


base = part("base", 40, 40, 5)
arm = part("arm", 30, 6, 6)
pad = part("pad", 8, 8, 3)

arm_asm = assembly("arm_asm")
tool.AddComponent(arm_asm, arm, at(0, 0, 0))
tool.AddComponent(arm_asm, pad, at(30, 0, 0))

machine = assembly("machine")
tool.AddComponent(machine, base, at(0, 0, 0))
tool.AddComponent(machine, arm_asm, at(0, 0, 20))
tool.AddComponent(machine, pad, at(0, 60, 0))

tool.UpdateAssemblies()

w = STEPCAFControl_Writer()
w.Transfer(doc)
status = w.Write(OUT)
print("WROTE", OUT, "status", status,
      "ok" if status == IFSelect_RetDone else "FAILED")
