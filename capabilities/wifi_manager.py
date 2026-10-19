# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""Wi-Fi Manager capability — AirBlit, Tailscale and USB Share for the phone.

Everything goes through `wifi-manager ctl` (Wi-Fi Manager's own command line,
JSON out), so the phone and the window share one implementation and see the
same state. Each body field is validated here and passed as its own argv
item; `ctl` validates again (argparse choices, interface names, Funnel only
ever publishes a local port).
"""

import asyncio
import json
import os
import pwd
import re
import shutil

_CANDIDATES = ("wifi-manager", "/opt/wifi-manager/bin/wifi-manager", "/usr/local/bin/wifi-manager")
_IFACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}$")
_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$")
_FUNNEL_PORTS = ("443", "8443", "10000")


def _command() -> str | None:
    for c in _CANDIDATES:
        path = shutil.which(c) if "/" not in c else (c if os.access(c, os.X_OK) else None)
        if path:
            return path
    return None


def available() -> bool:
    return _command() is not None


async def _ctl(*args: str, timeout: int = 90) -> tuple[int, dict]:
    cmd = _command()
    if not cmd:
        return 501, {"ok": False, "error": "Wi-Fi Manager isn't installed on this PC "
                     "(or its module isn't loaded)."}
    env = dict(os.environ)
    env.setdefault("HOME", pwd.getpwuid(os.getuid()).pw_dir)      # its saved AirBlit settings
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, "ctl", *args, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return 504, {"ok": False, "error": "wifi-manager ctl timed out"}
    try:
        data = json.loads(out.decode(errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        msg = (err or out).decode(errors="replace").strip().splitlines()
        return 500, {"ok": False, "error": msg[-1] if msg else "wifi-manager ctl failed"}
    if isinstance(data, dict) and "message" in data and "error" not in data and not data.get("ok"):
        data["error"] = data["message"]          # the phone app shows "error" on failure
    return 200, data


def _iface(v, what="interface") -> str:
    v = str(v)
    if not _IFACE.match(v):
        raise ValueError(f"invalid {what}: {v!r}")
    return v


def _onoff(v) -> str:
    if isinstance(v, str) and v.lower() in ("on", "off", "true", "false"):
        return "on" if v.lower() in ("on", "true") else "off"
    if isinstance(v, bool):
        return "on" if v else "off"
    raise ValueError(f"expected true/false, got {v!r}")


# ── routes (body/query dicts in, (status, dict) out) ──────────────────────────

async def status(body: dict, query: dict) -> tuple[int, dict]:
    return await _ctl("status")


async def airblit(body: dict, query: dict) -> tuple[int, dict]:
    action = body.get("action")
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    args = ["airblit", action]
    if action == "start":
        if body.get("role") is not None:
            if body["role"] not in ("send", "recv"):
                raise ValueError("role must be send or recv")
            args += ["--role", body["role"]]
        if body.get("adapter"):
            args += ["--adapter", _iface(body["adapter"], "adapter")]
        if body.get("fec") is not None:
            fec = int(body["fec"])
            if not 0 <= fec <= 100:
                raise ValueError("fec must be 0-100")
            args += ["--fec", str(fec)]
        for key in ("audio", "audio_only", "play_audio", "show_screen"):
            if body.get(key) is not None:
                args += [f"--{key.replace('_', '-')}", _onoff(body[key])]
    return await _ctl(*args)


async def tailscale(body: dict, query: dict) -> tuple[int, dict]:
    if "exit_node" in body:
        node = str(body["exit_node"] or "none")
        if node != "none" and not _NODE.match(node):
            raise ValueError(f"invalid exit node: {node!r}")
        return await _ctl("tailscale", "exit-node", node)
    if "lan_access" in body:
        return await _ctl("tailscale", "lan-access", _onoff(body["lan_access"]))
    if body.get("up"):
        return await _ctl("tailscale", "up")
    raise ValueError("expected exit_node, lan_access or up")


async def funnel(body: dict, query: dict) -> tuple[int, dict]:
    action = body.get("action")
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    port = str(body.get("port") or "443")
    if port not in _FUNNEL_PORTS:
        raise ValueError("Funnel only uses ports 443, 8443 and 10000")
    args = ["funnel", action, "--port", port]
    if action == "start":
        target = str(body.get("target", ""))
        if not re.fullmatch(r"\d{1,5}", target) or not 0 < int(target) < 65536:
            raise ValueError("target must be a local port number, e.g. 8080")
        args += ["--target", target]
    return await _ctl(*args)


async def usb(body: dict, query: dict) -> tuple[int, dict]:
    role = body.get("role")
    if role not in ("off", "in", "out"):
        raise ValueError("role must be off, in or out")
    args = ["usb", _iface(body.get("dev", ""), "device"), role]
    if role == "out":
        if body.get("address"):
            addr = str(body["address"])
            if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", addr):
                raise ValueError(f"invalid address: {addr!r}")
            args += ["--address", addr]
        if body.get("uplink"):
            args += ["--uplink", _iface(body["uplink"], "uplink")]
    if role == "in" and body.get("own_traffic") is not None:
        args += ["--own-traffic", _onoff(body["own_traffic"])]
    return await _ctl(*args, timeout=150)


ROUTES = {
    ("GET",  "/yay/network/wm/status"):    status,
    ("POST", "/yay/network/wm/airblit"):   airblit,
    ("POST", "/yay/network/wm/tailscale"): tailscale,
    ("POST", "/yay/network/wm/funnel"):    funnel,
    ("POST", "/yay/network/wm/usb"):       usb,
}
