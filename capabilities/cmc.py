# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""CMC capability — the phone's CMC remote (TrXi-Ctrl, CMC tab).

Relays to Chameleon Media Center's own control socket (CMCRemote in
Chameleon-Media-Center.py: JSON lines on a user-only Unix socket), so the
phone uses CMC's search, playlist and watch list exactly as the window does.
Position and pause state come from CMC's mpv socket directly. The speaker
switch is the audio capability's hardware mute, which keeps the stream to
the phone alive ("Phone only" listening).
"""

import asyncio
import itertools
import json
import os
import tempfile

from . import audio, mpv_ipc

SOCKET = os.environ.get("CMC_CONTROL_SOCKET",
                        os.path.join(tempfile.gettempdir(), "cmc-control.sock"))
MPV_SOCKET = os.environ.get("CMC_MPV_SOCKET", "/tmp/mpvsocket")
_ids = itertools.count(1)
_TRANSPORT = ("pause", "resume", "toggle", "stop", "next", "previous")
_SOURCES = ("youtube", "bilibili")


def _str(body, key, limit=2000, required=True):
    v = body.get(key, "")
    if not isinstance(v, str):
        raise ValueError(f"{key} must be text")
    v = v.strip()
    if required and not v:
        raise ValueError(f"{key} is required")
    if len(v) > limit:
        raise ValueError(f"{key} is too long")
    return v


def _url(body):
    u = _str(body, "url")
    if not u.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    return u


def _int(body, key):
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{key} must be a whole number")
    return v


async def call(cmd: str, timeout: float = 10, **args) -> tuple[int, dict]:
    """One request to CMC; (HTTP status, reply)."""
    if not os.path.exists(SOCKET):
        return 503, {"ok": False, "error": "Chameleon Media Center isn't running on the PC.",
                     "running": False}
    rid = next(_ids)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(SOCKET), 3)
    except (OSError, asyncio.TimeoutError):
        return 503, {"ok": False, "error": "Chameleon Media Center isn't answering.",
                     "running": False}
    try:
        writer.write((json.dumps({"id": rid, "cmd": cmd, **args}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        reply = json.loads(line or b"{}")
    except asyncio.TimeoutError:
        return 504, {"ok": False, "error": "Chameleon Media Center took too long to answer."}
    except ValueError:
        return 502, {"ok": False, "error": "Chameleon Media Center sent a bad answer."}
    finally:
        writer.close()
    reply.pop("id", None)
    reply.setdefault("running", True)
    return (200 if reply.get("ok") else 400), reply


# -- routes ------------------------------------------------------------------------
async def status(body, query):
    st, out = await call("status")
    if st == 200 and os.path.exists(MPV_SOCKET):
        mpv = await mpv_ipc.get_status(MPV_SOCKET) or {}
        if mpv:
            out["player"] = {"title": mpv.get("media_title"), "position": mpv.get("time_pos"),
                             "duration": mpv.get("duration"), "paused": bool(mpv.get("pause")),
                             "idle": bool(mpv.get("idle_active")), "volume": mpv.get("volume")}
    return st, out


async def search(body, query):
    q = _str(body, "query", 500)
    sources = body.get("sources") or list(_SOURCES)
    if not isinstance(sources, list) or not all(s in _SOURCES for s in sources):
        raise ValueError("sources must be a list of: " + ", ".join(_SOURCES))
    limit = body.get("limit", 15)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 40:
        raise ValueError("limit must be 1 to 40")
    return await call("search", timeout=120, query=q, sources=sources, limit=limit)


async def play(body, query):
    return await call("play", url=_url(body), title=_str(body, "title", 500, required=False))


async def playlist(body, query):
    return await call("playlist.list")


async def playlist_add(body, query):
    return await call("playlist.add", url=_url(body), title=_str(body, "title", 500, required=False))


async def playlist_remove(body, query):
    return await call("playlist.remove", index=_int(body, "index"))


async def playlist_play(body, query):
    return await call("playlist.play", index=_int(body, "index"))


async def watchlist(body, query):
    return await call("watchlist.list")


async def watchlist_add(body, query):
    r = body.get("result")
    if not isinstance(r, dict):
        raise ValueError("result must be a search result")
    clean = {"key": _str(r, "key", 200), "url": _url(r),
             "title": _str(r, "title", 500, required=False),
             "source": _str(r, "source", 50, required=False),
             "duration": r.get("duration") if isinstance(r.get("duration"), (int, float)) else None,
             "live": bool(r.get("live"))}
    return await call("watchlist.add", result=clean)


async def watchlist_remove(body, query):
    return await call("watchlist.remove", key=_str(body, "key", 200))


async def watchlist_play(body, query):
    return await call("watchlist.play", key=_str(body, "key", 200))


async def watchlist_play_all(body, query):
    return await call("watchlist.play_all")


async def transport(body, query):
    action = body.get("action")
    if action not in _TRANSPORT:
        raise ValueError("action must be one of: " + ", ".join(_TRANSPORT))
    return await call(action)


async def seek(body, query):
    s = body.get("seconds")
    if isinstance(s, bool) or not isinstance(s, (int, float)) or abs(s) > 24 * 3600:
        raise ValueError("seconds must be a number")
    return await call("seek", seconds=float(s))


async def _default_sink() -> str:
    rc, out, _ = await audio._run(["pactl", "get-default-sink"])
    return out.strip() if rc == 0 else ""


async def speakers(body, query):
    """The PC speakers: whether they're muted, and whether they can be muted
    while the phone keeps hearing (needs the sound card's own switch; on
    Bluetooth or HDMI muting would silence the stream too)."""
    d = await audio._detect()
    sink = await _default_sink()
    hw = bool(d.get("split_mute")) and not sink.startswith("bluez") and "hdmi" not in sink.lower()
    st = await audio.get_status()
    muted = bool((st.get("sink") or {}).get("muted"))
    why = None if hw else ("The PC is playing through Bluetooth or HDMI, which has no separate "
                           "speaker switch, so it can't play on the phone only.")
    return 200, {"ok": True, "muted": muted, "phone_only_possible": hw, "reason": why,
                 "output": sink}


async def set_speakers(body, query):
    on = body.get("on")
    if not isinstance(on, bool):
        raise ValueError("on must be true or false")
    if not on:
        _, info = await speakers({}, {})
        if not info["phone_only_possible"]:
            return 409, {"ok": False, "error": info["reason"]}
    res = await audio.set_mute(not on)
    return (200 if res.get("ok") else 500), {"ok": bool(res.get("ok")), "muted": not on,
                                             "error": res.get("error")}


ROUTES = {
    ("GET", "/yay/cmc/status"): status,
    ("POST", "/yay/cmc/search"): search,
    ("POST", "/yay/cmc/play"): play,
    ("GET", "/yay/cmc/playlist"): playlist,
    ("POST", "/yay/cmc/playlist/add"): playlist_add,
    ("POST", "/yay/cmc/playlist/remove"): playlist_remove,
    ("POST", "/yay/cmc/playlist/play"): playlist_play,
    ("GET", "/yay/cmc/watchlist"): watchlist,
    ("POST", "/yay/cmc/watchlist/add"): watchlist_add,
    ("POST", "/yay/cmc/watchlist/remove"): watchlist_remove,
    ("POST", "/yay/cmc/watchlist/play"): watchlist_play,
    ("POST", "/yay/cmc/watchlist/play_all"): watchlist_play_all,
    ("POST", "/yay/cmc/transport"): transport,
    ("POST", "/yay/cmc/seek"): seek,
    ("GET", "/yay/cmc/speakers"): speakers,
    ("PUT", "/yay/cmc/speakers"): set_speakers,
}
