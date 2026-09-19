"""Windows backend: input, windows, audio, screen-capture spec, system info.

Input and window control go through user32/ctypes rather than a package like
pyautogui, so the only hard dependency stays the stdlib. Audio is the one
exception -- the Core Audio API is COM, not a flat DLL export, so volume
control needs pycaw (see requirements-crossplatform.txt); without it the audio
routes report unsupported instead of lying about having set the volume.

Coordinates: the phone sends coordinates in the remote display's pixel space,
which only matches what SendInput expects if this process is DPI-aware --
otherwise Windows silently scales everything on a scaled display and clicks
land in the wrong place. _set_dpi_aware() below is therefore mandatory, not a
nicety, and runs at import.
"""

import asyncio
import ctypes
import ctypes.wintypes as wt
import logging
import os
import shutil

LOG = logging.getLogger("trixie-gateway.platforms.windows")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _set_dpi_aware() -> None:
    try:  # Windows 8.1+: per-monitor DPI awareness
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:  # Windows 7 fallback
        user32.SetProcessDPIAware()
    except Exception as e:
        LOG.warning("could not set DPI awareness (%s); clicks may be offset "
                    "on scaled displays", e)


_set_dpi_aware()

SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


# ── SendInput plumbing ───────────────────────────────────────────────────────

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0004, 0x0001


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send(*inputs: _INPUT) -> bool:
    arr = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
    return sent == len(inputs)


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(type=INPUT_MOUSE,
                  u=_INPUTUNION(mi=_MOUSEINPUT(dx, dy, data, flags, 0, None)))


def _abs_coords(x: int, y: int) -> tuple[int, int]:
    """Pixel coords -> the 0..65535 virtual-desktop space SendInput wants."""
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or 1
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or 1
    return (int((x - vx) * 65535 / vw), int((y - vy) * 65535 / vh))


_BUTTONS = {
    1: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    2: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    3: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}


def _move_sync(x: int, y: int) -> bool:
    ax, ay = _abs_coords(x, y)
    return _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE |
                        MOUSEEVENTF_VIRTUALDESK, ax, ay))


# ── Public input API (mirrors capabilities/desktop_input.py) ─────────────────

async def mouse_move(x: int, y: int) -> dict:
    return {"ok": _move_sync(x, y), "x": x, "y": y}


async def mouse_click(x: int, y: int, button: int = 1) -> dict:
    down, up = _BUTTONS.get(button, _BUTTONS[1])
    ok = _move_sync(x, y) and _send(_mouse(down), _mouse(up))
    return {"ok": ok, "x": x, "y": y, "button": button}


async def mouse_down(x: int, y: int, button: int = 1) -> dict:
    down, _ = _BUTTONS.get(button, _BUTTONS[1])
    return {"ok": _move_sync(x, y) and _send(_mouse(down))}


async def mouse_up(x: int, y: int, button: int = 1) -> dict:
    _, up = _BUTTONS.get(button, _BUTTONS[1])
    return {"ok": _move_sync(x, y) and _send(_mouse(up))}


async def scroll(x: int, y: int, direction: str = "down", amount: int = 3) -> dict:
    _move_sync(x, y)
    delta = WHEEL_DELTA if direction == "up" else -WHEEL_DELTA
    ok = all(_send(_mouse(MOUSEEVENTF_WHEEL, data=delta)) for _ in range(max(1, amount)))
    return {"ok": ok}


def _unicode_key(ch: str, up: bool = False) -> _INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return _INPUT(type=INPUT_KEYBOARD,
                  u=_INPUTUNION(ki=_KEYBDINPUT(0, ord(ch), flags, 0, None)))


async def key_type(text: str) -> dict:
    # KEYEVENTF_UNICODE types the character itself rather than a scan code, so
    # it is independent of the host keyboard layout -- important because the
    # phone has no idea what layout this PC uses.
    ok = True
    for ch in text:
        ok = _send(_unicode_key(ch), _unicode_key(ch, up=True)) and ok
    return {"ok": ok}


# X11/xdotool key names (what the phone app sends) -> Windows virtual keys.
_VK = {
    "return": 0x0D, "enter": 0x0D, "kp_enter": 0x0D,
    "backspace": 0x08, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "space": 0x20, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "prior": 0x21, "page_up": 0x21,
    "next": 0x22, "page_down": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "super": 0x5B, "super_l": 0x5B, "meta": 0x5B, "win": 0x5B,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "print": 0x2C, "menu": 0x5D, "caps_lock": 0x14,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}


def _vk_for(name: str) -> int | None:
    n = name.strip().lower()
    if n in _VK:
        return _VK[n]
    if len(n) == 1:
        # VkKeyScanW maps a character to a VK on the current layout; low byte
        # is the key, high byte the modifiers (ignored here -- combos come in
        # as explicit 'ctrl+c' style instead).
        res = user32.VkKeyScanW(ctypes.c_wchar(n))
        return res & 0xFF if res != -1 else None
    return None


def _vk_input(vk: int, up: bool = False) -> _INPUT:
    return _INPUT(type=INPUT_KEYBOARD,
                  u=_INPUTUNION(ki=_KEYBDINPUT(vk, 0,
                                               KEYEVENTF_KEYUP if up else 0, 0, None)))


async def key_press(key: str) -> dict:
    """Accepts xdotool-style names, including combos: 'Return', 'ctrl+c'."""
    parts = [p for p in key.split("+") if p]
    vks = [_vk_for(p) for p in parts]
    if not vks or any(v is None for v in vks):
        return {"ok": False, "error": f"unmapped key: {key}"}
    downs = [_vk_input(v) for v in vks]
    ups = [_vk_input(v, up=True) for v in reversed(vks)]
    return {"ok": _send(*downs, *ups)}


