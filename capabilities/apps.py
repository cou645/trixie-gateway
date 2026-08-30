"""Apps capability — launch pre-approved desktop applications via gateway."""

import asyncio
import os

_DISPLAY = os.environ.get("DISPLAY", ":0")

# Pre-approved application registry.
# cmd: argv list; icon: Material icon name hint for client.
APPROVED_APPS: dict[str, dict] = {
    "flow8": {
        "label":    "FLOW 8",
        "desc":     "Behringer FLOW 8 MIDI Controller",
        "cmd":      ["flow-8-midi"],
        "icon":     "tune",
        "category": "audio",
    },
    "audacity": {
        "label":    "Audacity",
        "desc":     "Audio editor",
        "cmd":      ["audacity"],
        "icon":     "graphic_eq",
        "category": "audio",
    },
    "vlc": {
        "label":    "VLC",
        "desc":     "Media player",
        "cmd":      ["vlc"],
        "icon":     "play_circle",
        "category": "media",
    },
    "gimp": {
        "label":    "GIMP",
        "desc":     "Image editor",
        "cmd":      ["gimp"],
        "icon":     "brush",
        "category": "graphics",
    },
    "thunar": {
        "label":     "Files",
        "desc":      "File manager",
        "cmd":       ["thunar"],
        "fallbacks": [["rox"], ["yay", "--file-browser"]],
        "icon":      "folder_open",
        "category":  "system",
    },
    "terminal": {
        "label":    "Terminal",
        "desc":     "ROXTerm terminal emulator",
        "cmd":      ["roxterm"],
        "icon":     "terminal",
        "category": "system",
    },
}

_RUNNING: dict[str, asyncio.subprocess.Process] = {}


async def list_apps() -> list[dict]:
    result = []
    for key, info in APPROVED_APPS.items():
        proc    = _RUNNING.get(key)
        running = proc is not None and proc.returncode is None
        result.append({
            "id":       key,
            "label":    info["label"],
            "desc":     info["desc"],
            "icon":     info["icon"],
            "category": info["category"],
            "running":  running,
        })
    return result


async def launch_app(app_id: str) -> dict:
    if app_id not in APPROVED_APPS:
        return {"ok": False, "error": f"unknown app: {app_id}"}

    info = APPROVED_APPS[app_id]
    env  = {**os.environ, "DISPLAY": _DISPLAY}

    # Return existing PID if still alive
    proc = _RUNNING.get(app_id)
    if proc is not None and proc.returncode is None:
        return {"ok": True, "pid": proc.pid, "already_running": True}

    cmds = [info["cmd"]] + info.get("fallbacks", [])
    last_err = ""
    for cmd in cmds:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            _RUNNING[app_id] = proc
            return {"ok": True, "pid": proc.pid, "cmd": cmd[0]}
        except FileNotFoundError:
            last_err = f"{cmd[0]}: not found"
        except Exception as e:
            last_err = str(e)
    return {"ok": False, "error": last_err}


async def kill_app(app_id: str) -> dict:
    if app_id not in APPROVED_APPS:
        return {"ok": False, "error": f"unknown app: {app_id}"}
    proc = _RUNNING.get(app_id)
    if proc is None or proc.returncode is not None:
        _RUNNING.pop(app_id, None)
        return {"ok": False, "error": "not running"}
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        proc.kill()
    _RUNNING.pop(app_id, None)
    return {"ok": True}
