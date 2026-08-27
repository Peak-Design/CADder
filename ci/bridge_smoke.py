# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the SolidWorks bridge: server up, registry file
written with a token, ping answers, a job posted over real HTTP runs the
rig pipeline on the main thread and returns its stage report, bad tokens
are refused, and stop() removes the registry entry.

Run:  blender -b --factory-startup -P bridge_smoke.py
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from STEPper_NEXT import bridge, rig  # noqa: E402

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.SwToBlender", "version": "smoke"},
    "units": {"length": "meter", "angle": "radian"},
    "frame": {"handedness": "right", "up_axis": "Z",
              "transform_convention": "row_major_4x4_global"},
    "step_export": {"file": "bridge-smoke.step", "ap": "AP214",
                    "sha1": None, "occurrence_matching": None},
    "components": [
        {"id": "c001", "sw_path": "base-1", "step_name": "base",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
        {"id": "c002", "sw_path": "arm-1", "step_name": "arm",
         "step_occurrence_path": None,
         "transform": [[1, 0, 0, 0.05], [0, 1, 0, 0], [0, 0, 1, 0],
                       [0, 0, 0, 1]]},
    ],
    "rigid_groups": [
        {"id": "g000", "name": "base", "components": ["c001"], "grounded": True,
         "frame": None, "bbox_diag": 0.1},
        {"id": "g001", "name": "arm", "components": ["c002"],
         "grounded": False, "frame": None, "bbox_diag": 0.08},
    ],
    "joints": [
        {"id": "j001", "type": "revolute", "parent_group": "g000",
         "child_group": "g001", "origin": [0.05, 0, 0], "axis": [0, 0, 1],
         "secondary_axis": [1, 0, 0], "limits": None},
    ],
    "loops": [],
    "warnings": [],
}


def pump_while(thread, seconds=60.0):
    deadline = time.time() + seconds
    while thread.is_alive() and time.time() < deadline:
        bridge._pump()
        time.sleep(0.02)
    assert not thread.is_alive(), "bridge job never finished"


def request(url, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"X-SWTB-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig.register()
    bridge.start()
    assert bridge.is_running()
    port = bridge.port()

    reg_path = os.path.join(bridge.registry_dir(), "%d.json" % os.getpid())
    with open(reg_path, "r", encoding="utf-8") as f:
        reg = json.load(f)
    assert reg["port"] == port and reg["token"]
    token = reg["token"]
    base = "http://127.0.0.1:%d" % port

    info = request(base + "/swtb/ping", token)
    assert info["ok"] and info["app"] == "blender", info

    try:
        request(base + "/swtb/ping", "wrong-token")
        raise AssertionError("bad token was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403

    with tempfile.NamedTemporaryFile("w", suffix=".rig.json",
                                     delete=False) as f:
        json.dump(MANIFEST, f)
        manifest_path = f.name
    try:
        result = {}

        def client():
            result["resp"] = request(base + "/swtb/import", token, {
                "manifest": manifest_path,
                "steps": {"import": False, "sync_poses": False,
                          "cleanup": True},
            })

        t = threading.Thread(target=client, daemon=True)
        t.start()
        pump_while(t)
    finally:
        os.unlink(manifest_path)

    resp = result["resp"]
    assert resp["ok"], resp
    stages = resp["stages"]
    assert stages["manifest"]["joints"] == 1
    assert stages["match"]["matched"] == 0
    assert sorted(stages["match"]["unmatched"]) == ["c001", "c002"]
    assert stages["rig"]["bones"] == 2, stages["rig"]
    assert "cleanup" in stages
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    assert len(arm.pose.bones) == 2

    # A re-send while the previous rig's armature sits in POSE mode must
    # not freeze: the job sweeps every object's own mode (context.mode only
    # reports the active object) before the rig rebuild deletes the
    # armature (live hang, 2026-08-23).
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    assert arm.mode == "POSE"
    with tempfile.NamedTemporaryFile("w", suffix=".rig.json",
                                     delete=False) as f:
        json.dump(MANIFEST, f)
        manifest_path = f.name
    try:
        result = {}

        def client2():
            result["resp"] = request(base + "/swtb/import", token, {
                "manifest": manifest_path,
                "steps": {"import": False, "sync_poses": False,
                          "cleanup": True},
            })

        t = threading.Thread(target=client2, daemon=True)
        t.start()
        pump_while(t)
    finally:
        os.unlink(manifest_path)
    assert result["resp"]["ok"], result["resp"]
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    assert len(arms) == 1 and len(arms[0].pose.bones) == 2
    stuck = [o.name for o in bpy.data.objects if o.mode != "OBJECT"]
    assert not stuck, "objects left in non-object mode: %s" % stuck

    # An exception that escapes _run_job entirely (even a BaseException)
    # must come back as an HTTP error, not kill the pump: a dead pump
    # leaves the server listening while every send stalls for 30 minutes.
    orig_run_job = bridge._run_job
    bridge._run_job = lambda payload: (_ for _ in ()).throw(
        SystemExit("smoke: escaped exception"))
    try:
        result = {}

        def client3():
            try:
                request(base + "/swtb/import", token, {"steps": {}})
                result["resp"] = "accepted"
            except urllib.error.HTTPError as exc:
                result["resp"] = exc.code

        t = threading.Thread(target=client3, daemon=True)
        t.start()
        pump_while(t)
    finally:
        bridge._run_job = orig_run_job
    assert result["resp"] == 500, result["resp"]
    assert not bridge._state["last_job"]["ok"]

    bridge.stop()
    assert not os.path.exists(reg_path), "registry entry not cleaned up"
    assert not bridge.is_running()

    check_option_parity()

    print("bridge_smoke: OK: ping, auth, a full pipeline job over "
          "HTTP on port %d, and import-option parity" % port)


def check_option_parity():
    """Every option the bridge accepts must really exist on the import
    operator, and every option the SolidWorks side sends must be accepted.

    This is the seam where the two halves drift: an option added to the
    importer and not to the allowlist is dropped in silence, and one the
    add-in sends that the allowlist does not know is reported as ignored
    where nobody looks. Both directions are checked here because the add-in
    and the addon ship separately.
    """
    from STEPper_NEXT import bridge as bridge_mod
    from STEPper_NEXT import main as main_mod

    # The class annotations, not the registered RNA: this smoke drives the
    # bridge module directly and never enables the addon, and the properties
    # are declared either way.
    real = set(main_mod.ImportStepCADOperator.__annotations__)
    allowed = set(bridge_mod._IMPORT_OPTION_KEYS)

    unreal = sorted(allowed - real)
    assert not unreal, (
        "the bridge accepts import options the operator does not have: %s"
        % unreal)

    # What Peak.SwToBlender.SendToBlenderCommand puts in import_options.
    sent_by_addin = {
        "hierarchy_types", "quality_preset", "up_as", "fw_as",
        "import_curves", "group_in_collection", "separate_solids",
    }
    dropped = sorted(sent_by_addin - allowed)
    assert not dropped, (
        "the SolidWorks add-in sends import options the bridge would drop: %s"
        % dropped)
    print("  option parity: %d accepted, all real, %d sent by the add-in, "
          "all accepted" % (len(allowed), len(sent_by_addin)))


main()
