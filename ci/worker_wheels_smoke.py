# SPDX-License-Identifier: GPL-3.0-or-later
"""The background worker must not uninstall the wheels of extensions.

    blender -b --factory-startup --python-exit-code 1 -P ci/worker_wheels_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the
script raises, and a test that crashed reads as a test that passed.

The worker emptied its scene with read_factory_settings. That also resets
the preferences, and Blender then uninstalled the wheels of all extensions
from the extensions folder that every session shares. The OCP DLLs of the
session that started the worker were open, so Blender deleted them at the
next start. CADder then did not load: the add-on showed as enabled, with
no tab, no preferences and no Import entry (Blender 5.2.2, 2026-10-02).

This smoke installs a small extension with a wheel in a temporary user
folder, runs the worker's _empty_scene in a factory-startup Blender, and
checks that the wheel is still there. A control run of
read_factory_settings shows that this Blender removes the wheel, so the
check can see the fault.
"""
import os
import subprocess
import sys
import tempfile
import zipfile

import bpy

_HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(os.path.dirname(_HERE), "worker.py")

FAILS = []


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


MANIFEST = """schema_version = "1.0.0"
id = "cadder_wheel_probe"
version = "1.0.0"
name = "CADder Wheel Probe"
tagline = "Test extension with one wheel"
maintainer = "CADder tests"
type = "add-on"
blender_version_min = "4.2.0"
license = ["SPDX:GPL-3.0-or-later"]
wheels = ["./wheels/probe_wheel-1.0-py3-none-any.whl"]
"""

ADDON = "def register():\n    pass\n\n\ndef unregister():\n    pass\n"

# Run in the second Blender: load worker.py as a module, as the worker does
# not import it, and call the step under test.
RESET = """import importlib.util, sys
import bpy
how = sys.argv[sys.argv.index("--") + 1]
if how == "worker":
    spec = importlib.util.spec_from_file_location("cadder_worker", sys.argv[-1])
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    worker._empty_scene()
else:
    bpy.ops.wm.read_factory_settings(use_empty=True)
print("RESET done", how, len(bpy.data.objects))
"""


def make_extension(folder):
    wheel = os.path.join(folder, "probe_wheel-1.0-py3-none-any.whl")
    info = "probe_wheel-1.0.dist-info/"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("probe_wheel/__init__.py", "VALUE = 1\n")
        z.writestr(info + "METADATA",
                   "Metadata-Version: 2.1\nName: probe_wheel\nVersion: 1.0\n")
        z.writestr(info + "WHEEL", "Wheel-Version: 1.0\nGenerator: smoke\n"
                   "Root-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(info + "RECORD", "probe_wheel/__init__.py,,\n"
                   + "".join(info + n + ",,\n" for n in ("METADATA", "WHEEL", "RECORD")))
    ext = os.path.join(folder, "cadder_wheel_probe.zip")
    with zipfile.ZipFile(ext, "w") as z:
        z.writestr("blender_manifest.toml", MANIFEST)
        z.writestr("__init__.py", ADDON)
        z.write(wheel, "wheels/" + os.path.basename(wheel))
    return ext


def blender(env, *args):
    run = subprocess.run([bpy.app.binary_path] + list(args), env=env,
                         capture_output=True, text=True, timeout=300)
    return run.returncode, run.stdout + run.stderr


def installed(user):
    site = os.path.join(user, "extensions", ".local", "lib",
                        "python%d.%d" % sys.version_info[:2], "site-packages")
    return os.path.isfile(os.path.join(site, "probe_wheel", "__init__.py"))


def fresh_user(root, name, ext):
    user = os.path.join(root, name)
    env = dict(os.environ, BLENDER_USER_RESOURCES=user)
    code, out = blender(env, "--command", "extension", "install-file",
                        "-r", "user_default", "-e", ext)
    if code != 0 or not installed(user):
        print(out[-2000:])
    return user, env


def reset(env, how):
    script = os.path.join(os.path.dirname(env["BLENDER_USER_RESOURCES"]), "reset.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(RESET)
    code, out = blender(env, "-b", "--factory-startup", "--python-exit-code", "1",
                        "--python", script, "--", how, WORKER)
    if code != 0 or "RESET done" not in out:
        print(out[-2000:])
    return code == 0 and "RESET done" in out


with tempfile.TemporaryDirectory(prefix="cadder_wheels_") as root:
    ext = make_extension(root)

    print("1. the worker's empty scene keeps the wheels")
    user, env = fresh_user(root, "worker", ext)
    check(installed(user), "the test extension installs its wheel")
    check(reset(env, "worker"), "the worker's _empty_scene runs")
    check(installed(user), "the wheel is still installed after _empty_scene")
    check(not os.path.exists(os.path.join(user, "config", "stale-pending")),
          "nothing waits to be deleted at the next start")

    print("2. control: read_factory_settings")
    user, env = fresh_user(root, "control", ext)
    ran = reset(env, "factory")
    if ran and not installed(user):
        print("   ok: this Blender uninstalls the wheel on a factory reset, "
              "so check 1 can see the fault")
    else:
        print("   note: this Blender keeps the wheel on a factory reset, so "
              "check 1 proves less here")

if FAILS:
    print("worker_wheels_smoke: %d failure(s)" % len(FAILS))
    sys.exit(1)
print("worker_wheels_smoke: all checks passed")
