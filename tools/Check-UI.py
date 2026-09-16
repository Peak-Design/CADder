#!/usr/bin/env python3
"""Check the user interface text against the Blender Human Interface
Guidelines.

    python tools\\Check-UI.py                 the whole addon
    python tools\\Check-UI.py rig\\ui.py       one file

What it looks for:

  * A label that is not in title case, or that is padded with "Use",
    "Enable", "Activate" or "Is".
  * A label that ends with a colon. A heading needs no colon, and a
    heading row is usually better as a sub-panel.
  * A tooltip that ends with a period, opens with "Enables" or
    "Whether", holds a pronoun, an abbreviation or a contraction.
  * A sentence in the interface that holds a pronoun, a contraction or
    an implementation name such as value_at_rest.

It reads the source as text, so it finds what a reader of the panel
would see without running Blender.

The check is a guide, not a judge. A label that breaks a rule for a
reason is fine: put the reason in a comment above it and add it to
ALLOWED below.
"""
import io
import os
import re
import sys

QUOTED = r'"((?:[^"\\]|\\.)*)"'
LABEL = re.compile(r"bl_label\s*=\s*" + QUOTED)
DESC = re.compile(r"(?:bl_description\s*=|description\s*=)\s*\(?\s*" + QUOTED, re.S)
LABEL_TEXT = re.compile(r"\.label\(\s*text\s*=\s*" + QUOTED)
OP_TEXT = re.compile(r"\.operator\([^)]*?text\s*=\s*" + QUOTED, re.S)
# Only a property declaration carries a label. A driver variable and
# a data-block also take name=, and neither is interface text.
NAME = re.compile(r"bpy\.props\.\w+\(\s*name\s*=\s*" + QUOTED, re.S)

# The words MLA title case keeps lowercase inside a title.
# "up" and "off" are missing on purpose: in a label they are nearly
# always the particle of a phrasal verb, as in Clean Up, and MLA
# capitalizes those.
SMALL = {"a", "an", "the", "and", "or", "but", "nor", "for", "from", "to",
         "in", "on", "of", "at", "by", "as", "with", "into", "over", "per",
         "than"}
PADS = ("Enable ", "Use ", "Activate ", "Is ", "Enables ", "Uses ")
WEAK = ("Enables", "Enable ", "Activates", "Activate ", "Whether", "If ",
        "This ")
ABBREV = re.compile(r"\b(verts?|rot|loc|fac|info|db|params|config|dir|img)\b",
                    re.I)
PRONOUN = re.compile(r"\byou\b|\byour\b", re.I)
IMPLEMENTATION = re.compile(r"\b[a-z]+_[a-z_]+\b")

# Text that breaks a rule on purpose.
ALLOWED = {
    # Blender itself names an editor "Info", so the word is its own.
    "Info",
    # The file extensions of the formats, as the file browser shows them.
    "STEP (.step/.stp)", "IGES (.iges/.igs)", "BREP (.brep/.brp)",
}


def title_problems(text):
    if text in ALLOWED:
        return []
    bad = []
    words = text.split()
    if words and words[0][:1].islower():
        bad.append("first word lowercase")
    for word in words[1:]:
        core = word.strip("()[[]:,.")
        if core and core[:1].islower() and core.lower() not in SMALL:
            bad.append("lowercase " + core)
    for i, word in enumerate(words):
        core = word.strip("()[]:,.")
        if (i not in (0, len(words) - 1) and core.lower() in SMALL
                and core[:1].isupper()):
            bad.append("capitalized " + core)
    for pad in PADS:
        if text.startswith(pad):
            bad.append("padded label")
    if text.endswith(":"):
        bad.append("trailing colon")
    return bad


def is_heading(text):
    """A short label with no sentence punctuation is a heading, and a
    heading takes title case. A whole sentence does not."""
    return len(text.split()) <= 4 and not text.endswith(".")


def sentence_problems(text):
    if text in ALLOWED:
        return []
    bad = []
    if PRONOUN.search(text):
        bad.append("pronoun")
    if "n't" in text:
        bad.append("contraction")
    if IMPLEMENTATION.search(text):
        bad.append("implementation name")
    return bad


def tooltip_problems(text):
    bad = []
    if text.endswith(".") and ". " not in text:
        bad.append("trailing period")
    for weak in WEAK:
        if text.startswith(weak):
            bad.append("weak opener")
    if PRONOUN.search(text):
        bad.append("pronoun")
    if ABBREV.search(text):
        bad.append("abbreviation")
    if "n't" in text:
        bad.append("contraction")
    return bad


def check(path):
    try:
        source = io.open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return []
    found = []

    def add(kind, match, problems):
        line = source[:match.start()].count("\n") + 1
        for problem in problems:
            found.append((kind, line, match.group(1), problem))

    for m in LABEL.finditer(source):
        add("label", m, title_problems(m.group(1)))
    for m in NAME.finditer(source):
        add("property", m, title_problems(m.group(1)))
    for m in OP_TEXT.finditer(source):
        add("button", m, title_problems(m.group(1)))
    for m in LABEL_TEXT.finditer(source):
        text = m.group(1)
        if "%" in text or "{" in text or text.startswith("http"):
            continue                    # a data line or a link, not a heading
        if is_heading(text):
            add("text", m, title_problems(text))
        else:
            add("text", m, sentence_problems(text))
    for m in DESC.finditer(source):
        add("tooltip", m, tooltip_problems(m.group(1)))
    return found


def files(argv):
    if argv:
        return argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in {".git", "__pycache__", "OCP", "wheels",
                                "native_libs", "ci", "tools", "assets",
                                "cadquery_ocp_novtk.libs"}]
        out += [os.path.join(base, n) for n in names if n.endswith(".py")]
    return out


def main(argv):
    total = 0
    for path in files(argv):
        found = check(path)
        if not found:
            continue
        print("==== %s ====" % os.path.relpath(path))
        for kind, line, text, problem in found:
            print("  %-9s %5d  %-46s %s"
                  % (kind, line, repr(text[:44]), problem))
        total += len(found)
    if total:
        print("\nUI check: %d problem(s)" % total)
        return 1
    print("UI check: the interface text follows the guidelines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
