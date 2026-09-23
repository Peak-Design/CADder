# SPDX-License-Identifier: GPL-3.0-or-later
"""The bridge's HTTP side (bridge.py), without Blender.

The handler and the registry file need no bpy: a request is answered on
the server's own thread, and only a job goes to Blender's main thread.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder import bridge  # noqa: E402

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setitem(bridge._state, "token", "s3cret")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), bridge._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def _ping(base, token):
    req = urllib.request.Request(base + "/cadlink/ping",
                                 headers={"X-CADLink-Token": token})
    return _NO_PROXY.open(req, timeout=10)


def test_the_reply_says_json_in_utf8(server):
    with _ping(server, "s3cret") as resp:
        assert resp.headers.get_content_type() == "application/json"
        assert resp.headers.get_content_charset() == "utf-8"
        assert json.loads(resp.read().decode("utf-8"))["ok"]


@pytest.mark.parametrize("token", ["", "s3cre", "s3cret2", "s3crét"])
def test_a_wrong_token_is_refused(server, token):
    with pytest.raises(urllib.error.HTTPError) as err:
        _ping(server, token)
    assert err.value.code == 403


def test_no_token_is_refused_before_the_bridge_starts(server, monkeypatch):
    monkeypatch.setitem(bridge._state, "token", None)
    with pytest.raises(urllib.error.HTTPError) as err:
        _ping(server, "")
    assert err.value.code == 403


def test_off_windows_the_registry_is_not_in_the_shared_temp_folder(
        tmp_path, monkeypatch):
    """Without LOCALAPPDATA the registry went to the temp folder, which
    is /tmp on Linux: every local user could read the token there."""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert bridge.registry_dir().startswith(str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    folder = bridge.registry_dir()
    assert not folder.startswith(tempfile.gettempdir())
    assert folder.startswith(os.path.expanduser("~"))


def test_the_registry_file_is_written(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setitem(bridge._state, "token", "s3cret")
    monkeypatch.setitem(bridge._state, "registry_path", None)
    bridge._write_registry()
    path = bridge._state["registry_path"]
    try:
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)["token"] == "s3cret"
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    finally:
        bridge._remove_registry()


def test_a_deleted_registry_file_comes_back(tmp_path, monkeypatch):
    """The add-in deletes the file of a Blender whose ping gets no answer,
    and a Blender inside a long job does not answer. Nothing wrote the file
    again, so no send found this Blender until it restarted."""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setitem(bridge._state, "token", "s3cret")
    monkeypatch.setitem(bridge._state, "registry_path", None)
    bridge._write_registry()
    path = bridge._state["registry_path"]
    try:
        os.remove(path)
        bridge._keep_registry(force=True)
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)["token"] == "s3cret"
    finally:
        bridge._remove_registry()
    # After stop() there is no file to keep.
    bridge._keep_registry(force=True)
    assert not os.path.exists(path)
