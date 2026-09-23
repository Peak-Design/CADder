# SPDX-License-Identifier: GPL-3.0-or-later
"""The STEP reader must start each import clean.

    blender -b --factory-startup --python-exit-code 1 -P ci/reader_state_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

  1. A file with broken references whose transfer fails is read again in
     a safe mode that uses only the 3D curves. The retry set the curve
     mode to 0, which is the default, so it repeated the failed transfer.
     It then set the mode to 1 ("?") for the rest of the session, and it
     did not check whether the second read worked.

  2. The reader of a file is cached, and the next import of the same
     unchanged file uses it again. The list of parts with no geometry was
     made once, with the reader, so each import added the same part again:
     the popup said 1 part, then 2, then 3.
"""
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_ADDON))

import bpy

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m, importer

from OCP.IFSelect import IFSelect_RetFail
from OCP.Interface import Interface_Static
from OCP.STEPCAFControl import STEPCAFControl_Reader

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


tmp = tempfile.mkdtemp(prefix="cadder_reader_")

# -- 1. the safe mode retry ----------------------------------------------------
print("\n== the safe mode retry")


def write_step_with_broken_reference(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    w = STEPControl_Writer()
    w.Transfer(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(),
               STEPControl_AsIs)
    w.Write(path)
    text = open(path, encoding="utf-8").read()
    # A reference to an entity that is not in the file. The data model
    # check then fails, which is what starts the retry.
    found = re.search(r"PRODUCT_RELATED_PRODUCT_CATEGORY\('part',\$,\(#\d+\)\)",
                      text)
    text = text.replace(found.group(0),
                        re.sub(r"#\d+", "#99999", found.group(0)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class _FlakyReader:
    """A real STEP reader whose first transfer fails. It records the curve
    mode of each transfer."""
    transfers = []
    made = 0
    fail_read_of = None

    def __init__(self):
        _FlakyReader.made += 1
        self.number = _FlakyReader.made
        self._real = STEPCAFControl_Reader()

    def __getattr__(self, name):
        return getattr(self._real, name)

    def ReadFile(self, filename):
        if self.number == _FlakyReader.fail_read_of:
            return IFSelect_RetFail
        return self._real.ReadFile(filename)

    def Transfer(self, doc):
        _FlakyReader.transfers.append(
            Interface_Static.IVal_s("read.surfacecurve.mode"))
        if len(_FlakyReader.transfers) == 1:
            return False
        return self._real.Transfer(doc)


BROKEN = os.path.join(tmp, "broken_reference.step")
write_step_with_broken_reference(BROKEN)
before = Interface_Static.IVal_s("read.surfacecurve.mode")

for fail_read_of, label in ((None, "the second read works"),
                            (2, "the second read fails")):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    m._cache_drop(BROKEN)
    _FlakyReader.transfers = []
    _FlakyReader.made = 0
    _FlakyReader.fail_read_of = fail_read_of
    importer.STEPCAFControl_Reader = _FlakyReader
    try:
        try:
            result = m.load_step(bpy.context, BROKEN, htypes="FLAT", up_as="Z")
            raised = None
        except Exception as exc:
            result = None
            raised = "%s: %s" % (type(exc).__name__, exc)
    finally:
        importer.STEPCAFControl_Reader = STEPCAFControl_Reader
    print("    %s: transfers at modes %s" % (label, _FlakyReader.transfers))
    check(raised is None, "%s: load_step does not raise (%s)"
          % (label, raised or "no exception"))
    after = Interface_Static.IVal_s("read.surfacecurve.mode")
    check(after == before, "%s: the curve mode is back to %d (%d)"
          % (label, before, after))
    if fail_read_of is None:
        check(_FlakyReader.transfers[1:2] == [-3],
              "%s: the retry uses only the 3D curves (%s)"
              % (label, _FlakyReader.transfers))
        check(result is not False and any(
            o.type == "MESH" and len(o.data.polygons)
            for o in bpy.data.objects),
            "%s: the retry imports the part" % label)
    else:
        check(result is False,
              "%s: the file is reported as one that cannot be opened (%r)"
              % (label, result))

# -- 2. a cached reader --------------------------------------------------------
print("\n== the same file imported three times")


def write_step_with_empty_part(path):
    """A box, and a part that holds only an edge: it gives no mesh."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for name, shape in (
            ("solid_part", BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()),
            ("wire_part", BRepBuilderAPI_MakeEdge(
                gp_Pnt(0, 0, 0), gp_Pnt(50, 0, 0)).Edge())):
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


EMPTY = os.path.join(tmp, "empty_part.step")
write_step_with_empty_part(EMPTY)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="CADder")
m._cache_drop(EMPTY)
counts = []
first = None
for i in range(3):
    result = m.load_step(bpy.context, EMPTY, htypes="FLAT", up_as="Z")
    check(result is not False, "import %d opens the file" % (i + 1))
    if result is False:
        break
    failed, _recovered = result
    counts.append(len(failed))
    if first is None:
        first, first_len = failed, len(failed)
check(bool(counts) and counts[0] >= 1,
      "the first import reports the part with no geometry (%s)" % counts)
check(len(set(counts)) == 1,
      "every import reports it once, cache or no cache (%s)" % counts)
check(first is not None and len(first) == first_len,
      "the list the first import gave back does not change later (%s)"
      % (first,))

if FAILS:
    print("\nreader_state_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nreader_state_smoke: OK - each import starts with a clean reader")
