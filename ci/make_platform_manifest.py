"""Narrow blender_manifest.toml to a single platform + wheel for CI builds.

Usage: python ci/make_platform_manifest.py <src_manifest> <platform> <wheel_filename> <out_manifest>

Example:
  python ci/make_platform_manifest.py blender_manifest.toml windows-x64 \
      cadquery_ocp_novtk-7.9.3.1.1-cp313-cp313-win_amd64.whl \
      CADder/blender_manifest.toml
"""
import re
import sys

src, platform, wheel, out = sys.argv[1:5]

text = open(src, encoding="utf-8").read()

text, n = re.subn(
    r"platforms\s*=\s*\[[^\]]*\]",
    f'platforms = ["{platform}"]',
    text,
    count=1,
)
assert n == 1, "platforms entry not found"

text, n = re.subn(
    r"wheels\s*=\s*\[[^\]]*\]",
    f'wheels = [\n  "./wheels/{wheel}",\n]',
    text,
    count=1,
)
assert n == 1, "wheels entry not found"

open(out, "w", encoding="utf-8").write(text)
print(f"wrote {out}: platform={platform} wheel={wheel}")
