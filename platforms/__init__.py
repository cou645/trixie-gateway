# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""Per-OS backends for the capabilities that talk to the desktop.

The gateway was written against Linux/X11: xdotool for input, wmctrl for
windows, amixer for audio, ffmpeg x11grab for the screen. Everything above
that layer -- aiohttp routing, aiortc/WebRTC signalling, the provider router,
the capability broker -- is already portable.

So rather than porting the gateway, this package isolates the parts that
aren't portable. On Linux `backend` is None and every capability module runs
its original code path unchanged (no behaviour change, no new dependency).
On Windows and macOS `backend` is the matching module and the capability
modules delegate to it.

Features with no equivalent on a platform (aufs layer control has no meaning
off Linux) are not faked: FEATURES below says what this host actually
supports, /yay/capabilities serves it, and the phone app hides the rest
instead of showing buttons that silently do nothing.
"""

import logging
import os
import platform
import sys

LOG = logging.getLogger("trixie-gateway.platforms")

if sys.platform.startswith("win"):
    HOST_OS = "windows"
elif sys.platform == "darwin":
    HOST_OS = "macos"
elif sys.platform.startswith("linux"):
    HOST_OS = "linux"
else:
    HOST_OS = sys.platform


# Wayland vs X11 is a session choice, not an OS -- only meaningful when
# HOST_OS == "linux". Checks the actual socket, not just whether
# WAYLAND_DISPLAY is set: trixie-gateway.service sets both DISPLAY and
# WAYLAND_DISPLAY unconditionally (systemd inherits neither from the boot-time
# session picker's choice), so on an X11 boot WAYLAND_DISPLAY would otherwise
# be set to a socket that doesn't exist -- see trixie-gateway.service's
# comment on this. XDG_SESSION_TYPE is checked first since it's authoritative
# when present; the socket check is the fallback for when it isn't (e.g. a
# manually-launched compositor outside a login manager).
def _detect_linux_session() -> str:
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE") == "x11":
        return "x11"
    wd = os.environ.get("WAYLAND_DISPLAY")
    if wd:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        sock = wd if os.path.isabs(wd) else os.path.join(runtime_dir, wd)
        if os.path.exists(sock):
            return "wayland"
    return "x11"


LINUX_SESSION = _detect_linux_session()


def _load():
    if HOST_OS == "windows":
        from . import windows
        return windows
    if HOST_OS == "macos":
        from . import macos
        return macos
    if HOST_OS == "linux" and LINUX_SESSION == "wayland":
        from . import linux_wayland
        return linux_wayland
    return None  # Linux/X11 keeps its own in-module implementations


try:
    backend = _load()
except Exception as e:  # a missing optional dep must not stop the gateway
    LOG.error("platform backend for %s failed to load: %s", HOST_OS, e)
    backend = None


# ── Feature matrix ───────────────────────────────────────────────────────────
# Keys match the phone app's screens. "supported" here means implemented for
# this OS; whether the tools/permissions are actually present at runtime is a
# separate question answered by each backend's probe() (see /yay/capabilities).

_ALL = {
    "system":    {"linux", "windows", "macos"},
    "desktop":   {"linux", "windows", "macos"},   # screen stream + input
    "windows":   {"linux", "windows", "macos"},   # window list/activate/close
    "audio":     {"linux", "windows", "macos"},   # volume + mute
    "terminal":  {"linux", "windows", "macos"},
    "chat":      {"linux", "windows", "macos"},   # LLM providers: pure HTTP
    "journal":   {"linux", "windows", "macos"},
    # Linux-only for now -- see README "Porting status".
    "layers":     {"linux"},                      # aufs: no Windows/macOS analogue
    "firewall":   {"linux"},                      # iptables
    "bluetooth":  {"linux"},                      # bluetoothctl
    "brightness": {"linux"},                      # /sys/class/backlight
    "network":    {"linux"},                      # nmcli
    "media":      {"linux"},                      # Chameleon Media Center / mpv IPC
}


def features() -> dict[str, bool]:
    """{feature: supported-on-this-OS} for every known feature."""
    return {name: HOST_OS in oses for name, oses in _ALL.items()}


def supported(feature: str) -> bool:
    return HOST_OS in _ALL.get(feature, set())


def unsupported_result(feature: str) -> dict:
    """Uniform reply for a route the running OS can't serve."""
    return {
        "ok": False,
        "error": f"'{feature}' is not supported on {HOST_OS}",
        "unsupported": True,
        "os": HOST_OS,
    }


async def probe() -> dict:
    """What this host can actually do right now -- supported features plus
    whatever the backend can tell us about missing tools or permissions."""
    info = {
        "os": HOST_OS,
        "os_release": platform.platform(),
        "python": platform.python_version(),
        "features": features(),
    }
    if backend is not None and hasattr(backend, "probe"):
        try:
            info["backend"] = await backend.probe()
        except Exception as e:
            info["backend"] = {"ok": False, "error": str(e)}
    else:
        info["backend"] = {"ok": True, "impl": "linux-native"}
    return info
