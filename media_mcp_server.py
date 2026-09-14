#!/usr/bin/env python3
"""media-mcp — media playback MCP bridge (JSON-RPC 2.0 stdio)

Controls whatever media session is currently detected by
capabilities/media.py: Chameleon-Media-Center.py / MPV-Media-Center-*.py
(via mpv's --input-ipc-server JSON socket — full control), yay --media
gstreamer sessions (pipe IPC — play/pause/stop/kill only, no position/
duration/volume reporting), and VLC (detected, not yet controllable — see
capabilities/media.py's _vlc_sessions() docstring).

This is a thin stdio wrapper around capabilities.media — the exact same
module trixie-gateway's REST API (/yay/media/*) uses, so an MCP client and
the yayos_admin phone app always see and control the same sessions the
same way; no separate control path to keep in sync.

Headless daemon — no GUI needed. Run as an MCP server.

Usage: python3 media_mcp_server.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capabilities import media  # noqa: E402


def _run(coro):
    """Each MCP tool call gets its own event loop — this server is called
    interactively (a human or an LLM issuing occasional commands), not in
    a tight loop, so the per-call asyncio.run() overhead is irrelevant and
    this stays a plain synchronous JSON-RPC loop like the rest of this
    codebase's other hand-rolled MCP servers (see openbox-mcp)."""
    return asyncio.run(coro)


# ── MCP JSON-RPC 2.0 server ──────────────────────────────────────────────────

class MCPServer:
    def __init__(self, name: str = "media-mcp"):
        self.name = name
        self.tools: dict[str, dict] = {}
        self._register()

    def _reg(self, name: str, description: str, schema: dict, handler):
        self.tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": schema,
            "handler": handler,
        }

    def _write(self, obj: dict):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _ok(self, id_, text: str):
        self._write({"jsonrpc": "2.0", "id": id_,
                     "result": {"content": [{"type": "text", "text": text}],
                                "isError": False}})

    def _err(self, id_, code: int, msg: str):
        self._write({"jsonrpc": "2.0", "id": id_,
                     "error": {"code": code, "message": msg}})

    def dispatch(self, line: str):
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return
        id_    = req.get("id")
        method = req.get("method", "")

        if method == "initialize":
            pv = req.get("params", {}).get("protocolVersion", "2024-11-05")
            self._write({"jsonrpc": "2.0", "id": id_,
                         "result": {"protocolVersion": pv,
                                    "capabilities": {"tools": {}},
                                    "serverInfo": {"name": self.name, "version": "1.0"}}})
        elif method in ("notifications/initialized", "$/cancelRequest"):
            pass
        elif method == "tools/list":
            tools = [{k: v for k, v in t.items() if k != "handler"}
                     for t in self.tools.values()]
            self._write({"jsonrpc": "2.0", "id": id_, "result": {"tools": tools}})
        elif method == "tools/call":
            params = req.get("params", {})
            name   = params.get("name", "")
            args   = params.get("arguments", {})
            if name not in self.tools:
                self._err(id_, -32601, f"Unknown tool: {name}")
                return
            try:
                result = self.tools[name]["handler"](args)
                text = result if isinstance(result, str) else json.dumps(result)
                self._ok(id_, text)
            except Exception as ex:
                self._err(id_, -32603, str(ex))
        elif id_ is not None:
            self._err(id_, -32601, "Method not found")

    def run(self):
        for raw in sys.stdin:
            line = raw.strip()
            if line:
                self.dispatch(line)

    # ── tool registrations ───────────────────────────────────────────────────

    def _register(self):
        self._reg("media_status",
            "List all detected media sessions (Chameleon-Media-Center/mpv, "
            "yay --media, VLC) with pid, title, source, and — for mpv-backed "
            "sessions — real playing/position/duration/volume.",
            {"type": "object", "properties": {}},
            lambda a: _run(media.list_sessions()))

        self._reg("media_play",
            "Resume playback. Omit pid to target the first controllable session.",
            {"type": "object", "properties": {
                "pid": {"type": "integer", "description": "Session pid (optional)"}}},
            lambda a: _run(media.play(pid=a.get("pid"))))

        self._reg("media_pause",
            "Pause playback. Omit pid to target the first controllable session.",
            {"type": "object", "properties": {
                "pid": {"type": "integer", "description": "Session pid (optional)"}}},
            lambda a: _run(media.pause(pid=a.get("pid"))))

        self._reg("media_stop",
            "Stop playback. Omit pid to target the first controllable session.",
            {"type": "object", "properties": {
                "pid": {"type": "integer", "description": "Session pid (optional)"}}},
            lambda a: _run(media.stop(pid=a.get("pid"))))

        self._reg("media_seek",
            "Seek to an absolute position in seconds. mpv-backed sessions only "
            "(Chameleon-Media-Center) — yay --media has no seek support.",
            {"type": "object", "properties": {
                "seconds": {"type": "number", "description": "Absolute position in seconds"},
                "pid": {"type": "integer", "description": "Session pid (optional)"}},
             "required": ["seconds"]},
            lambda a: _run(media.seek(a["seconds"], pid=a.get("pid"))))

        self._reg("media_set_volume",
            "Set volume, 0.0-1.0.",
            {"type": "object", "properties": {
                "volume": {"type": "number", "description": "0.0 (silent) to 1.0 (full)"},
                "pid": {"type": "integer", "description": "Session pid (optional)"}},
             "required": ["volume"]},
            lambda a: _run(media.set_volume(a["volume"], pid=a.get("pid"))))

        self._reg("media_open",
            "Open/play a file. With pid, replaces what's playing in that "
            "session; without pid, launches a new yay-media-open window.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Absolute path to a media file"},
                "pid": {"type": "integer", "description": "Session pid (optional)"}},
             "required": ["path"]},
            lambda a: _run(media.open_file(a["path"], pid=a.get("pid"))))

        self._reg("media_kill",
            "Terminate a media session. For an mpv-backed session (Chameleon-"
            "Media-Center) this closes mpv itself, not the whole app — the "
            "app's window stays open with an empty video panel, same as "
            "closing the file from within it.",
            {"type": "object", "properties": {
                "pid": {"type": "integer", "description": "Session pid"}},
             "required": ["pid"]},
            lambda a: _run(media.kill_session(a["pid"])))

        self._reg("media_recent",
            "List recently-played files.",
            {"type": "object", "properties": {}},
            lambda a: _run(media.list_recent()))

        self._reg("media_browse",
            "Browse a directory for media files and subdirectories.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Directory to list (default: home)"}}},
            lambda a: _run(media.browse(a.get("path"))))


if __name__ == "__main__":
    MCPServer().run()
