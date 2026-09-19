"""System info capability — CPU, memory, thermal zones, battery.

Linux reads procfs/sysfs directly (no dependency, and it is the only place
thermal zones are reliably exposed). Windows and macOS have no /proc, so they
use psutil, which ships the same numbers cross-platform.
"""

import os
import platform
from pathlib import Path

from platforms import backend as _backend


async def get() -> dict:
    if _backend:
        return _psutil_info()
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


def _psutil_info() -> dict:
    """Windows/macOS equivalent of the procfs read below.

    Thermals are left empty rather than guessed: psutil exposes
    sensors_temperatures() on Linux only, and neither Windows nor macOS offers
    temperatures without extra privileged tooling.
    """
    base = {
        "hostname":  platform.node(),
        "kernel":    platform.release(),
        "cpus":      os.cpu_count(),
        "cpu_model": platform.processor() or "unknown",
        "thermal":   [],
    }
    try:
        import psutil
    except ImportError:
        return {**base, "mem_total": 0, "mem_available": 0, "mem_used": 0,
                "swap_total": 0, "swap_free": 0, "uptime": 0.0,
                "load": [0.0, 0.0, 0.0], "battery": None,
                "error": "psutil not installed (pip install psutil)"}

    import time
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    try:
        load = list(psutil.getloadavg())
    except (AttributeError, OSError):
        load = [0.0, 0.0, 0.0]
    battery = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery = {
                "name": "battery",
                "capacity": int(b.percent),
                "status": "Charging" if b.power_plugged else "Discharging",
            }
    except Exception:
        pass
    return {
        **base,
        "mem_total": vm.total,
        "mem_available": vm.available,
        "mem_used": vm.total - vm.available,
        "swap_total": sw.total,
        "swap_free": sw.free,
        "uptime": max(0.0, time.time() - psutil.boot_time()),
        "load": load,
        "battery": battery,
    }


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
