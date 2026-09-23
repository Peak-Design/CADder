# SPDX-License-Identifier: GPL-3.0-or-later
"""Does every STEP file of the corpus import, the way File > Import does?

    blender -b --factory-startup --python-exit-code 1 -P ci/step_corpus_smoke.py

Keep --python-exit-code. Without it Blender exits 0 even when the script
raises, and a test that crashed reads as a test that passed.

Oscar, 2026-09-23: "STEP files no longer import at all. Any STEP file I
try to import comes back with an error ... Add this into the corpus for
the future". Every other smoke imports one or two fixtures, each in its own
session. This one imports every STEP file it can find, one after the other
in one session, through the import operator with a directory and a file
list, as the file browser calls it. Each file must finish and give objects.

The files: the STEP files under ci/fixtures and ci/baselines, and the
corpus of test assemblies of CADder Bridge. That folder is not in the
repository. Give it in CADDER_STEP_CORPUS, or keep a CADder-SW-Bridge
checkout next to this one (test-assemblies). Without it, the tracked files
still run, and the summary says that the corpus was not found.

It also checks what the error says when a file cannot be opened. It said
"STEP file could not be opened. Possibly damaged file." for every failure,
also for a file that the system would not give up. Now a damaged file says
that it is damaged, and a file that the system refuses gives the reason of
the system.
"""
import glob
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_REPO))

import bpy  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module=os.path.basename(_REPO))

FAILS = []
EXTS = (".step", ".stp")


def check(cond, msg):
    if cond:
        print("   ok:", msg)
    else:
        FAILS.append(msg)
        print("   FAIL:", msg)


def corpus_folder():
    folder = os.environ.get("CADDER_STEP_CORPUS", "")
    if folder:
        return folder if os.path.isdir(folder) else None
    sibling = os.path.join(os.path.dirname(_REPO), "CADder-SW-Bridge",
                           "test-assemblies")
    return sibling if os.path.isdir(sibling) else None


def step_files(folder):
    return sorted(f for f in glob.glob(os.path.join(folder, "**", "*"),
                                       recursive=True)
                  if f.lower().endswith(EXTS) and os.path.isfile(f))


def import_file(path):
    """File > Import: a directory and a file list. (result, new objects,
    error reports). A script call raises on an ERROR report, with the text
    that the user sees."""
    before = set(bpy.data.objects)
    errors = []
    try:
        result = bpy.ops.import_scene.occ_import_step(
            filepath=path, directory=os.path.dirname(path) + os.sep,
            files=[{"name": os.path.basename(path)}])
    except RuntimeError as e:
        result = {"CANCELLED"}
        errors.append(str(e))
    new = [o for o in bpy.data.objects if o not in before]
    return result, new, errors


def clear_scene():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for block in (bpy.data.meshes, bpy.data.curves, bpy.data.materials):
        for d in list(block):
            if d.users == 0:
                block.remove(d)


tracked = step_files(os.path.join(_HERE, "fixtures")) + \
    step_files(os.path.join(_HERE, "baselines"))
corpus = corpus_folder()
files = tracked + (step_files(corpus) if corpus else [])
print("step_corpus_smoke: %d tracked file(s), corpus %s" % (
    len(tracked), "%s (%d file(s))" % (corpus, len(files) - len(tracked))
    if corpus else "not found"))

# 1. Every file imports, one after the other in one session.
check(len(tracked) >= 3, "the tracked STEP files are there (%d)" % len(tracked))
done = 0
for path in files:
    result, new, errors = import_file(path)
    name = os.path.relpath(path, corpus) if corpus and path.startswith(corpus) \
        else os.path.relpath(path, _REPO)
    good = result == {"FINISHED"} and new and not errors
    if good:
        done += 1
    else:
        check(False, "%s imports: %s, %d object(s), %s"
              % (name, result, len(new), errors))
    clear_scene()
check(done == len(files), "%d of %d STEP files import" % (done, len(files)))

# 2. What a file that cannot be opened reports.
tmp = tempfile.mkdtemp(prefix="cadder_step_open_")
try:
    damaged = os.path.join(tmp, "damaged.step")
    with open(damaged, "w", encoding="ascii") as f:
        f.write("ISO-10303-21;\nHEADER;\nthis is not a STEP file\n")
    result, new, errors = import_file(damaged)
    check(result == {"CANCELLED"} and not new,
          "a damaged file imports nothing (%s)" % result)
    check(any("damaged.step" in e and "damaged" in e.split("damaged.step", 1)[1]
              for e in errors),
          "it says which file, and that the file is damaged: %s" % errors)

    if os.name == "nt":
        # A file that another handle holds with no sharing: the system
        # refuses to open it, as it refuses a cloud file that does not
        # download. It is not damaged, and the report must not say so.
        import ctypes
        from ctypes import wintypes
        locked = os.path.join(tmp, "locked.step")
        shutil.copy(tracked[0], locked)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE)
        GENERIC_READ, OPEN_EXISTING = 0x80000000, 3
        handle = k32.CreateFileW(locked, GENERIC_READ, 0, None,
                                 OPEN_EXISTING, 0, None)
        check(handle not in (None, wintypes.HANDLE(-1).value),
              "the test holds locked.step with no sharing")
        try:
            result, new, errors = import_file(locked)
        finally:
            k32.CloseHandle(handle)
        check(result == {"CANCELLED"} and not new,
              "a file that the system refuses imports nothing (%s)" % result)
        check(any("locked.step" in e and "cannot read" in e for e in errors),
              "it says that the system cannot read it: %s" % errors)
        check(not any("damaged" in e for e in errors),
              "it does not call the file damaged: %s" % errors)
        # and once the other handle lets go, it imports
        result, new, errors = import_file(locked)
        check(result == {"FINISHED"} and new,
              "the same file imports once the system gives it up (%s)"
              % result)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print("\nstep_corpus_smoke: %d FAIL(s)" % len(FAILS))
    sys.exit(1)
print("\nstep_corpus_smoke: OK")
