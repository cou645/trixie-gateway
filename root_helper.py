#!/usr/bin/env python3
"""trixie-gateway-helper: the few root-only gateway actions, so the gateway
itself can run as a normal user (ISO 27001 gap G6).

The helper runs as root (trixie-gateway-helper.service) and listens on a
Unix socket that only the gateway's account can open; the kernel's peer
credentials (SO_PEERCRED) are checked on every connection as well. It knows
a fixed table of routes -- firewall, Wi-Fi, layers -- validates every
argument, and never runs a shell or anything outside that table.

  sudo python3 root_helper.py --user alice        # serve for user "alice"

The gateway forwards those routes here when it isn't root (forward()).
"""
import argparse
import asyncio
import ipaddress
import json
import logging
import os
import pwd
import re
import socket
import struct
import sys

SOCKET = "/run/trixie-gateway-helper.sock"
LOG = logging.getLogger("trixie-gateway-helper")

# ── validation (everything reaches iptables/iwlist/wpa_cli as argv) ─────────

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,27}$")   # chain or interface
_TARGETS = {"ACCEPT", "DROP", "REJECT", "LOG", "RETURN"}
_PROTOS = {"tcp", "udp", "icmp", "icmpv6", "all"}


def _name(v, what):
    v = str(v)
    if not _NAME.match(v):
        raise ValueError(f"invalid {what}: {v!r}")
    return v


def _addr(v):
    v = str(v)
    ipaddress.ip_network(v, strict=False)          # raises ValueError
    return v


def _port(v):
    v = str(v)
    if not re.fullmatch(r"\d{1,5}(:\d{1,5})?", v) or \
            any(int(p) > 65535 for p in v.split(":")):
        raise ValueError(f"invalid port: {v!r}")
    return v


def _comment(v):
    v = str(v)
    if len(v) > 128 or not v.isprintable() or v.startswith("-"):
        raise ValueError("invalid comment")
    return v


def _opt(body, key, check):
    v = body.get(key)
    return None if v in (None, "") else check(v)


# ── the route table ──────────────────────────────────────────────────────────

async def _fw_rules(body, query):
    from capabilities import firewall
    return await firewall.list_rules()


async def _fw_presets(body, query):
    from capabilities import firewall
    return {"presets": [{"name": k, "description": v["description"]}
                        for k, v in firewall.PRESETS.items()]}


async def _fw_add(body, query):
    from capabilities import firewall
    target = str(body.get("target", ""))
    if target not in _TARGETS and not _NAME.match(target):
        raise ValueError(f"invalid target: {target!r}")
    proto = body.get("proto")
    if proto not in (None, "") and proto not in _PROTOS:
        raise ValueError(f"invalid proto: {proto!r}")
    return await firewall.add_rule(
        chain=_name(body.get("chain", ""), "chain"), target=target, proto=proto or None,
        src=_opt(body, "src", _addr), dst=_opt(body, "dst", _addr),
        dport=_opt(body, "dport", _port), sport=_opt(body, "sport", _port),
        iface_in=_opt(body, "iface_in", lambda v: _name(v, "interface")),
        iface_out=_opt(body, "iface_out", lambda v: _name(v, "interface")),
        insert=bool(body.get("insert", False)), comment=_opt(body, "comment", _comment))


async def _fw_delete(body, query):
    from capabilities import firewall
    num = int(body.get("num"))
    if not 1 <= num <= 10000:
        raise ValueError("invalid rule number")
    return await firewall.delete_rule(_name(body.get("chain", ""), "chain"), num)


async def _fw_policy(body, query):
    from capabilities import firewall
    return await firewall.set_policy(_name(body.get("chain", ""), "chain"),
                                     str(body.get("policy", "")))


async def _fw_flush(body, query):
    from capabilities import firewall
    return await firewall.flush_chain(_opt(body, "chain", lambda v: _name(v, "chain")))


async def _fw_preset(body, query):
    from capabilities import firewall
    name = str(body.get("name", ""))
    if name not in firewall.PRESETS:
        raise ValueError(f"unknown preset: {name!r}")
    return await firewall.apply_preset(name)


def _gw():
    import gateway                                # same helpers the root gateway uses
    return gateway


async def _wifi_scan(body, query):
    gw = _gw()
    iface = _name(query.get("iface") or gw.WIFI_IFACE, "interface")
    await gw._run_cmd(["ip", "link", "set", iface, "up"])
    rc, out, err = await gw._run_cmd(["iwlist", iface, "scan"], timeout=20)
    if rc != 0:
        return {"networks": [], "error": err.strip() or "iwlist failed"}
    return {"networks": gw._parse_iwlist(out)}


