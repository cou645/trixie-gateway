"""Bluetooth capability — BlueZ 5 via bluetoothctl + hciconfig (Linux)."""

import asyncio
import re


# ── low-level runner ──────────────────────────────────────────────────────────

async def _bt(*args, timeout: int = 8) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "--", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        return 1, "", "timeout"
    except FileNotFoundError:
        return 1, "", "bluetoothctl: not found"


async def _hci(*args, timeout: int = 5) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "hciconfig", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        return 1, "", "timeout"


# ── adapter status ────────────────────────────────────────────────────────────

async def get_status() -> dict:
    rc, out, _ = await _bt("show")
    if rc != 0:
        return {"powered": False, "error": "bluetoothctl show failed"}

    def _flag(key: str) -> bool:
        m = re.search(rf"{key}:\s+(yes|no)", out)
        return m.group(1) == "yes" if m else False

    def _val(key: str) -> str:
        m = re.search(rf"{key}:\s+(.+)", out)
        return m.group(1).strip() if m else ""

    m = re.search(r"Controller\s+([0-9A-Fa-f:]{17})", out)
    addr = m.group(1) if m else ""

    return {
        "address":     addr,
        "name":        _val("Alias") or _val("Name"),
        "powered":     _flag("Powered"),
        "discoverable":_flag("Discoverable"),
        "pairable":    _flag("Pairable"),
        "discovering": _flag("Discovering"),
    }


# ── device list ───────────────────────────────────────────────────────────────

async def list_devices(only_connected: bool = False) -> list[dict]:
    cmd = ["devices", "Connected"] if only_connected else ["devices"]
    rc, out, _ = await _bt(*cmd)
    if rc != 0:
        return []

    devices = []
    for line in out.splitlines():
        # "Device AA:BB:CC:DD:EE:FF Some Name"
        m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line.strip())
        if not m:
            continue
        addr, name = m.group(1), m.group(2).strip()
        info = await _device_info(addr)
        devices.append(info if info else {"address": addr, "name": name})
    return devices


async def _device_info(addr: str) -> dict | None:
    rc, out, _ = await _bt("info", addr)
    if rc != 0:
        return None

    def _flag(key: str) -> bool:
        m = re.search(rf"\b{key}:\s+(yes|no)", out)
        return m.group(1) == "yes" if m else False

    def _val(key: str) -> str:
        m = re.search(rf"\b{key}:\s+(.+)", out)
        return m.group(1).strip() if m else ""

    uuids = re.findall(r"UUID:\s+(.+?)\s+\(", out)
    return {
        "address":   addr,
        "name":      _val("Name") or _val("Alias"),
        "icon":      _val("Icon"),
        "paired":    _flag("Paired"),
        "bonded":    _flag("Bonded"),
        "trusted":   _flag("Trusted"),
        "blocked":   _flag("Blocked"),
        "connected": _flag("Connected"),
        "uuids":     uuids,
    }


# ── adapter control ───────────────────────────────────────────────────────────

async def set_power(on: bool) -> dict:
    val = "on" if on else "off"
    rc, out, err = await _bt("power", val)
    ok = rc == 0 and "succeeded" in out.lower()
    return {"ok": ok, "powered": on, "error": err.strip() or None}


async def set_discoverable(on: bool, timeout: int = 180) -> dict:
    val = "on" if on else "off"
    rc, out, err = await _bt("discoverable", val)
    ok = rc == 0 and "succeeded" in out.lower()
    return {"ok": ok, "discoverable": on, "error": err.strip() or None}


async def set_pairable(on: bool) -> dict:
    val = "on" if on else "off"
    rc, out, err = await _bt("pairable", val)
    ok = rc == 0 and "succeeded" in out.lower()
    return {"ok": ok, "pairable": on, "error": err.strip() or None}


# ── scan ─────────────────────────────────────────────────────────────────────
# bluetoothctl scan is interactive; we run it for a fixed window and collect output

async def scan(duration: int = 10) -> list[dict]:
    """Scan for nearby devices for `duration` seconds. Returns discovered devices."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # send "scan on", wait, then "scan off"
        proc.stdin.write(b"scan on\n")
        await proc.stdin.drain()
        await asyncio.sleep(duration)
        proc.stdin.write(b"scan off\nquit\n")
        await proc.stdin.drain()
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=duration + 5)
        text = out.decode(errors="replace")
    except (asyncio.TimeoutError, OSError):
        text = ""

    found = {}
    for line in text.splitlines():
        m = re.search(r"\[NEW\]\s+Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line)
        if m:
            found[m.group(1)] = m.group(2).strip()
        m = re.search(r"\[CHG\]\s+Device\s+([0-9A-Fa-f:]{17})\s+Name:\s+(.*)", line)
        if m:
            found[m.group(1)] = m.group(2).strip()

    return [{"address": addr, "name": name} for addr, name in found.items()]


# ── connect / disconnect / pair / remove ──────────────────────────────────────

async def connect(addr: str) -> dict:
    rc, out, err = await _bt("connect", addr, timeout=15)
    ok = rc == 0 and "successful" in out.lower()
    return {"ok": ok, "address": addr, "error": None if ok else (err.strip() or out.strip())}


async def disconnect(addr: str) -> dict:
    rc, out, err = await _bt("disconnect", addr)
    ok = rc == 0 and "successful" in out.lower()
    return {"ok": ok, "address": addr, "error": None if ok else (err.strip() or out.strip())}


async def pair(addr: str) -> dict:
    rc, out, err = await _bt("pair", addr, timeout=30)
    ok = rc == 0 and ("successful" in out.lower() or "already paired" in out.lower())
    return {"ok": ok, "address": addr, "error": None if ok else (err.strip() or out.strip())}


async def trust(addr: str, trusted: bool = True) -> dict:
    cmd = "trust" if trusted else "untrust"
    rc, out, err = await _bt(cmd, addr)
    ok = rc == 0 and "succeeded" in out.lower()
    return {"ok": ok, "address": addr, "trusted": trusted, "error": err.strip() or None}


async def remove_device(addr: str) -> dict:
    rc, out, err = await _bt("remove", addr)
    ok = rc == 0 and "removed" in out.lower()
    return {"ok": ok, "address": addr, "error": None if ok else (err.strip() or out.strip())}
