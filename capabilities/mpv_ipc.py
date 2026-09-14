"""Thin async JSON-IPC client for mpv's --input-ipc-server unix socket.

mpv's IPC protocol: https://mpv.io/manual/master/#json-ipc
One JSON object per line in both directions:
  request:  {"command": ["get_property", "pause"], "request_id": N}
  response: {"data": ..., "request_id": N, "error": "success"|<message>}

Used by Chameleon-Media-Center.py's embedded mpv (always launched with
--input-ipc-server=<path> — confirmed live: /root/Desktop/scripts/
Chameleon-Media-Center.py spawns mpv --wid=<embedded window> --input-ipc-
server=/tmp/mpvsocket). Any other mpv instance launched with the same flag
(MPV-Media-Center-*.py, manual use) is controllable the same way.
"""

from __future__ import annotations

import asyncio
import json


async def _request(socket_path: str, command: list, timeout: float = 2.0) -> dict:
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path), timeout=timeout)
    try:
        writer.write((json.dumps({"command": command}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            raise ConnectionError("mpv IPC socket closed with no response")
        return json.loads(line.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def get_property(socket_path: str, prop: str):
    """Returns the property value, or None on any failure (socket gone,
    property doesn't apply to current state, etc — mirrors the rest of this
    gateway's graceful-degrade convention)."""
    try:
        r = await _request(socket_path, ["get_property", prop])
    except Exception:
        return None
    if r.get("error") != "success":
        return None
    return r.get("data")


async def set_property(socket_path: str, prop: str, value) -> bool:
    try:
        r = await _request(socket_path, ["set_property", prop, value])
    except Exception:
        return False
    return r.get("error") == "success"


async def run_command(socket_path: str, *args) -> bool:
    try:
        r = await _request(socket_path, list(args))
    except Exception:
        return False
    return r.get("error") == "success"


async def is_alive(socket_path: str) -> bool:
    try:
        r = await _request(socket_path, ["get_property", "pid"], timeout=1.0)
    except Exception:
        return False
    return r.get("error") == "success"


# Properties pulled for a session status snapshot. mpv reports "pause":true
# when stopped/paused alike — there's no separate "stopped" state while a
# file is loaded, only pause on/off plus "idle-active" when nothing is
# loaded at all.
_STATUS_PROPS = ["pause", "idle-active", "path", "media-title", "filename",
                 "duration", "time-pos", "volume", "mute"]


async def get_status(socket_path: str) -> dict | None:
    """Fetch a batch of common playback properties in one go. Returns None
    if the socket isn't reachable at all (process gone / not mpv-ipc)."""
    if not await is_alive(socket_path):
        return None
    result = {}
    for prop in _STATUS_PROPS:
        result[prop.replace("-", "_")] = await get_property(socket_path, prop)
    return result
