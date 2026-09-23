"""Stop a release when its version numbers do not agree.

Usage: python ci/check_version.py [tag]

Three places hold the version, and each one does a different job:

- blender_manifest.toml gives the zip names and the version Blender shows.
- bl_info in __init__.py is what the update check compares with the latest
  release.
- The tag names the release and the download table in its notes.

A release where they differ goes wrong for every user. A tag v1.0.2 on a
commit that still says 1.0.1 publishes cadder-1.0.1 zips under the name
v1.0.2, and every 1.0.1 install then says an update is available, also
after the user installs it. A tag with a suffix (v1.1.0-rc1) becomes the
latest release, and the update check reads it as 1.1.0.

With no tag (a test build), only the two files are compared.
"""
import ast
import os
import re
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Three numbers and nothing else. bl_info holds a tuple of numbers, so a
# suffix can never agree with it.
PLAIN = re.compile(r"^\d+\.\d+\.\d+$")


def manifest_version(root=ROOT):
    with open(os.path.join(root, "blender_manifest.toml"), "rb") as f:
        return str(tomllib.load(f)["version"])


def bl_info_version(root=ROOT):
    """bl_info["version"] from __init__.py, read as text. Importing the
    package needs Blender."""
    with open(os.path.join(root, "__init__.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "bl_info"
                for t in node.targets):
            info = ast.literal_eval(node.value)
            return ".".join(str(p) for p in info["version"])
    raise ValueError("__init__.py has no bl_info")


def problems(manifest, bl_info, tag=""):
    """What is wrong, as one line each. An empty list means the release
    can go ahead."""
    found = []
    if not PLAIN.match(manifest):
        found.append(
            'The version "%s" in blender_manifest.toml is not three numbers '
            '(for example 1.0.2).' % manifest)
    if manifest != bl_info:
        found.append(
            "The version in blender_manifest.toml (%s) and bl_info in "
            "__init__.py (%s) are not the same. Set the two to the same "
            "version." % (manifest, bl_info))
    if tag and tag != "v" + manifest:
        found.append(
            'The tag "%s" does not agree with version %s in '
            "blender_manifest.toml. The tag must be v%s." % (
                tag, manifest, manifest))
    return found


def main(argv):
    tag = argv[1].strip() if len(argv) > 1 else ""
    manifest = manifest_version()
    found = problems(manifest, bl_info_version(), tag)
    for line in found:
        print("ERROR: " + line)
    if found:
        return 1
    places = ("blender_manifest.toml, bl_info and the tag" if tag
              else "blender_manifest.toml and bl_info")
    print("Version %s is the same in %s." % (manifest, places))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
