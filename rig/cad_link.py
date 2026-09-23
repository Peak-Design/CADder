# SPDX-License-Identifier: GPL-3.0-or-later
"""Talking back to the CAD application.

The bridge in bridge.py listens so a CAD add-in can push a model in. This
is the other direction: a client that finds a running CAD application
with a CAD Link add-in loaded (today: SW To Blender for SolidWorks) and
asks it for something: today, for a part to be tessellated again at a
finer tolerance.

Discovery mirrors the one on the add-in's side exactly: each process drops
a small JSON file naming its port and a per-session token, and a stale file
whose process is gone is deleted on sight rather than tried twice. A file
whose process still runs stays, also when its ping gets no answer: the
add-in serves one request at a time, so a ping that waits behind a long
job times out, and neither side writes its file again.

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
# How much sooner than the client the CAD application gives up waiting for
# its own thread. It then answers that it is busy (a dialog, most often),
# and the user reads that instead of a bare "timed out".
_SERVER_MARGIN_S = 30.0

# No proxy, ever. urllib sends 127.0.0.1 through a proxy set in the
# environment or in Internet Options: it bypasses only host names without a
# dot. The proxy cannot reach this machine's listener, and every call
# carries the session token. The add-in's own client makes the same choice.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _running(pid):
    """True when the process `pid` runs, False when it does not, None when
    `pid` is not a process id."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        query_limited_information = 0x1000
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            # Access denied: the process is there, it belongs to someone
            # else.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259            # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _entries():
    """The registry entries, newest first."""
    out = []
    if not os.path.isdir(_REGISTRY):
        return out
    for name in os.listdir(_REGISTRY):
        if not name.endswith(".json"):
            continue
        path = os.path.join(_REGISTRY, name)
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            pid = doc.get("pid")
            if pid is None:
                # The add-in names its file after its process id.
                pid = os.path.splitext(name)[0]
            inst = Instance(pid, int(doc.get("port", 0)),
                            doc.get("token"), path, doc.get("addin_version"))
            written = os.path.getmtime(path)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if not inst.port or not inst.token:
            continue
        out.append((written, inst))
    out.sort(key=lambda row: row[0], reverse=True)
    return [inst for _written, inst in out]


def _forget(inst):
    try:
        os.unlink(inst.registry_file)
    except OSError:
        pass


def _scan():
    """(the instances that answered, the instances that did not answer but
    whose process still runs), each newest first.

    Only the file of a process that has gone is deleted. A process that
    still runs and does not answer is busy: the add-in serves one request
    at a time, so a ping that waits behind a long job times out. Its file
    stays, because nothing writes it again until the application
    restarts."""
    found, silent = [], []
    for inst in _entries():
        try:
            reply = _post(inst, "/ping", None, _TIMEOUT_PING)
        except (urllib.error.URLError, OSError, ValueError):
            if _running(inst.pid) is False:
                _forget(inst)
            else:
                silent.append(inst)
            continue
        answered = reply.get("pid")
        if answered is not None and str(answered) != str(inst.pid):
            # A different process listens on this port now: a session that
            # started after this entry's own one ended. It would refuse the
            # token of this entry, and its own entry answers for it.
            if _running(inst.pid) is False:
                _forget(inst)
            continue
        if reply.get("ok"):
            found.append(inst)
    return found, silent


def discover():
    """Every reachable CAD application, newest first. Never raises: an empty
    list is the normal answer when none is running."""
    return _scan()[0]


def first():
    """The one CAD application to talk to, or an error explaining that there is
    none: the message a user actually needs at that moment."""
    instances, silent = _scan()
    if instances:
        return instances[0]
    if silent:
        raise CadLinkError(
            "The CAD application does not answer. It can be busy with a "
            "different job, or it can show a dialog. Try again when it is "
            "available.")
    raise CadLinkError(
        "No CAD application was found. Start SolidWorks with the CADder "
        "Bridge add-in enabled, and open the assembly.")


def _server_wait(timeout):
    """How long the CAD application may wait for its own thread, for a
    client that waits `timeout` seconds. It must answer first: a client that
    gives up first shows "timed out" and not the reason."""
    return max(timeout - _SERVER_MARGIN_S, timeout / 2.0)


def request(op, timeout=_TIMEOUT_JOB, instance=None, **fields):
    """Runs one op and returns its reply, raising on anything that is not a
    plain success."""
    inst = instance or first()
    payload = dict(fields)
    payload["op"] = op
    payload.setdefault("timeout_s", _server_wait(timeout))
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
    """Asks for those components again at `quality`. The reply names a
    .swmesh on disk. Persistent ids name the same occurrences after an edit;
    see `poses`.

    quality is what quality.cad_request gives: the 0..1 dial that Bridge
    1.0.0 reads, and the distance, angle and relative setting a newer
    bridge cuts to. A bare number is taken as the dial alone.

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
    fields = dict(quality) if isinstance(quality, dict) else {"quality": float(quality)}
    payload = dict(components=list(component_ids),
                   persistent_ids=list(persistent_ids or []), **fields)
    if separate_solids is not None:
        payload["separate_solids"] = bool(separate_solids)
    if defeature:
        payload["defeature"] = list(defeature)
    if paths:
        payload["paths"] = list(paths)
    return request("retessellate", instance=instance, **payload)
