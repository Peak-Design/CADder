# SPDX-License-Identifier: GPL-3.0-or-later
"""The quality settings every route shares.

A file import, Regenerate, a send from SolidWorks and Rebuild from CAD
take the same five settings. A name must cut to the same numbers on every
route, or a part that comes over one route looks different from the same
part that came over another.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder import quality  # noqa: E402

_BRIDGE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
    "CADder-SW-Bridge", "sw-addin", "src", "Peak.Cadder")


def _bridge_source(*parts):
    path = os.path.join(_BRIDGE, *parts)
    if not os.path.exists(path):
        pytest.skip("not a checkout of both repos")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_every_name_has_numbers_and_a_dial():
    names = [item[0] for item in quality.ITEMS if item[0] != "CUSTOM"]
    assert names == ["DRAFT", "BALANCED", "FINE", "ULTRA"]
    assert sorted(names) == sorted(quality.PRESETS) == sorted(quality.DIAL)


def test_the_names_run_coarse_to_fine():
    distances = [quality.PRESETS[n][0] for n in ("DRAFT", "BALANCED", "FINE", "ULTRA")]
    angles = [quality.PRESETS[n][1] for n in ("DRAFT", "BALANCED", "FINE", "ULTRA")]
    assert distances == sorted(distances, reverse=True)
    assert angles == sorted(angles, reverse=True)


def test_a_name_is_its_preset_whatever_custom_holds():
    s = quality.spec("FINE", distance=0.123, angle=1.0)
    assert (s["lin_m"], s["ang"]) == quality.PRESETS["FINE"]


def test_custom_is_the_distance_and_angle():
    s = quality.spec("CUSTOM", distance=0.003, angle=0.7)
    assert s["mode"] == "physical"
    assert (s["lin_m"], s["ang"]) == (0.003, 0.7)


def test_relative_takes_the_share_and_the_angle():
    s = quality.spec("DRAFT", distance=0.003, angle=0.7, relative=True,
                     relative_distance=0.01)
    assert s == {"mode": "relative", "lin": 0.01, "ang": 0.7}


def test_the_dialog_the_scene_and_a_dict_read_alike():
    class Owner:
        quality_preset = "CUSTOM"
        lin_deflection_len = 0.004
        ang_deflection_rot = 0.3
        tessellation_relative = False
        lin_deflection_rel = 0.005
    as_dict = {k: getattr(Owner, k) for k in dir(Owner) if not k.startswith("_")}
    assert quality.spec_of(Owner()) == quality.spec_of(as_dict)
    # Import options that say nothing cut at the default.
    assert quality.spec_of({})["lin_m"] == quality.PRESETS[quality.DEFAULT][0]


def test_a_distance_turns_into_file_units():
    # A millimetre file: 1 file unit is 0.001 m.
    lin, ang, relative = quality.resolve(quality.spec("DRAFT"), 0.001)
    assert lin == pytest.approx(2.0)
    assert ang == 0.6 and relative is False


def test_a_share_stays_a_share():
    s = quality.spec("BALANCED", angle=0.4, relative=True, relative_distance=0.02)
    assert quality.resolve(s, 0.001) == (0.02, 0.4, True)


def test_the_cad_request_carries_both_the_dial_and_the_numbers():
    fine = quality.cad_request(quality.spec("FINE"))
    assert fine == {"quality": 0.75, "relative": False,
                    "chord_m": 0.0002, "angle_rad": 0.25}
    custom = quality.cad_request(quality.spec("CUSTOM", 0.003, 0.7))
    assert custom["chord_m"] == 0.003 and custom["quality"] == quality.DIAL["BALANCED"]
    rel = quality.cad_request(quality.spec("DRAFT", angle=0.5, relative=True,
                                           relative_distance=0.01))
    assert rel == {"quality": 0.45, "relative": True,
                   "relative_distance": 0.01, "angle_rad": 0.5}


def test_the_dials_match_the_bridge():
    """The dial is what Bridge 1.0.0 reads. Read the C# side, do not trust
    a copy."""
    text = _bridge_source("SendToBlenderCommand.cs")
    body = text[text.index("QualityDial(string preset)"):]
    body = body[:body.index("}", body.index("switch"))]
    found = dict(re.findall(r'case "(\w+)": return ([0-9.]+);', body))
    found["BALANCED"] = re.search(r"default: return ([0-9.]+);", body).group(1)
    assert {k: float(v) for k, v in found.items()} == quality.DIAL


def test_the_presets_match_the_bridge():
    """BodyTessellator.FinenessFor cuts each name to these numbers."""
    text = _bridge_source("Sw", "BodyTessellator.cs")

    def array(name):
        m = re.search(name + r"\s*=\s*\{([^}]*)\}", text)
        return [float(v) for v in m.group(1).split(",")]
    dials, chords, angles = array("Dials"), array("Chords"), array("Angles")
    for name, (distance, angle) in quality.PRESETS.items():
        i = dials.index(quality.DIAL[name])
        assert chords[i] == pytest.approx(distance), name
        assert angles[i] == pytest.approx(angle), name
