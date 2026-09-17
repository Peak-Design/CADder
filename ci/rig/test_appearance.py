# SPDX-License-Identifier: GPL-3.0-or-later
"""What an appearance carries, and what it must never do.

The library block of an appearance is not written by the addon or by the
CAD add-in: it is the lines of the appearance file of the CAD application,
copied as they are written there. So it holds whatever that file holds, and
an import must never stop on one of them.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig.appearance import number  # noqa: E402


def test_a_plain_number_comes_back_as_itself():
    assert number(0.65) == 0.65
    assert number(2) == 2.0
    assert number("0.7") == 0.7


def test_the_separator_of_a_library_file_is_not_a_number():
    # One dialect of the appearance file separates its settings with
    # commas, so a value arrives as "0 ," and the import stopped on it
    # (Conveyor12k-A00, Oscar, 2026-09-17).
    assert number("0 ,") == 0.0
    assert number("0.2997 ,") == 0.2997


def test_the_first_number_of_several_is_the_answer():
    assert number("1 0.976471 0.839216") == 1.0


def test_anything_with_no_number_in_it_is_the_fallback():
    for held in ("on", "", '"" ,', None, True, False, "polishedgold"):
        assert number(held, 0.5) == 0.5
