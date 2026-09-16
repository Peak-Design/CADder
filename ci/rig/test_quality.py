# SPDX-License-Identifier: GPL-3.0-or-later
"""The quality dial of Update from CAD.

The CAD Link panel offers the same four names as the Export Options of
the CAD add-in. A name must mean the same chord over either route, or a
part that comes over an update looks different from the same part that
came over a send.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import ui  # noqa: E402


class Settings:
    def __init__(self, name, factor=0.5):
        self.update_quality = name
        self.update_quality_factor = factor


def test_every_name_has_a_dial():
    names = [item[0] for item in ui.QUALITY_ITEMS if item[0] != "CUSTOM"]
    assert sorted(names) == sorted(ui.QUALITY_DIAL)


def test_the_names_run_coarse_to_fine():
    dials = [ui.QUALITY_DIAL[n] for n in ("DRAFT", "BALANCED", "FINE", "ULTRA")]
    assert dials == sorted(dials)
    assert 0.0 <= dials[0] and dials[-1] <= 1.0


def test_custom_takes_the_factor():
    assert ui.quality_dial(Settings("CUSTOM", 0.31)) == 0.31


def test_a_name_ignores_the_factor():
    assert ui.quality_dial(Settings("BALANCED", 0.31)) == 0.45


def test_an_unknown_name_still_answers():
    # An old .blend can hold a name this build no longer offers.
    assert 0.0 <= ui.quality_dial(Settings("NONSENSE")) <= 1.0


def test_the_dials_match_the_cad_addin():
    """The C# side is the source: read it, do not trust a copy."""
    source = os.path.join(
        r"C:\PeakDesign\CADder-SW-Bridge\sw-addin\src\Peak.Cadder",
        "SendToBlenderCommand.cs")
    if not os.path.exists(source):
        return                      # not a checkout of both repos
    with open(source, "r", encoding="utf-8") as fh:
        text = fh.read()
    body = text.split("QualityDial(string preset)")[1].split("}")[0]
    found = dict(re.findall(r'case "(\w+)": return ([\d.]+);', body))
    default = re.search(r"default: return ([\d.]+);", body).group(1)
    found["BALANCED"] = default
    assert {k: float(v) for k, v in found.items()} == ui.QUALITY_DIAL
