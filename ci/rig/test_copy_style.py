# SPDX-License-Identifier: GPL-3.0-or-later
"""The add-on's copy carries no em dash.

Readers take one as a sign that a machine wrote the text, and everything
here is read by someone: docstrings, comments, the panel labels and the
messages an operator reports. The replacement is the punctuation the
sentence needs, usually a colon, a comma, brackets or two sentences, and
never a hyphen or an en dash in the same role.
"""

import io
import os

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIP = {".git", "__pycache__", "MaterialDB", "wheels", "lib", ".claude",
        "assets", "OCP", "cadquery_ocp_novtk.libs"}
SUFFIXES = (".py", ".md", ".toml", ".yml", ".yaml", ".json")


def _offences():
    found = []
    for folder, dirs, names in os.walk(ADDON):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in names:
            if not name.endswith(SUFFIXES):
                continue
            path = os.path.join(folder, name)
            try:
                text = io.open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.split("\n"), 1):
                if EM_DASH in line:
                    found.append("%s:%d: em dash" % (os.path.relpath(path, ADDON), number))
                elif " %s " % EN_DASH in line:
                    found.append("%s:%d: en dash as punctuation"
                                 % (os.path.relpath(path, ADDON), number))
    return found


def _client_names():
    """The patterns in .claude/client-names.txt, which git ignores, so the
    list is not published by the test that enforces it."""
    import re
    path = os.path.join(ADDON, ".claude", "client-names.txt")
    try:
        lines = io.open(path, encoding="utf-8").read().split("\n")
    except OSError:
        return []
    return [re.compile(line.strip(), re.IGNORECASE) for line in lines
            if line.strip() and not line.strip().startswith("#")]


def test_no_client_name_in_anything_published():
    names = _client_names()
    if not names:
        return                          # no list on this machine
    found = []
    for folder, dirs, files in os.walk(ADDON):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in files:
            if not name.endswith(SUFFIXES):
                continue
            path = os.path.join(folder, name)
            try:
                text = io.open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.split("\n"), 1):
                if any(n.search(line) for n in names):
                    found.append("%s:%d" % (os.path.relpath(path, ADDON), number))
    assert not found, ("Replace the client name with a description of the "
                       "model:\n  " + "\n  ".join(found))


def test_no_em_dash_in_anything_someone_reads():
    offences = _offences()
    assert not offences, (
        "Use the punctuation the sentence needs (a colon, a comma, brackets, or "
        "two sentences):\n  " + "\n  ".join(offences))