async def _wifi_connect(body, query):
    gw = _gw()
    ssid, password = str(body.get("ssid", "")).strip(), str(body.get("password", "")).strip()
    # wpa_cli/wpa_supplicant.conf wrap both in double quotes: no quote or
    # backslash may break out of them
    if not ssid or len(ssid.encode()) > 32 or not ssid.isprintable() or set(ssid) & {'"', "\\"}:
        raise ValueError("invalid ssid")
    if len(password) > 63 or not password.isprintable() or set(password) & {'"', "\\"}:
        raise ValueError("invalid password (quotes and backslashes are not supported)")
    iface = _name(body.get("iface") or gw.WIFI_IFACE, "interface")
    ok, msg = await gw._wpa_connect(iface, ssid, password)
    return {"ok": ok, "ssid": ssid, "output": msg}


async def _wifi_disconnect(body, query):
    gw = _gw()
    iface = _name(body.get("device") or gw.WIFI_IFACE, "interface")
    rc, out, err = await gw._run_cmd(["ip", "link", "set", iface, "down"])
    return {"ok": rc == 0, "device": iface, "output": (out + err).strip()}


async def _set_layer(body, query, name):
    from capabilities import layer_control
    gw = _gw()
    sda2 = object.__new__(gw.TrixieGateway)._resolve_sda2()
    return layer_control.set_layer(sda2, _name(name, "layer"), bool(body.get("active", False)))


ROUTES = {
    ("GET", "/yay/firewall/rules"): _fw_rules,
    ("GET", "/yay/firewall/presets"): _fw_presets,
    ("POST", "/yay/firewall/rule"): _fw_add,
    ("DELETE", "/yay/firewall/rule"): _fw_delete,
    ("PUT", "/yay/firewall/policy"): _fw_policy,
    ("POST", "/yay/firewall/flush"): _fw_flush,
    ("POST", "/yay/firewall/preset"): _fw_preset,
    ("GET", "/yay/network/wifi/scan"): _wifi_scan,
    ("POST", "/yay/network/wifi/connect"): _wifi_connect,
    ("POST", "/yay/network/wifi/disconnect"): _wifi_disconnect,
}


async def dispatch(method: str, path: str, body: dict, query: dict) -> tuple[int, dict]:
    try:
        if method == "PUT" and path.startswith("/yay/layers/"):
            name = path[len("/yay/layers/"):]
            if "/" in name or name in (".", ".."):
                raise ValueError(f"invalid layer: {name!r}")
            return 200, await _set_layer(body, query, name)
        fn = ROUTES.get((method, path))
        if fn is None:
            return 404, {"ok": False, "error": "not a helper route"}
        return 200, await fn(body, query)
    except (ValueError, TypeError, KeyError) as e:
        return 400, {"ok": False, "error": str(e)}


# ── server ───────────────────────────────────────────────────────────────────

def _peer_uid(writer) -> int:
    sock = writer.get_extra_info("socket")
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def make_handler(allowed_uid: int):
    async def handle(reader, writer):
        try:
            uid = _peer_uid(writer)
            if uid not in (allowed_uid, 0):
                LOG.warning("refused connection from uid %d", uid)
                status, out = 403, {"ok": False, "error": "not allowed"}
            else:
                req = json.loads(await asyncio.wait_for(reader.readline(), 10))
                status, out = await dispatch(str(req.get("method", "")), str(req.get("path", "")),
                                             req.get("body") or {}, req.get("query") or {})
                LOG.info("uid %d %s %s -> %d", uid, req.get("method"), req.get("path"), status)
            writer.write((json.dumps({"status": status, "body": out}) + "\n").encode())
            await writer.drain()
        except Exception as e:                      # one bad request never kills the helper
            LOG.warning("bad request: %s", e)
        finally:
            writer.close()
    return handle


async def serve(user: str, path: str = SOCKET):
    uid = pwd.getpwnam(user).pw_uid
    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(make_handler(uid), path)
    os.chown(path, uid, -1)
    os.chmod(path, 0o600)                           # only that user (and root)
    LOG.info("serving %s for user %s (uid %d)", path, user, uid)
    async with server:
        await server.serve_forever()


# ── client (used by the gateway) ─────────────────────────────────────────────

def available(path: str | None = None) -> bool:
    return os.path.exists(path or SOCKET)


async def call(method: str, path: str, body: dict | None = None, query: dict | None = None,
               sock: str | None = None) -> tuple[int, dict]:
    reader, writer = await asyncio.open_unix_connection(sock or SOCKET)
    try:
        writer.write((json.dumps({"method": method, "path": path, "body": body or {},
                                  "query": query or {}}) + "\n").encode())
        await writer.drain()
        resp = json.loads(await asyncio.wait_for(reader.readline(), 40))
        return resp["status"], resp["body"]
    finally:
        writer.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--user", required=True, help="the account the gateway runs as")
    p.add_argument("--socket", default=SOCKET)
    a = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("trixie-gateway-helper must run as root")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    asyncio.run(serve(a.user, a.socket))
