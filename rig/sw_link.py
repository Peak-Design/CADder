# SPDX-License-Identifier: GPL-3.0-or-later
"""Talking back to SolidWorks.

The bridge in bridge.py listens so SolidWorks can push a model in. This is
the other direction: a client that finds a running SolidWorks with the
SW To Blender add-in loaded and asks it for something — today, for a part
to be tessellated again at a finer tolerance.

Discovery mirrors the one on the add-in's side exactly: each process drops
a small JSON file naming its port and a per-session token, and a stale file
whose process is gone is deleted on sight rather than tried twice.

Requests are answered with a FILE PATH, not geometry. Both ends are on the
same machine by construction — the whole protocol is 127.0.0.1 — so a
megabyte of triangles has no business being JSON-escaped through a socket.
"""

import json
import os
import urllib.error
import urllib.request

_REGISTRY = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "PeakDesign", "SwToBlender", "solidworks")

_TIMEOUT_PING = 1.5
_TIMEOUT_JOB = 600.0     # tessellating a big assembly finely is not quick


class SwLinkError(Exception):
    """SolidWorks could not be reached, or refused the request."""


class Instance:
    def __init__(self, pid, port, token, registry_file, version=None):
        self.pid = pid
        self.port = port
        self.token = token
        self.registry_file = registry_file
        self.version = version

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def __repr__(self):
        return "<SolidWorks pid=%s port=%s>" % (self.pid, self.port)


def _post(inst, path, payload, timeout):
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(
        inst.url + path, data=data,
        headers={"Content-Type": "application/json",
                 "X-SWTB-Token": inst.token or ""})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover():
    """Every reachable SolidWorks, newest first. Never raises: an empty list
    is the normal answer when SolidWorks simply is not running."""
    found = []
    if not os.path.isdir(_REGISTRY):
        return found
    for name in sorted(os.listdir(_REGISTRY)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(_REGISTRY, name)
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            inst = Instance(doc.get("pid"), int(doc.get("port", 0)),
                            doc.get("token"), path, doc.get("addin_version"))
        except (OSError, ValueError, TypeError):
            continue
        if not inst.port or not inst.token:
            continue
        try:
            reply = _post(inst, "/ping", None, _TIMEOUT_PING)
        except (urllib.error.URLError, OSError, ValueError):
            # The SolidWorks behind this entry is gone; clearing it keeps the
            # next discovery from paying the timeout again.
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        if reply.get("ok"):
            found.append(inst)
    return found


def first():
    """The one SolidWorks to talk to, or an error explaining that there is
    none — the message a user actually needs at that moment."""
    instances = discover()
    if not instances:
        raise SwLinkError(
            "No running SolidWorks with the SW To Blender add-in was found. "
            "Start SolidWorks, open the assembly, and make sure the add-in "
            "is enabled.")
    return instances[0]


def request(op, timeout=_TIMEOUT_JOB, instance=None, **fields):
    """Runs one op and returns its reply, raising on anything that is not a
    plain success."""
    inst = instance or first()
    payload = dict(fields)
    payload["op"] = op
    try:
        reply = _post(inst, "/job", payload, timeout)
    except urllib.error.HTTPError as exc:
        raise SwLinkError("SolidWorks rejected the request (%s)" % exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise SwLinkError("could not reach SolidWorks: %s" % exc)
    except ValueError as exc:
        raise SwLinkError("SolidWorks sent something unreadable: %s" % exc)
    if not reply.get("ok"):
        raise SwLinkError(reply.get("error") or "SolidWorks refused the request")
    return reply


def status(instance=None):
    return request("status", timeout=_TIMEOUT_PING, instance=instance)


def retessellate(component_ids, quality, instance=None):
    """Asks for those components again at `quality` (0..1). The reply names
    a .swmesh on disk."""
    return request("retessellate", instance=instance,
                   components=list(component_ids), quality=float(quality))
