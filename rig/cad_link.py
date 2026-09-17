# SPDX-License-Identifier: GPL-3.0-or-later
"""Talking back to the CAD application.

The bridge in bridge.py listens so a CAD add-in can push a model in. This
is the other direction: a client that finds a running CAD application
with a CAD Link add-in loaded (today: SW To Blender for SolidWorks) and
asks it for something: today, for a part to be tessellated again at a
finer tolerance.

Discovery mirrors the one on the add-in's side exactly: each process drops
a small JSON file naming its port and a per-session token, and a stale file
whose process is gone is deleted on sight rather than tried twice.

Requests are answered with a FILE PATH, not geometry. Both ends are on the
same machine by construction (the whole protocol is 127.0.0.1), so a
megabyte of triangles has no business being JSON-escaped through a socket.
"""

import json
import os
import urllib.error
import urllib.request

_REGISTRY = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "PeakDesign", "CADder", "solidworks")

_TIMEOUT_PING = 1.5
_TIMEOUT_JOB = 600.0     # tessellating a big assembly finely is not quick


class CadLinkError(Exception):
    """The CAD application could not be reached, or refused the request."""


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
        return "<CAD pid=%s port=%s>" % (self.pid, self.port)


def _post(inst, path, payload, timeout):
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(
        inst.url + path, data=data,
        headers={"Content-Type": "application/json",
                 "X-CADLink-Token": inst.token or ""})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover():
    """Every reachable CAD application, newest first. Never raises: an empty
    list is the normal answer when none is running."""
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
            # The application behind this entry is gone. Clearing it keeps the
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
    """The one CAD application to talk to, or an error explaining that there is
    none: the message a user actually needs at that moment."""
    instances = discover()
    if not instances:
        raise CadLinkError(
            "No CAD application with a CAD Link add-in was found. Start the "
            "CAD application (SolidWorks with SW To Blender), open the "
            "assembly, and make sure the add-in is enabled.")
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
        raise CadLinkError("The CAD application rejected the request (%s)" % exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise CadLinkError("could not reach the CAD application: %s" % exc)
    except ValueError as exc:
        raise CadLinkError("The CAD application sent something unreadable: %s" % exc)
    if not reply.get("ok"):
        raise CadLinkError(reply.get("error") or "The CAD application refused the request")
    return reply


def status(instance=None):
    return request("status", timeout=_TIMEOUT_PING, instance=instance)


def poses(component_ids=None, persistent_ids=None, instance=None):
    """Asks where those components sit now. The reply lists the component
    id, its CAD path and its world transform, in metres.

    Persistent ids are SolidWorks' own references: they still name the same
    occurrences after the assembly has been edited, which the manifest's
    c001, c002 numbering does not."""
    return request("poses", instance=instance,
                   components=list(component_ids or []),
                   persistent_ids=list(persistent_ids or []))


def retessellate(component_ids, quality, persistent_ids=None, instance=None,
                 separate_solids=None, defeature=None, paths=None):
    """Asks for those components again at `quality` (0..1). The reply names
    a .swmesh on disk. Persistent ids name the same occurrences after an
    edit; see `poses`.

    separate_solids says whether this scene holds a multibody part as one
    object per body. The geometry has to come back in the same pieces it
    went out in: the whole part arriving as one piece put the whole part on
    every body object, drawn over itself once per body (Oscar,
    2026-09-16). None leaves the CAD application to use its own export
    setting, which is what an older scene has to fall back on.

    defeature names the components this scene holds without their small
    features, one entry each. The CAD application holds no such setting of
    its own, so saying nothing gets the geometry as it is.

    paths name the PLACEMENTS wanted, where the scene can say. A component
    id is the rig body's, and every part of a rigid subassembly shares it,
    so the ids alone ask for the whole branch (native_import.cad_paths)."""
    payload = dict(components=list(component_ids), quality=float(quality),
                   persistent_ids=list(persistent_ids or []))
    if separate_solids is not None:
        payload["separate_solids"] = bool(separate_solids)
    if defeature:
        payload["defeature"] = list(defeature)
    if paths:
        payload["paths"] = list(paths)
    return request("retessellate", instance=instance, **payload)
