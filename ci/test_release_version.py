"""The release workflow stops when the version numbers do not agree, and it
tags the commit it built.

Run:  python -m pytest ci/test_release_version.py -q

The zip names come from blender_manifest.toml, the update check compares
with bl_info and the release is named after the tag. Nothing compared the
three, so a tag pushed before the version bump published a release that
told every user an update was available, also after they installed it.

A release started by hand with a version created its tag on the default
branch, not on the commit that was built, so the tag and the zips did not
match.
"""
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "release.yml")


def _checker():
    path = os.path.join(HERE, "check_version.py")
    assert os.path.isfile(path), "ci/check_version.py is missing"
    spec = importlib.util.spec_from_file_location("check_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root, manifest, bl_info):
    with open(os.path.join(root, "blender_manifest.toml"), "w",
              encoding="utf-8") as f:
        f.write('schema_version = "1.0.0"\nid = "cadder"\n'
                'version = "%s"\n' % manifest)
    with open(os.path.join(root, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("import os\n\nbl_info = {\n    \"name\": \"CADder\",\n"
                "    \"version\": %r,\n}\n" % (bl_info,))


def test_the_repository_agrees_with_itself():
    check = _checker()
    assert check.problems(check.manifest_version(),
                          check.bl_info_version()) == []


def test_files_that_disagree_are_refused(tmp_path):
    check = _checker()
    _write(str(tmp_path), "1.0.2", (1, 0, 1))
    found = check.problems(check.manifest_version(str(tmp_path)),
                           check.bl_info_version(str(tmp_path)))
    assert len(found) == 1 and "bl_info" in found[0]


def test_files_that_agree_pass(tmp_path):
    check = _checker()
    _write(str(tmp_path), "1.0.2", (1, 0, 2))
    assert check.problems(check.manifest_version(str(tmp_path)),
                          check.bl_info_version(str(tmp_path)),
                          "v1.0.2") == []


def test_a_tag_ahead_of_the_files_is_refused():
    check = _checker()
    found = check.problems("1.0.1", "1.0.1", "v1.0.2")
    assert len(found) == 1 and "v1.0.1" in found[0]


def test_a_tag_with_a_suffix_is_refused():
    check = _checker()
    assert check.problems("1.1.0", "1.1.0", "v1.1.0-rc1")
    assert check.problems("1.1.0-rc1", "1.1.0", "v1.1.0-rc1")


def test_no_tag_compares_the_files_only():
    check = _checker()
    assert check.problems("1.0.1", "1.0.1", "") == []


def _jobs():
    yaml = pytest.importorskip("yaml")
    with open(WORKFLOW, encoding="utf-8") as f:
        return yaml.safe_load(f)["jobs"]


def _needs(job):
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def test_the_workflow_checks_before_it_builds():
    jobs = _jobs()
    checkers = [name for name, job in jobs.items()
                if any("ci/check_version.py" in (step.get("run") or "")
                       for step in job.get("steps", []))]
    assert checkers, "no job runs ci/check_version.py"
    for name, job in jobs.items():
        if name.startswith("build-"):
            assert set(_needs(job)) & set(checkers), \
                "%s does not wait for the version check" % name


def test_the_release_tags_the_commit_it_built():
    jobs = _jobs()
    steps = [step for step in jobs["release"]["steps"]
             if "action-gh-release" in (step.get("uses") or "")]
    assert steps, "no release step"
    assert steps[0].get("with", {}).get("target_commitish") \
        == "${{ github.sha }}"
