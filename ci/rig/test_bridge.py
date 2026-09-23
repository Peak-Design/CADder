# SPDX-License-Identifier: GPL-3.0-or-later
"""The bridge's HTTP side (bridge.py), without Blender.

The handler and the registry file need no bpy: a request is answered on
the server's own thread, and only a job goes to Blender's main thread.
"""

import json
import os
import sys
import threading
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
