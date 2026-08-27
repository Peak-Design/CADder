# Additional CAD format readers for STEPper NEXT: IGES and BREP.
#
# Both subclass ReadSTEP and override only the document-transfer stage. the
# shared tree walk, tessellation, healing and native handoff run unchanged.
# STEP-entity recovery is a STEP-only feature (guarded by _xcaf_reader).

import ntpath
import os

from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools
from OCP.BinTools import BinTools
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESCAFControl import IGESCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopoDS import TopoDS_Shape
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool

from .importer import ReadSTEP
import io

STEP_EXTS = (".step", ".stp", ".st")
IGES_EXTS = (".iges", ".igs")
BREP_EXTS = (".brep", ".brp")
ALL_CAD_EXTS = STEP_EXTS + IGES_EXTS + BREP_EXTS


class ReadIGES(ReadSTEP):
    """IGES import through the same XCAF pipeline as STEP.

    IGES has no assembly structure in practice, so the free-shapes walk
    yields a flat tree. The IGES reader converts geometry to millimeters
    (OCCT xstep.cascade.unit default), hence the fixed 0.001 scale.
    """

    def transfer_with_units(self, filename):
        print("Init IGES transfer")
        doc = TDocStd_Document(TCollection_ExtendedString("IGES"))
        reader = IGESCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)
        reader.SetLayerMode(True)

        status = reader.ReadFile(filename)
        if status != IFSelect_RetDone:
            raise AssertionError("Error: can't read IGES file. File possibly damaged.")

        print("IGES read into memory, transferring")
        try:
            ok = reader.Transfer(doc)
            if not ok:
                print("IGES transfer reported failure, continuing with partial data")
        except Exception as e:
            raise AssertionError(f"IGES transfer failed: {e}")

        self.scale = 0.001  # geometry arrives in millimeters
        self.doc = doc
        self._xcaf_reader = None  # STEP-entity recovery not applicable


class ReadBREP(ReadSTEP):
    """Native OCCT .brep shape import (text or binary), no colors/names."""

    def transfer_with_units(self, filename):
        print("Init BREP read")
        with open(filename, "rb") as f:
            data = f.read()

        shape = TopoDS_Shape()
        if data.lstrip()[:32].startswith((b"DBRep_DrawableShape",
                                          b"CASCADE Topology")):
            BRepTools.Read_s(shape, io.BytesIO(data), BRep_Builder())
        else:
            BinTools.Read_s(shape, io.BytesIO(data))
        if shape.IsNull():
            raise AssertionError("Error: BREP file contained no shape.")

        doc = TDocStd_Document(TCollection_ExtendedString("BREP"))
        app = XCAFApp_Application.GetApplication_s()
        app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
        st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        lab = st.NewShape()
        st.SetShape(lab, shape)
        name = os.path.splitext(ntpath.basename(filename))[0] or "BREP"
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(name))
        st.UpdateAssemblies()

        self.scale = 0.001  # BREP is unitless. Assume millimeters like STEP
        self.doc = doc
        self._xcaf_reader = None


def make_reader(filepath, skip_name_prefixes=()):
    """Reader factory: choose the reader class from the file extension."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in IGES_EXTS:
        return ReadIGES(filepath, skip_name_prefixes=skip_name_prefixes)
    if ext in BREP_EXTS:
        return ReadBREP(filepath, skip_name_prefixes=skip_name_prefixes)
    return ReadSTEP(filepath, skip_name_prefixes=skip_name_prefixes)


classes = ()
