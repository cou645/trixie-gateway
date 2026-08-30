"""System info capability — CPU, memory, thermal zones, battery via sysfs/procfs."""

import os
import platform
from pathlib import Path


async def get() -> dict:
    return {
        "hostname":  platform.node(),
        "kernel":    platform.release(),
        "cpus":      os.cpu_count(),
        "cpu_model": _cpu_model(),
        **_meminfo(),
        "uptime":    _uptime(),
        "load":      _loadavg(),
        "thermal":   _thermal_zones(),
        "battery":   _battery(),
    }


def cpu_model() -> str:
    return _cpu_model()


def meminfo() -> dict:
    return _meminfo()


def uptime() -> float:
    return _uptime()


# ── internals ─────────────────────────────────────────────────────────────────

def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def _meminfo() -> dict:
    raw: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split(":")
            if len(parts) == 2:
                raw[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024
    except Exception:
        pass
    return {
        "mem_total":     raw.get("MemTotal", 0),
        "mem_available": raw.get("MemAvailable", 0),
        "mem_used":      raw.get("MemTotal", 0) - raw.get("MemAvailable", 0),
        "swap_total":    raw.get("SwapTotal", 0),
        "swap_free":     raw.get("SwapFree", 0),
    }


def _uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return 0.0


def _loadavg() -> list[float]:
    try:
        parts = Path("/proc/loadavg").read_text().split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        return [0.0, 0.0, 0.0]


def _thermal_zones() -> list[dict]:
    zones = []
    base = Path("/sys/class/thermal")
    if not base.exists():
        return zones
    for path in sorted(base.glob("thermal_zone*")):
        try:
            zones.append({
                "name":   (path / "type").read_text().strip(),
                "temp_c": int((path / "temp").read_text().strip()) / 1000,
            })
        except Exception:
            pass
    return zones


def _battery() -> dict | None:
    base = Path("/sys/class/power_supply")
    if not base.exists():
        return None
    for supply in sorted(base.glob("BAT*")):
        try:
            return {
                "name":     supply.name,
                "capacity": int((supply / "capacity").read_text().strip()),
                "status":   (supply / "status").read_text().strip(),
            }
        except Exception:
            pass
    return None
