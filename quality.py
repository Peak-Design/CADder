# SPDX-License-Identifier: GPL-3.0-or-later
"""How fine a part is cut, the same way on every route.

A STEP, IGES or BREP import, Regenerate, a send from SolidWorks and
Rebuild from CAD all take the same settings, with the same names and the
same numbers:

  Quality              Draft, Balanced, Fine, Ultra or Custom
  Distance, Angle      what Custom cuts to: the largest distance between
                       the mesh and the true surface, and the largest angle
                       one facet may turn through
  Relative Tessellation
                       cut to a share of the size of each feature instead
                       of a length
  Relative Distance    that share

OpenCASCADE and SolidWorks cut a little differently at the same numbers,
so the two routes give close meshes, not identical ones. The relative mode
differs more: OpenCASCADE measures each edge, and SolidWorks measures each
body, because it takes one tolerance for a whole body.

CADder Bridge holds the same table (BodyTessellator.FinenessFor), and a
test on each side reads the other, so the two cannot drift apart.

No bpy here, so it can be tested without Blender.
"""

# name -> (distance in metres, angle in radians)
PRESETS = {
    "DRAFT": (0.002, 0.6),
    "BALANCED": (0.0008, 0.5),
    "FINE": (0.0002, 0.25),
    "ULTRA": (0.00005, 0.1),
}

ITEMS = [
    ("DRAFT", "Draft", "A coarse preview, up to 2 mm from the true surface", 0),
    ("BALANCED", "Balanced", "The default, up to 0.8 mm from the true surface", 1),
    ("FINE", "Fine", "Smooth enough for a close-up, up to 0.2 mm from the true surface", 2),
    ("ULTRA", "Ultra", "The finest, up to 0.05 mm from the true surface", 3),
    ("CUSTOM", "Custom", "Set the distance and the angle yourself", 4),
]

DEFAULT = "BALANCED"
DEFAULT_RELATIVE = 0.005

# Where each name sits on the 0..1 dial of CADder Bridge 1.0.0, which
# takes nothing else. A newer bridge reads the distance and angle instead.
DIAL = {"DRAFT": 0.15, "BALANCED": 0.45, "FINE": 0.75, "ULTRA": 1.0}


def spec(quality, distance=None, angle=None, relative=False,
         relative_distance=DEFAULT_RELATIVE):
    """What to cut to, as a dict that every route reads.

    physical: {"mode": "physical", "lin_m": metres, "ang": radians}
    relative: {"mode": "relative", "lin": share, "ang": radians}

    With Relative Tessellation the quality name is not used: the relative
    distance and the angle are what the user set.
    """
    if relative:
        return {"mode": "relative", "lin": float(relative_distance),
                "ang": float(angle if angle is not None else PRESETS[DEFAULT][1])}
    if quality in PRESETS:
        lin_m, ang = PRESETS[quality]
    else:
        lin_m = distance if distance is not None else PRESETS[DEFAULT][0]
        ang = angle if angle is not None else PRESETS[DEFAULT][1]
    return {"mode": "physical", "lin_m": float(lin_m), "ang": float(ang),
            "quality": quality}


def spec_of(owner):
    """The spec of anything that holds the five settings under their usual
    names: the import dialog, the scene's Mesh Quality settings, or a
    dict of import options."""
    get = owner.get if isinstance(owner, dict) else (
        lambda key, default=None: getattr(owner, key, default))
    return spec(get("quality_preset", DEFAULT),
                get("lin_deflection_len", None),
                get("ang_deflection_rot", None),
                bool(get("tessellation_relative", False)),
                get("lin_deflection_rel", DEFAULT_RELATIVE))


def resolve(s, scale):
    """(linear, angular, relative) for OpenCASCADE. The linear value is in
    the file's own units, or a share when relative. `scale` is metres per
    file unit."""
    if s["mode"] == "relative":
        return s["lin"], s["ang"], True
    lin = s["lin_m"] / scale if scale and scale > 0 else s["lin_m"]
    return lin, s["ang"], False


def cad_request(s):
    """The fields a request to the CAD application carries. `quality` is
    the dial for Bridge 1.0.0. The rest is what a newer bridge cuts to."""
    if s["mode"] == "relative":
        return {"quality": DIAL[DEFAULT], "relative": True,
                "relative_distance": s["lin"], "angle_rad": s["ang"]}
    return {"quality": DIAL.get(s.get("quality"), DIAL[DEFAULT]),
            "relative": False, "chord_m": s["lin_m"], "angle_rad": s["ang"]}
