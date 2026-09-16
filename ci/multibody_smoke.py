# SPDX-License-Identifier: GPL-3.0-or-later
"""Two bodies in one part: do both come through whole, each in its color?

    blender -b --factory-startup --python-exit-code 1 -P ci/multibody_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

The fixture is a buoyancy module in miniature: a foam block, and a skin
round it whose inside lies exactly on the foam. Both bodies are one part.
The bodies carry one color and their faces another, and the foam's face
labels hold each face turned the other way round from the face in the
body. A SOLIDWORKS export of a real buoyancy module has all of this.

Three faults met here. The importer welded vertices by position across the
whole part, so the foam and the skin were welded where they touch, and a
filter for duplicate triangles then threw away one body's copy of every
triangle they shared. Faces were matched with their orientation, so a face
label turned the other way counted as a second face, and the face was
meshed twice in two colors. And every labeled face was meshed again on
its own, which can split its edges differently from its neighbors, so the
edges stop welding and the face comes loose as an island.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import bpy
import numpy as np

bpy.ops.preferences.addon_enable(module="CADder")
from CADder import main as m

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def write_step(path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorSurf
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TDataStd import TDataStd_Name

    # A drilled foam block, and a skin 2 mm thick whose inside is the foam.
    # The skin is cut with a copy of the foam. Cut with the foam itself,
    # OCC would reuse the foam's faces as the skin's inside, and the two
    # bodies would share faces instead of only touching.
    foam = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 100, 60, 40).Shape()
    hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(50, 30, -5), gp_Dir(0, 0, 1)), 8, 50).Shape()
    foam = BRepAlgoAPI_Cut(foam, hole).Shape()
    outer = BRepPrimAPI_MakeBox(gp_Pnt(-2, -2, -2), 104, 64, 44).Shape()
    outer = BRepAlgoAPI_Cut(outer, hole).Shape()
    skin = BRepAlgoAPI_Cut(outer, BRepBuilderAPI_Copy(foam).Shape()).Shape()

    part = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(part)
    builder.Add(part, skin)
    builder.Add(part, foam)

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    label = tool.AddShape(part, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString("buoyancy module"))

    def rgb(r, g, b):
        return Quantity_Color(r, g, b, Quantity_TOC_RGB)

    body = rgb(0.85, 0.65, 0.13)
    for solid, face_color in ((skin, rgb(1.0, 1.0, 0.0)),
                              (foam, rgb(1.0, 0.9, 0.77))):
        sub = tool.AddSubShape(label, solid)
        colors.SetColor(sub, body, XCAFDoc_ColorSurf)
        ex = TopExp_Explorer(solid, TopAbs_FACE)
        while ex.More():
            face = TopoDS.Face_s(ex.Current())
            sub = tool.AddSubShape(label, face)
            if not sub.IsNull():
                colors.SetColor(sub, face_color, XCAFDoc_ColorSurf)
            ex.Next()
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    w.Write(path)


tmp = tempfile.mkdtemp(prefix="stepper_multibody_")
STEP = os.path.join(tmp, "buoyancy.step")
write_step(STEP)


def turn_foam_labels(reader):
    """Turn the foam's face labels the other way round, as SOLIDWORKS does.

    OCC writes a label with the face the way it is in its body, so the
    fixture turns the foam's face labels over after the file is read. The
    foam is the body whose faces carry the second face color.
    """
    from CADder.importer import ShapeKey
    from OCP.TopAbs import TopAbs_FACE
    turned = 0
    for key, subs in reader.sub_shapes.items():
        for i, s in enumerate(subs):
            if s.ShapeType() != TopAbs_FACE:
                continue
            col = reader.face_colors.get(ShapeKey(s))
            if col is None or col.Blue() < 0.5:
                continue
            flipped = s.Reversed()
            reader.face_colors[ShapeKey(flipped)] = col
            subs[i] = flipped
            turned += 1
    return turned


def load():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    live = sys.modules["CADder.main"]
    live._cache_drop(STEP)
    turned = []
    from CADder import importer
    real_read = importer.ReadSTEP.read_file

    def read_and_turn(self, filename):
        real_read(self, filename)
        turned.append(turn_foam_labels(self))

    importer.ReadSTEP.read_file = read_and_turn
    try:
        live.load_step(bpy.context, STEP, htypes="FLAT", up_as="Z",
                       tris_to_quads=False, uv_mode="SURFACE")
    finally:
        importer.ReadSTEP.read_file = real_read
    objs = [o for o in bpy.data.objects
            if o.type == "MESH" and len(o.data.polygons)]
    return objs, (turned[0] if turned else 0)


print("\n== both bodies come through whole")
objs, turned = load()
check(len(objs) == 1, "the part is one mesh (%d)" % len(objs))
check(turned > 0, "the fixture turned %d foam face labels over" % turned)
me = objs[0].data
le = np.empty(len(me.loops), dtype=np.int64)
me.loops.foreach_get("edge_index", le)
per_edge = np.bincount(le, minlength=len(me.edges))
check(int((per_edge > 2).sum()) == 0,
      "no edge is shared by more than two faces (%d are)"
      % int((per_edge > 2).sum()))
check(int((per_edge == 1).sum()) == 0,
      "no edge is open, so both bodies are closed (%d are)"
      % int((per_edge == 1).sum()))

# Every triangle of the foam's outside lies on a triangle of the skin's
# inside. With both bodies kept, each such place is covered twice, facing
# opposite ways.
co = np.empty(len(me.vertices) * 3, dtype=np.float64)
me.vertices.foreach_get("co", co)
co = co.reshape(-1, 3)
places = {}
for p in me.polygons:
    key = tuple(sorted(tuple(np.round(co[v], 5)) for v in p.vertices))
    places.setdefault(key, []).append(p.index)
doubled = sum(1 for v in places.values() if len(v) == 2)
check(doubled > 0,
      "the faces where the bodies touch are there twice, once for each "
      "body (%d places)" % doubled)

print("\n== each face takes its own color, once")
# The foam's faces are labeled in the foam's color, turned over. Matched
# with their orientation, those labels counted as second faces, and the
# foam came out in the body color and facing in.
names = [s.material.name if s.material else "" for s in objs[0].material_slots]
mi = np.empty(len(me.polygons), dtype=np.int32)
me.polygons.foreach_get("material_index", mi)
used = sorted(set(names[i] for i in mi))
# LIGHTGOLDENROD2 is the nearest named color to the body color.
foam_name = [n for n in used if n not in ("YELLOW", "LIGHTGOLDENROD2")]
check(len(used) == 2 and len(foam_name) == 1,
      "two colors, one for each body's faces, and none for the bodies "
      "(%s)" % used)
normals = np.empty(len(me.polygons) * 3, dtype=np.float64)
me.polygons.foreach_get("normal", normals)
normals = normals.reshape(-1, 3)
mid = np.array([co[list(p.vertices)].mean(axis=0) for p in me.polygons])
foam = [i for i in range(len(me.polygons))
        if foam_name and names[mi[i]] == foam_name[0]]
height = max(mid[i][2] for i in foam) if foam else 0.0
top = [i for i in foam if abs(mid[i][2] - height) < 1e-9]
inward = [i for i in top if normals[i][2] < 0.0]
check(top and not inward,
      "the foam faces out (%d of %d top faces face in)"
      % (len(inward), len(top)))
# A place covered twice holds one face of each body, so the two face
# opposite ways. The same face meshed twice would face the same way.
same_way = sum(1 for v in places.values()
               if len(v) > 2 or (len(v) == 2
                                 and normals[v[0]] @ normals[v[1]] > 0.0))
check(same_way == 0,
      "no face is there twice (%d places are)" % same_way)

if FAILS:
    print("\nmultibody_smoke: FAILED (%d)\n  %s"
          % (len(FAILS), "\n  ".join(FAILS)))
    sys.exit(1)
print("\nmultibody_smoke: OK - two touching bodies stay two, in their colors")
