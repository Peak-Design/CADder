# SPDX-License-Identifier: GPL-3.0-or-later
"""SolidWorks appearances as Blender materials.

The SolidWorks add-in sends each appearance whole (colours, finish, the
library file's own values, the texture and its mapping, the decals on the
faces) as JSON on the material record of the .swmesh. SolidWorks has no PBR
material, so what is built here is a reading of that data: a Principled
BSDF with the colour or image, a roughness from how sharp the reflections
are, metal from the library category, transmission for glass, a normal or
bump map, and decals laid over the base colour.

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


def decal_projection(g, decal, unit_scale, x0, y0):
    """The texture vector and the facing factor for a decal.

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
    decal with other offsets is what would test this further."""
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
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    coord = g.node("ShaderNodeTexCoord", x0, y0)
    rel = g.vmath("SUBTRACT", coord.outputs["Object"], origin, x0 + _X_STEP, y0)
    a = g.vmath("DOT_PRODUCT", rel, H, x0 + 2 * _X_STEP, y0 + 100)
    b = g.vmath("DOT_PRODUCT", rel, Vt, x0 + 2 * _X_STEP, y0 - 100)
    x1 = x0 + 3 * _X_STEP
    ar = g.math("ADD", g.math("MULTIPLY", a, cos_t, x1, y0 + 150),
                g.math("MULTIPLY", b, sin_t, x1, y0 + 50), x1 + _X_STEP, y0 + 100)
    br = g.math("SUBTRACT", g.math("MULTIPLY", b, cos_t, x1, y0 - 50),
                g.math("MULTIPLY", a, sin_t, x1, y0 - 150), x1 + _X_STEP, y0 - 100)
    x1 += 2 * _X_STEP
    mirror = -1.0 if (m.get("width_mirror") or face.get("mirrored")) else 1.0
    u = g.math("ADD", g.math("DIVIDE", ar, width * mirror, x1, y0 + 100), 0.5, x1 + _X_STEP, y0 + 100)
    v = g.math("ADD", g.math("DIVIDE", br, height * (-1.0 if m.get("height_mirror") else 1.0),
                             x1, y0 - 100), 0.5, x1 + _X_STEP, y0 - 100)
    combine = g.node("ShaderNodeCombineXYZ", x1 + 2 * _X_STEP, y0)
    g.link(u, combine.inputs[0])
    g.link(v, combine.inputs[1])
    facing = g.math("GREATER_THAN", g.vmath("DOT_PRODUCT", coord.outputs["Normal"], P,
                                            x1, y0 - 300), 0.0, x1 + _X_STEP, y0 - 300)
    return combine.outputs[0], facing, x1 + 3 * _X_STEP


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
        factor = g.math("MULTIPLY", tex.outputs["Alpha"], facing, x_end + 300, y + 100)
        mask = _image(decal.get("mask_image"), non_color=True) if int(decal.get("mask_type") or 0) == 1 else None
        if mask is not None:
            mtex = g.node("ShaderNodeTexImage", x_end, y - 280, image=mask, projection="FLAT", extension="CLIP")
            g.link(vec, mtex.inputs["Vector"])
            factor = g.math("MULTIPLY", factor, mtex.outputs["Color"], x_end + 300, y - 200)
        if decal.get("mask_invert"):
            factor = g.math("SUBTRACT", 1.0, factor, x_end + 450, y - 200)
        mix = g.node("ShaderNodeMix", 1500, y, data_type="RGBA", blend_type="MIX")
        g.link(factor, mix.inputs["Factor"])
        if base_socket is not None:
            g.link(base_socket, mix.inputs["A"])
        else:
            mix.inputs["A"].default_value = (colour[0], colour[1], colour[2], 1.0)
        g.link(tex.outputs["Color"], mix.inputs["B"])
        base_socket = mix.outputs["Result"]
        y -= 600

    if base_socket is not None:
        g.link(base_socket, bsdf.inputs["Base Color"])

    mat.diffuse_color = (colour[0], colour[1], colour[2], alpha)
    mat.metallic = metallic
    mat.roughness = roughness
    return mat
