# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the CAD Link bridge: server up, registry file
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

from CADder import bridge, rig  # noqa: E402

MANIFEST = {
    "manifest_version": "1.0.0",
    "generator": {"name": "Peak.Cadder", "version": "smoke"},
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


def request(url, token, payload=None, header="X-CADLink-Token"):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={header: token, "Content-Type": "application/json"})
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

    info = request(base + "/cadlink/ping", token)
    assert info["ok"] and info["app"] == "blender", info

    try:
        request(base + "/cadlink/ping", "wrong-token")
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
            result["resp"] = request(base + "/cadlink/import", token, {
                "manifest": manifest_path,
                "steps": {"import": False, "sync_poses": False,
                          "cleanup": True},
                # Where SolidWorks was looking. A background Blender has no
                # 3D view to turn, so this asks only that a job carries it
                # through without tripping over it.
                "view": {"orientation": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                         "box": [-0.1, -0.1, -0.1, 0.1, 0.1, 0.1]},
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
            result["resp"] = request(base + "/cadlink/import", token, {
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

    # A pose push: the CAD application moved a part and says where it is
    # now. No geometry, no manifest file, no rig rebuild beyond what the
    # new poses need. The manifest of the send above is still in the
    # scene, which is what the push writes into.
    moved_rows = [1, 0, 0, 0.45, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    result = {}

    def client_poses():
        result["resp"] = request(base + "/cadlink/import", token, {
            "poses": {"document": "bridge-smoke", "components": [
                {"id": "c002", "sw_path": "arm-1", "transform": moved_rows},
            ]},
        })

    t = threading.Thread(target=client_poses, daemon=True)
    t.start()
    pump_while(t)
    resp = result["resp"]
    assert resp["ok"], resp
    assert resp["stages"]["poses"]["components"] == 1, resp["stages"]
    from CADder.rig import ui as rig_ui
    arm_component = next(c for c in rig_ui._STATE["manifest"].components
                         if c.id == "c002")
    assert abs(arm_component.transform[0][3] - 0.45) < 1e-9,         "the push did not reach the manifest: %s" % (arm_component.transform,)

    # A push into a scene with no manifest is refused, and says what to do.
    manifest_was = rig_ui._STATE["manifest"]
    rig_ui._STATE["manifest"] = None
    result = {}

    def client_no_manifest():
        try:
            result["resp"] = request(base + "/cadlink/import", token, {
                "poses": {"components": [
                    {"id": "c002", "sw_path": "arm-1", "transform": moved_rows},
                ]},
            })
        except urllib.error.HTTPError as exc:
            result["resp"] = json.loads(exc.read().decode("utf-8"))

    t = threading.Thread(target=client_no_manifest, daemon=True)
    t.start()
    pump_while(t)
    rig_ui._STATE["manifest"] = manifest_was
    assert not result["resp"]["ok"], result["resp"]
    assert "Send the assembly" in result["resp"]["error"], result["resp"]

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
                request(base + "/cadlink/import", token, {"steps": {}})
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

    # The stall watchdog is faulthandler's C-level timer, re-armed by the
    # pump: it fires when the main thread stays away from the pump, with
    # or without the interpreter lock. A Python-thread watchdog cannot
    # fire while the main thread sits inside a bpy call, which is exactly
    # the freeze it was for (live 2026-09-14). Here the main thread simply
    # sleeps past a 1 s stall window.
    stall_path = bridge.stall_log_path()
    bridge._STALL_S = 1
    bridge._last_beat = 0.0
    bridge._pump()
    time.sleep(2.5)
    with open(stall_path, "r", encoding="utf-8", errors="replace") as f:
        stall = f.read()
    assert "job started" in stall and "job finished" in stall, stall[:400]
    assert "most recent call first" in stall and "bridge_smoke.py" in stall, \
        "no stack dump after a %ds stall:\n%s" % (bridge._STALL_S, stall[:800])
    bridge._STALL_S = 60

    bridge.stop()
    assert not os.path.exists(reg_path), "registry entry not cleaned up"
    assert not bridge.is_running()

    check_option_parity()

    print("bridge_smoke: OK: ping, auth, a full pipeline job over "
          "HTTP on port %d, a pose push into the manifest, a push with no "
          "manifest refused, and import-option parity" % port)


def check_option_parity():
    """Every option the bridge accepts must really exist on the import
    operator, and every option the SolidWorks side sends must be accepted.

    This is the seam where the two halves drift: an option added to the
    importer and not to the allowlist is dropped in silence, and one the
    add-in sends that the allowlist does not know is reported as ignored
    where nobody looks. Both directions are checked here because the add-in
    and the addon ship separately.
    """
    from CADder import bridge as bridge_mod
    from CADder import main as main_mod

    # The class annotations, not the registered RNA: this smoke drives the
    # bridge module directly and never enables the addon, and the properties
    # are declared either way.
    real = set(main_mod.ImportStepCADOperator.__annotations__)
    allowed = set(bridge_mod._IMPORT_OPTION_KEYS)

    unreal = sorted(allowed - real)
    assert not unreal, (
        "the bridge accepts import options the operator does not have: %s"
        % unreal)

    # The direction that used to go unchecked: an option the OPERATOR gained
    # that the bridge does not know is dropped in silence, and a SolidWorks
    # user sending it never learns why it had no effect. Only the file and
    # UI properties the bridge fills in itself are exempt (2026-09-14: the
    # UV rework added eight options and the bridge forwarded none of them).
    not_forwarded = {"filter_glob", "filepath", "files", "directory",
                     "override_file"}
    unforwarded = sorted(real - allowed - not_forwarded)
    assert not unforwarded, (
        "the operator has import options the bridge never forwards: %s"
        % unforwarded)

    # What Peak.Cadder.SendToBlenderCommand puts in import_options.
    sent_by_addin = {
        "hierarchy_types", "quality_preset", "up_as", "fw_as",
        "import_curves", "group_in_collection", "separate_solids",
        "tris_to_quads", "uv_unwrap_compound",
    }
    dropped = sorted(sent_by_addin - allowed)
    assert not dropped, (
        "the SolidWorks add-in sends import options the bridge would drop: %s"
        % dropped)
    print("  option parity: %d accepted, all real, %d sent by the add-in, "
          "all accepted" % (len(allowed), len(sent_by_addin)))


main()
