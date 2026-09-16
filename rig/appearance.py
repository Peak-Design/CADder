# SPDX-License-Identifier: GPL-3.0-or-later
"""SolidWorks appearances as Blender materials.

The SolidWorks add-in sends each appearance whole (colours, finish, the
library file's own values, the texture and its mapping, the decals on the
faces) as JSON on the material record of the .swmesh. SolidWorks has no PBR
material, so what is built here is a reading of that data: a Principled
BSDF with the colour or image, a roughness from how sharp the reflections
are, metal from the library category, transmission for glass, a normal or
bump map, and decals laid over the base colour.

Each decal is two node groups and an image, not the twenty maths nodes
the placement takes. "SW Decal Frame" says where the decal sits and
"Add SW Decal" lays it over what is under it. Every decal in the file
shares the same two groups, so a user who wants decals to behave
differently edits one place.

The raw JSON stays on the material (SWMESH_appearance), so a material
database can swap the reading for an authored material by name.

Texture placement does not use UVs. SolidWorks maps an image by projecting
it from the appearance's own frame (centre, U and V directions, tile width
and height, rotation), and its tessellation texture coordinates ignore that
frame (live usb_case2, 2026-09-15: identical before and after a checker was
applied at 10 mm). The shader rebuilds the projection from object
coordinates, which are the part's own space, so the image stays put on the
part however it moves.
"""

import hashlib
import json
import math
import os

try:
    import bpy
except ImportError:
    bpy = None

# SolidWorks' mapping types (IRenderMaterial.MappingType), pinned live on
# usb_case2 with a checker, 2026-09-15.
SURFACE, PLANAR, SPHERICAL, CYLINDRICAL, AUTOMATIC = 0, 1, 2, 3, 4

_X_STEP = 190
_missing_logged = set()


