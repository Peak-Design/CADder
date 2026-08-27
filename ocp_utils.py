# Helpers bridging pythonocc-core conveniences that do not exist in the
# OCP (pybind11) bindings.

from OCP.TCollection import TCollection_AsciiString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Tool


def get_label_name(label):
    """Replacement for pythonocc's TDF_Label.GetLabelName() SWIG extension.

    ToCString() hands back bytes that OCP decodes as UTF-8, and a STEP file
    is under no obligation to be UTF-8: a part named with a degree sign
    arrives as a lone 0xB0 and the whole import dies on one character
    (live ClampRig, 2026-08-24). The name is a label, so a
    best-effort decode is worth far more than an exception.
    """
    n = TDataStd_Name()
    if not label.FindAttribute(TDataStd_Name.GetID_s(), n):
        return ""
    ascii_string = TCollection_AsciiString(n.Get(), "?")
    try:
        return ascii_string.ToCString()
    except UnicodeDecodeError:
        pass
    # ToCString is all-or-nothing, so read it a character at a time and let
    # the one bad character be the only casualty. Value() returns a str on
    # some OCP builds and an int on others; both are handled, and a
    # character that cannot be read at all becomes "?".
    out = []
    try:
        length = ascii_string.Length()
    except Exception:
        return ""
    for i in range(1, length + 1):
        try:
            value = ascii_string.Value(i)
        except Exception:
            out.append("?")
            continue
        if isinstance(value, int):
            value = bytes([value & 0xFF]).decode("cp1252", "replace")
        out.append(str(value))
    return "".join(out)


def label_entry(label):
    """Replacement for pythonocc's TDF_Label.EntryDumpToString()."""
    a = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, a)
    return a.ToCString()


class ShapeKey:
    """Dict/set key wrapper restoring pythonocc's TopoDS_Shape semantics.

    OCP's TopoDS_Shape.__hash__ is content-based but __eq__ is wrapper
    identity, so two wrappers of the same shape hash alike yet compare
    unequal, silently breaking every shape-keyed container.
    """

    __slots__ = ("shape", "_h")

    def __init__(self, shape):
        self.shape = shape
        self._h = hash(shape)

    def __hash__(self):
        return self._h

    def __eq__(self, other):
        if not isinstance(other, ShapeKey):
            return NotImplemented
        return self.shape.IsEqual(other.shape)
