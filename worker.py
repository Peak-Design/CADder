# Background import worker for STEPper NEXT.
#
# Runs inside a HEADLESS Blender instance spawned by background.py:
#   blender -b --factory-startup --python-exit-code 1 \
#           --python worker.py -- <request.json>
#
# It enables the same addon, applies the parent session's relevant addon
# preferences, runs the exact same import operator (full parity with the
# synchronous path by construction) and saves the result to a .blend that
# the interactive session appends. Progress is communicated through stdout
# lines prefixed with "@STEPPER ".

import json
import os
import sys
import threading


def log(payload):
    print("@STEPPER " + json.dumps(payload), flush=True)


def _parent_alive(pid):
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        # OpenProcess can still succeed on an exited process (its object
        # lives while any handle exists). A zero-timeout wait tells us
        # whether it is actually running.
        res = kernel32.WaitForSingleObject(handle, 0)
        kernel32.CloseHandle(handle)
        return res == WAIT_TIMEOUT
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _watch_parent(pid):
    """Exit if the interactive Blender that spawned us disappears, so a
    crashed/closed parent never leaves an orphan worker parsing for hours."""
    import time
    while True:
        time.sleep(3.0)
        if not _parent_alive(pid):
            os._exit(3)


def main():
    import bpy

    argv = sys.argv[sys.argv.index("--") + 1:]
    with open(argv[0], encoding="utf-8") as f:
        request = json.load(f)

    module = request["addon_module"]
    filepath = request["filepath"]
    out_blend = request["out_blend"]
    op_kwargs = request.get("op_kwargs", {})

    parent_pid = request.get("parent_pid")
    if parent_pid:
        threading.Thread(
            target=_watch_parent, args=(parent_pid,), daemon=True).start()

    log({"phase": "startup", "file": filepath})

    # Empty scene so the saved .blend contains only the import result
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module=module)

    # Mirror the parent session's result-affecting addon preferences:
    # factory-startup defaults would silently change the import output.
    addon = bpy.context.preferences.addons.get(module)
    if addon is not None:
        for key, value in request.get("prefs", {}).items():
            try:
                setattr(addon.preferences, key, value)
            except Exception as e:
                print(f"worker: could not apply preference {key}: {e}")

    # Match the parent session's scene unit length. load_step divides
    # the file unit scale by it, so a factory scene at 1.0 would import
    # a different size than the scene the result is appended into.
    unit_scale = request.get("scene_unit_scale")
    if unit_scale:
        bpy.context.scene.unit_settings.scale_length = unit_scale

    log({"phase": "importing"})
    result = bpy.ops.import_scene.occ_import_step(
        filepath=filepath, override_file=os.path.basename(filepath),
        **op_kwargs)
    if "FINISHED" not in result:
        log({"phase": "error", "message": "import operator failed"})
        sys.exit(2)

    n_objects = len(bpy.data.objects)
    log({"phase": "saving", "objects": n_objects})
    bpy.ops.wm.save_as_mainfile(filepath=out_blend, compress=False)
    log({"phase": "done", "objects": n_objects, "blend": out_blend})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log({"phase": "error", "message": str(e)})
        raise