async def get_display_size() -> dict:
    w = user32.GetSystemMetrics(SM_CXSCREEN)
    h = user32.GetSystemMetrics(SM_CYSCREEN)
    if w and h:
        return {"ok": True, "width": int(w), "height": int(h)}
    return {"ok": False, "width": 1280, "height": 1024}


# ── Screen capture ───────────────────────────────────────────────────────────

def capture_spec(display: str, width: int, height: int, framerate: int) -> tuple[str, str, dict]:
    """(url, av-format, options) for the shared capture in webrtc_screen.

    gdigrab is ffmpeg's built-in Windows desktop grabber, so this needs no
    extra package -- the same PyAV that already ships with the gateway. It
    cannot capture some hardware-accelerated fullscreen apps (games,
    protected video); those show up black, which is a gdigrab limitation.
    """
    opts = {
        "framerate": str(framerate),
        "draw_mouse": "1",
        "offset_x": "0",
        "offset_y": "0",
    }
    if width and height:
        opts["video_size"] = f"{width}x{height}"
    return "desktop", "gdigrab", opts


# ── Window management ────────────────────────────────────────────────────────

_EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

SW_RESTORE, SW_MINIMIZE, SW_MAXIMIZE = 9, 6, 3
WM_CLOSE = 0x0010


def _window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _list_sync() -> list[dict]:
    out: list[dict] = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append({
            "id": int(hwnd),
            "id_hex": hex(int(hwnd)),
            "desktop": 0,
            "x": rect.left, "y": rect.top,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
            "title": title,
        })
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return out


async def list_windows() -> list[dict]:
    return await asyncio.to_thread(_list_sync)


async def get_active_window() -> dict | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    return await get_window(int(hwnd))


async def get_window(wid: int) -> dict | None:
    for w in await list_windows():
        if w["id"] == wid:
            return w
    return None


async def activate_window(wid: int) -> dict:
    user32.ShowWindow(wid, SW_RESTORE)
    ok = bool(user32.SetForegroundWindow(wid))
    return {"ok": ok, "id": wid}


async def close_window(wid: int) -> dict:
    ok = bool(user32.PostMessageW(wid, WM_CLOSE, 0, 0))
    return {"ok": ok, "id": wid}


async def minimize_window(wid: int) -> dict:
    return {"ok": bool(user32.ShowWindow(wid, SW_MINIMIZE)), "id": wid}


async def maximize_window(wid: int) -> dict:
    return {"ok": bool(user32.ShowWindow(wid, SW_MAXIMIZE)), "id": wid}


# ── Audio (pycaw / Core Audio) ───────────────────────────────────────────────

def _endpoint():
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    devices = AudioUtilities.GetSpeakers()
    iface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def _audio_status_sync() -> dict:
    vol = _endpoint()
    pct = round(vol.GetMasterVolumeLevelScalar() * 100)
    muted = bool(vol.GetMute())
    from pycaw.pycaw import AudioUtilities
    name = AudioUtilities.GetSpeakers().FriendlyName if hasattr(
        AudioUtilities.GetSpeakers(), "FriendlyName") else "Default output"
    return {"name": name, "description": name, "volume_pct": pct,
            "muted": muted, "state": "running"}


async def get_status() -> dict:
    try:
        sink = await asyncio.to_thread(_audio_status_sync)
        return {"sink": sink, "source": None}
    except ImportError:
        return {"sink": None, "source": None,
                "error": "pycaw not installed (pip install pycaw comtypes)"}
    except Exception as e:
        return {"sink": None, "source": None, "error": str(e)}


async def set_volume(pct: int, target: str | None = None) -> dict:
    pct = max(0, min(100, pct))
    try:
        def _apply():
            _endpoint().SetMasterVolumeLevelScalar(pct / 100.0, None)
        await asyncio.to_thread(_apply)
        return {"ok": True, "target": "default", "pct": pct}
    except Exception as e:
        return {"ok": False, "target": "default", "pct": pct, "error": str(e)}


async def set_mute(muted: bool, target: str | None = None) -> dict:
    try:
        def _apply():
            _endpoint().SetMute(1 if muted else 0, None)
        await asyncio.to_thread(_apply)
        return {"ok": True, "target": "default", "muted": muted}
    except Exception as e:
        return {"ok": False, "target": "default", "muted": muted, "error": str(e)}


async def list_sinks() -> list[dict]:
    try:
        st = await get_status()
        return [st["sink"]] if st.get("sink") else []
    except Exception:
        return []


# ── Terminal ─────────────────────────────────────────────────────────────────

def shell_command(cmd: str) -> list[str]:
    """PowerShell if present (what a Windows user expects), else cmd.exe."""
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if pwsh:
        return [pwsh, "-NoProfile", "-NonInteractive", "-Command", cmd]
    return [os.environ.get("COMSPEC", "cmd.exe"), "/c", cmd]


def default_cwd() -> str:
    return os.path.expanduser("~")


# ── Probe ────────────────────────────────────────────────────────────────────

async def probe() -> dict:
    checks: dict[str, object] = {"impl": "user32/ctypes + gdigrab"}
    size = await get_display_size()
    checks["display"] = f"{size['width']}x{size['height']}" if size["ok"] else "unknown"
    try:
        import pycaw  # noqa: F401
        checks["audio"] = True
    except Exception:
        checks["audio"] = False
        checks["audio_hint"] = "pip install pycaw comtypes"
    try:
        import psutil  # noqa: F401
        checks["system_info"] = True
    except Exception:
        checks["system_info"] = False
        checks["system_info_hint"] = "pip install psutil"
    checks["ok"] = True
    return checks