def parse(text):
    """The appearance JSON of a material record, or None."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def digest(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _input(node, name):
    return node.inputs.get(name) if node is not None else None


def _set(node, name, value):
    socket = _input(node, name)
    if socket is not None:
        try:
            socket.default_value = value
        except (TypeError, ValueError):
            pass


def _image(path, non_color=False):
    """An image datablock for a file, reused by path. None, with one log
    line, when the file is not there: the colour still shows."""
    if not path:
        return None
    path = os.path.normpath(path)
    for img in bpy.data.images:
        if img.filepath and os.path.normpath(bpy.path.abspath(img.filepath)) == path:
            return img
    if not os.path.isfile(path):
        if path not in _missing_logged:
            _missing_logged.add(path)
            print("[CADLink appearance] texture not found: %s" % path)
        return None
    try:
        img = bpy.data.images.load(path, check_existing=True)
    except RuntimeError as exc:
        print("[CADLink appearance] texture not loaded: %s (%s)" % (path, exc))
        return None
    if non_color:
        try:
            img.colorspace_settings.name = "Non-Color"
        except TypeError:
            pass
    return img


class _Graph:
    """Node creation with a running column position, so the tree reads
    left to right in the editor."""

    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links

    def node(self, kind, x, y, **props):
        n = self.nodes.new(kind)
        n.location = (x, y)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, out, into):
        self.links.new(out, into)

    def value(self, v, x, y):
        n = self.node("ShaderNodeValue", x, y)
        n.outputs[0].default_value = float(v)
        return n.outputs[0]

    def math(self, op, a, b, x, y):
        n = self.node("ShaderNodeMath", x, y, operation=op)
        for i, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = float(v)
            else:
                self.link(v, n.inputs[i])
        return n.outputs[0]

    def vmath(self, op, a, b, x, y):
        n = self.node("ShaderNodeVectorMath", x, y, operation=op)
        for i, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, (tuple, list)):
                n.inputs[i].default_value = v
            else:
                self.link(v, n.inputs[i])
        return n.outputs["Value"] if op == "DOT_PRODUCT" else n.outputs["Vector"]


def linear(rgb):
    """SolidWorks colours are display (sRGB) values; Blender's shader and
    viewport colours are linear. Passed through unconverted, every part
    came in lighter than SolidWorks draws it (a dark red cap read pink)."""
    out = []
    for c in rgb[:3]:
        c = max(0.0, min(1.0, float(c)))
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return tuple(out)


def _unit(v, fallback):
    try:
        x, y, z = (float(c) for c in v)
    except (TypeError, ValueError):
        return fallback
    n = math.sqrt(x * x + y * y + z * z)
    return (x / n, y / n, z / n) if n > 1e-12 else fallback


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def projection(g, mapping, unit_scale, x0, y0):
    """The texture vector for a SolidWorks mapping, from object
    coordinates. Returns (vector socket, Image Texture projection mode).

    u, v, w are distances in metres along the appearance's U, V and U x V
    from its centre. Planar tiles u/width, v/height. Automatic hands the
    three to a box projection. Cylindrical wraps around V (arc length over
    width), spherical around U x V."""
    m = mapping or {}
    U = _unit(m.get("u"), (1.0, 0.0, 0.0))
    V = _unit(m.get("v"), (0.0, 1.0, 0.0))
    N = _unit(_cross(U, V), (0.0, 0.0, 1.0))
    c = m.get("centre") or (0.0, 0.0, 0.0)
    s = float(unit_scale) if unit_scale else 1.0
    width = max(float(m.get("width") or 1.0), 1e-9) * s
    height = max(float(m.get("height") or m.get("width") or 1.0), 1e-9) * s
    kind = int(m.get("type", AUTOMATIC))

    coord = g.node("ShaderNodeTexCoord", x0, y0)
    rel = g.vmath("SUBTRACT", coord.outputs["Object"],
                  tuple(float(ci) * s for ci in c), x0 + _X_STEP, y0)
    u = g.vmath("DOT_PRODUCT", rel, U, x0 + 2 * _X_STEP, y0 + 120)
    v = g.vmath("DOT_PRODUCT", rel, V, x0 + 2 * _X_STEP, y0)
    w = g.vmath("DOT_PRODUCT", rel, N, x0 + 2 * _X_STEP, y0 - 120)
    x1 = x0 + 3 * _X_STEP

    if kind == CYLINDRICAL:
        radius = g.math("SQRT", g.math("ADD", g.math("MULTIPLY", u, u, x1, y0 + 200),
                                       g.math("MULTIPLY", w, w, x1, y0 + 100), x1 + _X_STEP, y0 + 150),
                        None, x1 + 2 * _X_STEP, y0 + 150)
        angle = g.math("ARCTAN2", w, u, x1, y0)
        u = g.math("MULTIPLY", angle, radius, x1 + 3 * _X_STEP, y0 + 100)
        x1 += 4 * _X_STEP
    elif kind == SPHERICAL:
        rxy = g.math("SQRT", g.math("ADD", g.math("MULTIPLY", u, u, x1, y0 + 200),
                                    g.math("MULTIPLY", v, v, x1, y0 + 100), x1 + _X_STEP, y0 + 150),
                     None, x1 + 2 * _X_STEP, y0 + 150)
        r = g.math("SQRT", g.math("ADD", g.math("MULTIPLY", rxy, rxy, x1 + 3 * _X_STEP, y0 + 200),
                                  g.math("MULTIPLY", w, w, x1 + 3 * _X_STEP, y0 + 100),
                                  x1 + 4 * _X_STEP, y0 + 150), None, x1 + 5 * _X_STEP, y0 + 150)
        lon = g.math("ARCTAN2", v, u, x1, y0)
        lat = g.math("ARCTAN2", w, rxy, x1, y0 - 100)
        u = g.math("MULTIPLY", lon, rxy, x1 + 6 * _X_STEP, y0 + 100)
        v = g.math("MULTIPLY", lat, r, x1 + 6 * _X_STEP, y0)
        x1 += 7 * _X_STEP

    # Rotation (degrees, SolidWorks turns the image, so the coordinates
    # turn the other way), then tiling, offset and mirror.
    rot = math.radians(float(m.get("rotation") or 0.0))
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    if abs(sin_r) > 1e-9:
        ur = g.math("ADD", g.math("MULTIPLY", u, cos_r, x1, y0 + 100),
                    g.math("MULTIPLY", v, sin_r, x1, y0), x1 + _X_STEP, y0 + 50)
        vr = g.math("SUBTRACT", g.math("MULTIPLY", v, cos_r, x1, y0 - 100),
                    g.math("MULTIPLY", u, sin_r, x1, y0 - 200), x1 + _X_STEP, y0 - 150)
        u, v = ur, vr
        x1 += 2 * _X_STEP
    su = -1.0 if m.get("width_mirror") else 1.0
    sv = -1.0 if m.get("height_mirror") else 1.0
    u = g.math("DIVIDE", u, width * su, x1, y0 + 100)
    v = g.math("DIVIDE", v, height * sv, x1, y0)
    x1 += _X_STEP
    # Automatic: a box. Blender's box reads (x, z) on faces turned along
    # the second axis, and SolidWorks tiles those with the height, not the
    # width (usb_case2 checker at 10 x 5 mm, 2026-09-15).
    ww = g.math("DIVIDE", w, height, x1, y0 - 100) if kind == AUTOMATIC else None

    combine = g.node("ShaderNodeCombineXYZ", x1 + _X_STEP, y0)
    g.link(u, combine.inputs[0])
    g.link(v, combine.inputs[1])
    if ww is not None:
        g.link(ww, combine.inputs[2])
    mode = "BOX" if kind == AUTOMATIC else "FLAT"
    return combine.outputs[0], mode, x1 + 2 * _X_STEP


# A decal takes about twenty maths nodes to place and lay down. Loose in
# the material they bury the shader, so they live in two node groups: one
# says where the decal sits, the other lays it over what is under it.
# Both are built once per file and every decal shares them, which is also
# what makes them worth editing: a change inside one reaches every decal.
FRAME_GROUP = "SW Decal Frame"
MIX_GROUP = "Add SW Decal"
# Raise this when the contents of a group change. A file saved by an
# older version then gets the new contents rather than the ones it saved.
GROUP_VERSION = 1
_VERSION_KEY = "SW_decal_group_version"


def _group(name, fill):
    """The shader node group of this name, built if it is not there yet.

    An older file can hold a group of the same name from an earlier
    version of this addon. Such a group is filled again in place, so
    every material that already points at it gets the new contents.

    Only a group carrying the version stamp is ever refilled. The name
    alone is not enough: a group of the same name can be somebody else's,
    and Blender renames on collision, so "Add SW Decal.001" is as likely
    to be a copy a user made to edit as it is to be this one renamed.
    Refilling either of those would throw away their work."""
    exact = bpy.data.node_groups.get(name)
    if exact is not None and exact.bl_idname == "ShaderNodeTree"             and exact.get(_VERSION_KEY) is not None:
        group = exact
    else:
        # This group under a name Blender gave it when the plain one was
        # taken. Only ours, and only when the plain name is not ours.
        group = None
        for candidate in bpy.data.node_groups:
            if candidate.bl_idname != "ShaderNodeTree":
                continue
            if candidate.name.startswith(name + ".")                     and candidate.get(_VERSION_KEY) is not None:
                group = candidate
                break
    if group is not None and group.get(_VERSION_KEY) == GROUP_VERSION:
        return group
    if group is None:
        group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    else:
        group.nodes.clear()
        group.interface.clear()
    group[_VERSION_KEY] = GROUP_VERSION
    fill(group)
    return group


def _socket(tree, name, kind, into, **props):
    socket = tree.interface.new_socket(name=name, in_out=into, socket_type=kind)
    for key, value in props.items():
        try:
            setattr(socket, key, value)
        except (AttributeError, TypeError):
            pass
    return socket


def _fill_frame(tree):
    """Where a decal sits: object coordinates in, image coordinates out.

    The frame itself (centre, the two directions across the image, the
    tile size and the turn) arrives on the sockets, so one group serves
    every decal in the file. A negative width or height mirrors that
    axis, which is how SolidWorks asks for a mirrored decal."""
    _socket(tree, "Origin", "NodeSocketVector", "INPUT")
    _socket(tree, "Horizontal", "NodeSocketVector", "INPUT",
            default_value=(1.0, 0.0, 0.0))
    _socket(tree, "Vertical", "NodeSocketVector", "INPUT",
            default_value=(0.0, 1.0, 0.0))
    _socket(tree, "Projection", "NodeSocketVector", "INPUT",
            default_value=(0.0, 0.0, 1.0))
    _socket(tree, "Width", "NodeSocketFloat", "INPUT", default_value=1.0)
    _socket(tree, "Height", "NodeSocketFloat", "INPUT", default_value=1.0)
    _socket(tree, "Rotation", "NodeSocketFloat", "INPUT", subtype="ANGLE")
    _socket(tree, "Vector", "NodeSocketVector", "OUTPUT")
    _socket(tree, "Facing", "NodeSocketFloat", "OUTPUT")

    g = _Graph(tree)
    ins = g.node("NodeGroupInput", -1400, 0)
    outs = g.node("NodeGroupOutput", 700, 0)
    coord = g.node("ShaderNodeTexCoord", -1400, 420)

    rel = g.vmath("SUBTRACT", coord.outputs["Object"], ins.outputs["Origin"],
                  -1150, 300)
    a = g.vmath("DOT_PRODUCT", rel, ins.outputs["Horizontal"], -900, 400)
    b = g.vmath("DOT_PRODUCT", rel, ins.outputs["Vertical"], -900, 200)
    sin_t = g.math("SINE", ins.outputs["Rotation"], None, -900, 0)
    cos_t = g.math("COSINE", ins.outputs["Rotation"], None, -900, -150)

    ar = g.math("ADD", g.math("MULTIPLY", a, cos_t, -650, 450),
                g.math("MULTIPLY", b, sin_t, -650, 300), -400, 400)
    br = g.math("SUBTRACT", g.math("MULTIPLY", b, cos_t, -650, 150),
                g.math("MULTIPLY", a, sin_t, -650, 0), -400, 100)

    u = g.math("ADD", g.math("DIVIDE", ar, ins.outputs["Width"], -150, 400),
               0.5, 100, 400)
    v = g.math("ADD", g.math("DIVIDE", br, ins.outputs["Height"], -150, 100),
               0.5, 100, 100)
    combine = g.node("ShaderNodeCombineXYZ", 400, 300)
    g.link(u, combine.inputs[0])
    g.link(v, combine.inputs[1])
    g.link(combine.outputs[0], outs.inputs["Vector"])

    # SolidWorks does not print a decal through to the far side of a
    # part, so only the faces turned toward the projection take it.
    facing = g.math("GREATER_THAN",
                    g.vmath("DOT_PRODUCT", coord.outputs["Normal"],
                            ins.outputs["Projection"], -150, -300),
                    0.0, 100, -300)
    g.link(facing, outs.inputs["Facing"])


def _fill_mix(tree):
    """One decal over what is already on the face."""
    _socket(tree, "Base Color", "NodeSocketColor", "INPUT",
            default_value=(0.8, 0.8, 0.8, 1.0))
    _socket(tree, "Decal Color", "NodeSocketColor", "INPUT",
            default_value=(1.0, 1.0, 1.0, 1.0))
    _socket(tree, "Decal Alpha", "NodeSocketFloat", "INPUT",
            default_value=1.0, min_value=0.0, max_value=1.0)
    _socket(tree, "Facing", "NodeSocketFloat", "INPUT",
            default_value=1.0, min_value=0.0, max_value=1.0)
    _socket(tree, "Mask", "NodeSocketFloat", "INPUT",
            default_value=1.0, min_value=0.0, max_value=1.0)
    _socket(tree, "Invert Mask", "NodeSocketFloat", "INPUT",
            default_value=0.0, min_value=0.0, max_value=1.0)
    _socket(tree, "Color", "NodeSocketColor", "OUTPUT")

    g = _Graph(tree)
    ins = g.node("NodeGroupInput", -700, 0)
    outs = g.node("NodeGroupOutput", 500, 0)

    factor = g.math("MULTIPLY", ins.outputs["Decal Alpha"],
                    ins.outputs["Facing"], -450, 200)
    factor = g.math("MULTIPLY", factor, ins.outputs["Mask"], -250, 200)
    flipped = g.math("SUBTRACT", 1.0, factor, -250, 0)
    choose = g.node("ShaderNodeMix", -50, 100, data_type="FLOAT")
    g.link(ins.outputs["Invert Mask"], choose.inputs["Factor"])
    g.link(factor, choose.inputs["A"])
    g.link(flipped, choose.inputs["B"])

    mix = g.node("ShaderNodeMix", 250, 0, data_type="RGBA", blend_type="MIX")
    g.link(choose.outputs["Result"], mix.inputs["Factor"])
    g.link(ins.outputs["Base Color"], mix.inputs["A"])
    g.link(ins.outputs["Decal Color"], mix.inputs["B"])
    g.link(mix.outputs["Result"], outs.inputs["Color"])


def decal_projection(g, decal, unit_scale, x0, y0):
    """One "SW Decal Frame" group node, set up for this decal.

    A decal reads its frame differently from an appearance: U is the
    direction it is projected along, V the image's horizontal, and U x V
    its vertical. The image centre is the decal's centre point moved by
    its y value along the horizontal, and the image turns by the decal's
    rotation less the face's own angle. Only faces turned toward the
    projection take the image; SolidWorks does not print it through to the
    far side.

    Pinned on one decal only (usb_case2's logo, 2026-09-15: the logo lands
    where SolidWorks draws it to a fraction of a millimetre in the top
    view, upright, 40 mm long). The x value is not used: on that decal it
    equals the centre point's own distance along the horizontal. A second
    decal with other offsets is what would test this further.

    A mirrored axis travels as a negative width or height, which is what
    the group divides by, so the group needs no switch for it."""
    m = decal.get("mapping") or {}
    face = decal.get("face") or {}
    s = float(unit_scale) if unit_scale else 1.0
    P = _unit(m.get("u"), (0.0, 0.0, 1.0))
    V = m.get("v") or (1.0, 0.0, 0.0)
    dot = sum(float(V[i]) * P[i] for i in range(3))
    H = _unit([float(V[i]) - P[i] * dot for i in range(3)], (1.0, 0.0, 0.0))
    Vt = _cross(P, H)
    c = m.get("centre") or (0.0, 0.0, 0.0)
    y_off = float(m.get("y") or 0.0)
    origin = tuple((float(c[i]) + y_off * H[i]) * s for i in range(3))
    width = max(float(m.get("width") or 1.0), 1e-9) * s
    height = max(float(m.get("height") or m.get("width") or 1.0), 1e-9) * s
    theta = math.radians(float(m.get("rotation") or 0.0) - float(face.get("angle") or 0.0))

    mirror = -1.0 if (m.get("width_mirror") or face.get("mirrored")) else 1.0

    frame = g.node("ShaderNodeGroup", x0, y0,
                   node_tree=_group(FRAME_GROUP, _fill_frame),
                   label=os.path.basename(decal.get("image") or "decal"))
    frame.inputs["Origin"].default_value = origin
    frame.inputs["Horizontal"].default_value = H
    frame.inputs["Vertical"].default_value = Vt
    frame.inputs["Projection"].default_value = P
    frame.inputs["Width"].default_value = width * mirror
    frame.inputs["Height"].default_value =         height * (-1.0 if m.get("height_mirror") else 1.0)
    frame.inputs["Rotation"].default_value = theta
    return frame.outputs["Vector"], frame.outputs["Facing"], x0 + 2 * _X_STEP


def build(mat, spec, rgba, unit_scale=1.0):
    """Fills mat's node tree from an appearance. rgba is the record's own
    colour (the fallback when there is no JSON)."""
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    g = _Graph(tree)
    out = g.node("ShaderNodeOutputMaterial", 2400, 0)
    bsdf = g.node("ShaderNodeBsdfPrincipled", 2100, 0)
    g.link(bsdf.outputs["BSDF"], out.inputs["Surface"])

    spec = spec or {}
    blender = spec.get("blender") or {}
    library = spec.get("library") or {}
    colour = linear(spec.get("colour") or rgba[:3])
    alpha = float(rgba[3]) if len(rgba) > 3 else 1.0
    roughness = float(blender.get("roughness", 0.5))
    metallic = float(blender.get("metallic", 0.0))
    glass = bool(blender.get("glass", False))

    _set(bsdf, "Base Color", (colour[0], colour[1], colour[2], 1.0))
    _set(bsdf, "Metallic", metallic)
    _set(bsdf, "Roughness", roughness)
    specular = spec.get("specular")
    if specular is not None and metallic < 0.5:
        _set(bsdf, "Specular IOR Level", max(0.0, min(1.0, float(specular))))
    tint = spec.get("specular_colour")
    if tint and metallic >= 0.5:
        tint = linear(tint)
        _set(bsdf, "Specular Tint", (tint[0], tint[1], tint[2], 1.0))
    reflectivity = float(spec.get("reflectivity") or 0.0)
    if metallic < 0.5 and not glass and reflectivity > 0.02:
        _set(bsdf, "Coat Weight", max(0.0, min(1.0, reflectivity * 2.0)))
        _set(bsdf, "Coat Roughness", min(roughness, 0.1))

    transparency = float(spec.get("transparency") or 0.0)
    if glass:
        _set(bsdf, "Transmission Weight", 1.0 if transparency > 0.0 else 0.0)
        ior = float(library.get("mtl_ior") or spec.get("ior") or 1.5)
        _set(bsdf, "IOR", ior if ior > 1.0 else 1.5)
    elif alpha < 0.999:
        _set(bsdf, "Alpha", alpha)
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "BLENDED"

    emission = max(float(spec.get("emission") or 0.0),
                   float(library.get("luminousIntensity") or 0.0))
    if emission > 0.0:
        _set(bsdf, "Emission Color", (colour[0], colour[1], colour[2], 1.0))
        _set(bsdf, "Emission Strength", emission)

    mapping = spec.get("mapping") or {}
    y = 400
    base_socket = None
    texture = spec.get("texture")
    image = _image(texture)
    if image is not None:
        vec, mode, x_end = projection(g, mapping, unit_scale, -1800, y)
        tex = g.node("ShaderNodeTexImage", x_end, y, image=image, projection=mode, extension="REPEAT")
        if mode == "BOX":
            tex.projection_blend = 0.15
        g.link(vec, tex.inputs["Vector"])
        base_socket = tex.outputs["Color"]
        y -= 500

    bump_path = spec.get("bump_texture")
    bump = _image(bump_path, non_color=True)
    if bump is not None:
        vec, mode, x_end = projection(g, mapping, unit_scale, -1800, y)
        tex = g.node("ShaderNodeTexImage", x_end, y, image=bump, projection=mode, extension="REPEAT")
        g.link(vec, tex.inputs["Vector"])
        normal_map = str(library.get("bumpIsNormalMap", "")).lower() == "on" \
            or os.path.splitext(bump_path)[0].lower().endswith("_n") \
            or "normalmap" in os.path.basename(bump_path).lower()
        if normal_map:
            nm = g.node("ShaderNodeNormalMap", 1800, y)
            g.link(tex.outputs["Color"], nm.inputs["Color"])
            _set(nm, "Strength", 1.0)
            g.link(nm.outputs["Normal"], bsdf.inputs["Normal"])
        else:
            bn = g.node("ShaderNodeBump", 1800, y)
            g.link(tex.outputs["Color"], bn.inputs["Height"])
            distance = float(library.get("displacementDistance") or 0.001) * float(unit_scale or 1.0)
            _set(bn, "Distance", distance)
            g.link(bn.outputs["Normal"], bsdf.inputs["Normal"])
        y -= 500

    # Decals, in order, each over what is below it.
    for decal in spec.get("decals") or []:
        img = _image(decal.get("image"))
        if img is None:
            continue
        vec, facing, x_end = decal_projection(g, decal, unit_scale, -1800, y)
        tex = g.node("ShaderNodeTexImage", x_end, y, image=img, projection="FLAT", extension="CLIP")
        g.link(vec, tex.inputs["Vector"])
        over = g.node("ShaderNodeGroup", 1500, y,
                      node_tree=_group(MIX_GROUP, _fill_mix),
                      label=os.path.basename(decal.get("image") or "decal"))
        g.link(facing, over.inputs["Facing"])
        g.link(tex.outputs["Color"], over.inputs["Decal Color"])
        g.link(tex.outputs["Alpha"], over.inputs["Decal Alpha"])
        mask = _image(decal.get("mask_image"), non_color=True) if int(decal.get("mask_type") or 0) == 1 else None
        if mask is not None:
            mtex = g.node("ShaderNodeTexImage", x_end, y - 280, image=mask, projection="FLAT", extension="CLIP")
            g.link(vec, mtex.inputs["Vector"])
            g.link(mtex.outputs["Color"], over.inputs["Mask"])
        if decal.get("mask_invert"):
            over.inputs["Invert Mask"].default_value = 1.0
        if base_socket is not None:
            g.link(base_socket, over.inputs["Base Color"])
        else:
            over.inputs["Base Color"].default_value = (
                colour[0], colour[1], colour[2], 1.0)
        base_socket = over.outputs["Color"]
        y -= 600

    if base_socket is not None:
        g.link(base_socket, bsdf.inputs["Base Color"])

    mat.diffuse_color = (colour[0], colour[1], colour[2], alpha)
    mat.metallic = metallic
    mat.roughness = roughness
    return mat
