# SPDX-License-Identifier: GPL-3.0-or-later
"""The client that talks to the CAD application (rig/cad_link.py).

Each test runs a stand-in for the CAD add-in's listener on 127.0.0.1 and a
private registry folder, so a SolidWorks that runs on this machine is
never found or touched.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from CADder.rig import cad_link  # noqa: E402

TOKEN = "t0ken"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        self.server.seen.append((self.path, body, self.headers.get("X-CADLink-Token")))
        if self.path == "/ping":
            reply = {"ok": True, "pid": self.server.pid_reply}
        else:
            reply = {"ok": True, "echo": body}
        data = json.dumps(reply).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(pid_reply=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.seen = []
    server.pid_reply = os.getpid() if pid_reply is None else pid_reply
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _silent():
    """A port that takes the connection and never answers: a listener
    whose one thread is busy with a different job."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock


def _closed_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def registry(tmp_path, monkeypatch):
    folder = tmp_path / "solidworks"
    folder.mkdir()
    monkeypatch.setattr(cad_link, "_REGISTRY", str(folder))
    monkeypatch.setattr(cad_link, "_TIMEOUT_PING", 0.5)
    return folder


def _entry(folder, name, pid, port, mtime=None):
    path = folder / name
    path.write_text(json.dumps({"pid": pid, "port": port, "token": TOKEN,
                                "addin_version": "test"}), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _instance(port, pid=None):
    return cad_link.Instance(pid or os.getpid(), port, TOKEN, "unused")


# ── Transport ──────────────────────────────────────────────────────────


def test_a_system_proxy_is_never_used(monkeypatch):
    """A proxy set for the user (the environment or Internet Options) must
    not see a call to 127.0.0.1: it cannot reach this machine's listener,
    and the call carries the session token."""
    target = _serve()
    proxy = _serve()
    try:
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        url = "http://127.0.0.1:%d" % proxy.server_address[1]
        for name in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(name, url)
        # The default opener reads the proxy settings when it is made.
        urllib.request.install_opener(None)
        try:
            cad_link.request("status", instance=_instance(target.server_address[1]))
        except cad_link.CadLinkError:
            pass
        assert not proxy.seen, "the call went to the proxy: %s" % proxy.seen
        assert [p for p, _b, _t in target.seen] == ["/job"]
    finally:
        urllib.request.install_opener(None)
        target.shutdown()
        proxy.shutdown()


def test_every_request_says_how_long_the_server_may_wait():
    server = _serve()
    try:
        inst = _instance(server.server_address[1])
        for timeout in (600.0, 60.0, 20.0):
            cad_link.request("status", timeout=timeout, instance=inst)
            wait = server.seen[-1][1]["timeout_s"]
            # The server answers "busy" before the client stops waiting.
            assert 0 < wait <= timeout - 10.0, (timeout, wait)
        cad_link.request("status", timeout=1.5, instance=inst)
        assert 0 < server.seen[-1][1]["timeout_s"] < 1.5
    finally:
        server.shutdown()


# ── Discovery ──────────────────────────────────────────────────────────


def test_a_busy_live_instance_keeps_its_registry_file(registry):
    """One thread serves the add-in's listener. A ping that waits behind a
    long job times out, and the process is still there."""
    sock = _silent()
    try:
        path = _entry(registry, "%d.json" % os.getpid(), os.getpid(),
                      sock.getsockname()[1])
        assert cad_link.discover() == []
        assert path.exists(), "the registry file of a live process was deleted"
        with pytest.raises(cad_link.CadLinkError) as err:
            cad_link.first()
        assert "does not answer" in str(err.value)
        assert path.exists()
    finally:
        sock.close()


def test_the_file_of_a_dead_process_goes(registry):
    pid = _dead_pid()
    path = _entry(registry, "%d.json" % pid, pid, _closed_port())
    assert cad_link.discover() == []
    assert not path.exists()
    with pytest.raises(cad_link.CadLinkError) as err:
        cad_link.first()
    assert "No CAD application was found" in str(err.value)


def test_a_different_process_on_the_port_is_not_that_instance(registry):
    """A new session took the port of one that died. Its answer to the
    ping is not an answer for the dead one, whose token it would refuse."""
    server = _serve(pid_reply=os.getpid())
    try:
        pid = _dead_pid()
        stale = _entry(registry, "%d.json" % pid, pid, server.server_address[1])
        assert cad_link.discover() == []
        assert not stale.exists()
    finally:
        server.shutdown()


def test_newest_first(registry):
    old = _serve()
    new = _serve()
    try:
        now = time.time()
        _entry(registry, "1.json", os.getpid(), old.server_address[1], now - 60)
        _entry(registry, "2.json", os.getpid(), new.server_address[1], now)
        ports = [i.port for i in cad_link.discover()]
        assert ports == [new.server_address[1], old.server_address[1]]
        assert cad_link.first().port == new.server_address[1]
    finally:
        old.shutdown()
        new.shutdown()


def test_running():
    assert cad_link._running(os.getpid()) is True
    assert cad_link._running(_dead_pid()) is False
    assert cad_link._running(None) is None
    assert cad_link._running("not a pid") is None
