# SPDX-License-Identifier: GPL-3.0-or-later
"""The client that talks to the CAD application (rig/cad_link.py).

Each test runs a stand-in for the CAD add-in's listener on 127.0.0.1 and a
private registry folder, so a SolidWorks that runs on this machine is
never found or touched.
"""

import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
