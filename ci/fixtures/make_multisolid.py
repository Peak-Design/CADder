"""A STEP file of the shape the "separate solids" option exists for: ONE
product holding several disjoint solids and no assembly structure — what a
multibody part exports as.

The distinction that matters is XCAFDoc_ShapeTool.AddShape's makeAssembly
flag. With it true (or with a plain STEPControl_Writer transfer of a
compound) the reader gets one node per solid and there is nothing to
separate. With it false the compound stays ONE label, one product, one node
whose shape holds three solids.

    blender -b --factory-startup -P make_multisolid.py -- <out.step>
"""
import sys

OUT = sys.argv[-1]

ADDON = (r"C:\Users\Oscar\AppData\Roaming\Blender Foundation\Blender\5.1"
         r"\scripts\addons")
sys.path.insert(0, ADDON)
import bpy
bpy.ops.preferences.addon_enable(module="STEPper_NEXT")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeSphere
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Compound
from OCP.gp import gp_Pnt
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.TDataStd import TDataStd_Name
from OCP.IFSelect import IFSelect_RetDone

builder = BRep_Builder()
comp = TopoDS_Compound()
builder.MakeCompound(comp)

# Three disjoint bodies, each distinguishable by position and extent after
# the split.
builder.Add(comp, BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape())
builder.Add(comp, BRepPrimAPI_MakeBox(gp_Pnt(30, 0, 0), 20, 5, 5).Shape())
builder.Add(comp, BRepPrimAPI_MakeSphere(gp_Pnt(0, 40, 0), 7).Shape())

doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
label = tool.AddShape(comp, False)          # False: ONE product, not an assembly
TDataStd_Name.Set_s(label, TCollection_ExtendedString("multibody part"))

w = STEPCAFControl_Writer()
w.Transfer(doc)
status = w.Write(OUT)
print("WROTE", OUT, "status", status,
      "ok" if status == IFSelect_RetDone else "FAILED")
