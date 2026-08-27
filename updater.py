# Update check for STEPper NEXT.
#
# Asks the GitHub releases API once a day whether a newer release exists,
# so the N-panel can say so and hand the user the zip for their platform.
# The addon is installed from a zip rather than from extensions.blender.org
# (it ships precompiled binaries), so Blender's own extension updater never
# sees it and this is the only way a user learns a new version is out.
#
# The request runs on a daemon thread and touches no bpy data. The result
# is picked up by a bpy.app timer on the main thread, which is also the
# only place the cached answer is written back to the preferences.

import json
import threading
import urllib.error
import urllib.request
from datetime import date

try:
    import bpy
except ImportError:
    bpy = None

REPO = "Peak-Design/STEPper_NEXT"
RELEASES_API = "https://api.github.com/repos/{}/releases/latest".format(REPO)
RELEASES_PAGE = "https://github.com/{}/releases/latest".format(REPO)
KOFI_URL = "https://ko-fi.com/oskarasspalvys"

_TIMEOUT = 6.0
# GitHub rejects requests with no User-Agent.
_USER_AGENT = "STEPper-NEXT-updater"

# Written by the worker thread, read by the main thread. Assignment of a
# whole dict is atomic under the GIL, so no lock is needed.
_result = None
_thread = None
_timer_running = False


def current_version():
    """This build's version as a tuple, or None if it cannot be read."""
    try:
        from . import bl_info
        return tuple(bl_info["version"])
    except Exception:
        return None


def version_string(version=None):
    version = current_version() if version is None else version
    return ".".join(str(p) for p in version) if version else "?"


def parse_version(text):
    """'v2.4.6' -> (2, 4, 6). Stops at the first non-numeric component, so
    a tag like 'v2.2.1-Test' still compares as (2, 2, 1)."""
    if not text:
        return None
    cleaned = str(text).strip().lstrip("vV")
    parts = []
    for chunk in cleaned.replace("-", ".").replace("_", ".").split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts) if parts else None


def is_newer(latest, current):
    """True when latest is strictly newer, comparing (2, 4) as (2, 4, 0)."""
    if not latest or not current:
        return False
    width = max(len(latest), len(current))
    latest += (0,) * (width - len(latest))
    current += (0,) * (width - len(current))
    return latest > current


def platform_key():
    """Release-asset suffix for the running platform."""
    import platform
    import sys
    if sys.platform.startswith("win"):
        return "windows_x64"
    if sys.platform == "darwin":
        machine = platform.machine().lower()
        return "macos_arm64" if machine in ("arm64", "aarch64") else "macos_x64"
    return "linux_x64"


def _fetch():
    """Network only. Runs on a worker thread, so it must not touch bpy."""
    request = urllib.request.Request(
        RELEASES_API,
        headers={"User-Agent": _USER_AGENT,
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    tag = data.get("tag_name") or ""
    # Hand back the zip for this platform where we can recognize one, so
    # the user does not have to pick from four downloads.
    key = platform_key()
    url = ""
    for asset in data.get("assets") or ():
        name = asset.get("name") or ""
        if key in name and name.endswith(".zip"):
            url = asset.get("browser_download_url") or ""
            break
    return {"tag": tag, "url": url or RELEASES_PAGE}


def _worker():
    global _result
    try:
        _result = _fetch()
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        # Offline, rate limited or a changed payload: not worth bothering
        # the user about. Try again tomorrow.
        _result = {"error": str(exc)}
    except Exception as exc:  # never let a thread exception escape
        _result = {"error": repr(exc)}


def _prefs():
    if bpy is None:
        return None
    try:
        return bpy.context.preferences.addons[
            __package__].preferences
    except (AttributeError, KeyError):
        return None


def _should_check(prefs):
    if prefs is None or not getattr(prefs, "check_for_updates", False):
        return False
    last = getattr(prefs, "update_last_check", "")
    if not last:
        return True
    try:
        year, month, day = (int(p) for p in last.split("-"))
        return date.today() > date(year, month, day)
    except (ValueError, TypeError):
        return True


def _apply_result(prefs, result):
    prefs.update_last_check = date.today().isoformat()
    if result.get("error"):
        return
    prefs.update_latest_tag = result.get("tag", "")
    prefs.update_latest_url = result.get("url", "")


def _tick():
    """Main-thread timer: start the check, then collect its result."""
    global _thread, _timer_running
    if bpy is None or not _timer_running:
        return None
    prefs = _prefs()
    if prefs is None:
        return None
    if _thread is None:
        if not _should_check(prefs):
            _timer_running = False
            return None
        _thread = threading.Thread(target=_worker, daemon=True)
        _thread.start()
        return 0.5
    if _thread.is_alive():
        return 0.5
    if _result is not None:
        try:
            _apply_result(prefs, _result)
        except Exception as exc:
            print("STEPper NEXT: could not store update check:", exc)
    _timer_running = False
    return None


def start(delay=6.0):
    """Schedule the daily check. Called from register(). The delay keeps it
    clear of Blender's startup so nothing competes with file loading."""
    global _timer_running
    if bpy is None or _timer_running:
        return
    _timer_running = True
    try:
        bpy.app.timers.register(_tick, first_interval=delay,
                                persistent=True)
    except Exception as exc:
        _timer_running = False
        print("STEPper NEXT: update check not scheduled:", exc)


def stop():
    global _timer_running, _thread
    _timer_running = False
    _thread = None
    if bpy is None:
        return
    try:
        if bpy.app.timers.is_registered(_tick):
            bpy.app.timers.unregister(_tick)
    except Exception:
        pass


def available_update(prefs=None):
    """The cached newer release as {'version', 'url'}, or None.

    Reads only the preference cache, so drawing a panel never blocks.
    """
    prefs = _prefs() if prefs is None else prefs
    if prefs is None or not getattr(prefs, "check_for_updates", False):
        return None
    latest = parse_version(getattr(prefs, "update_latest_tag", ""))
    if not is_newer(latest, current_version()):
        return None
    return {"version": version_string(latest),
            "url": getattr(prefs, "update_latest_url", "") or RELEASES_PAGE}
