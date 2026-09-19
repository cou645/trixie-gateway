"""macOS backend: input, windows, audio, screen-capture spec, system info.

Input goes through Quartz CGEvent (pyobjc), window listing through
CGWindowListCopyWindowInfo, audio through `osascript` -- AppleScript is the
only volume interface that needs no extra package and no permission prompt.

macOS gates the two things this gateway most needs behind user-granted
permissions, and there is no way to grant them from code:

  * Accessibility  -- required to post synthetic mouse/key events. Without it
    CGEventPost silently does nothing (no error), so probe() reports the state
    explicitly rather than letting input fail invisibly.
  * Screen Recording -- required to capture the display. Without it the
    avfoundation capture yields black frames.

Both are granted in System Settings > Privacy & Security, for whichever app
runs this process (Terminal, iTerm, or a packaged build). See README.
"""

import asyncio
import logging
import os
import shutil

LOG = logging.getLogger("trixie-gateway.platforms.macos")

try:
    import Quartz
    from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
    _HAVE_QUARTZ = True
except Exception as e:  # pyobjc missing -- gateway still starts, input reports why
    LOG.error("pyobjc/Quartz unavailable (%s); desktop input and window "
              "control will not work. pip install pyobjc-framework-Quartz", e)
    Quartz = None
    _HAVE_QUARTZ = False

_NO_QUARTZ = {"ok": False, "error": "pyobjc-framework-Quartz not installed"}


# ── Mouse ────────────────────────────────────────────────────────────────────

if _HAVE_QUARTZ:
    _DOWN = {1: Quartz.kCGEventLeftMouseDown, 2: Quartz.kCGEventOtherMouseDown,
             3: Quartz.kCGEventRightMouseDown}
    _UP = {1: Quartz.kCGEventLeftMouseUp, 2: Quartz.kCGEventOtherMouseUp,
           3: Quartz.kCGEventRightMouseUp}
    _BTN = {1: Quartz.kCGMouseButtonLeft, 2: Quartz.kCGMouseButtonCenter,
            3: Quartz.kCGMouseButtonRight}
else:
    _DOWN = _UP = _BTN = {}


def _post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _mouse_event(kind: int, x: int, y: int, button: int, clicks: int = 1):
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), _BTN.get(button, 0))
    if clicks > 1:
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
    return ev


async def mouse_move(x: int, y: int) -> dict:
    if not _HAVE_QUARTZ:
        return {**_NO_QUARTZ, "x": x, "y": y}
    _post(_mouse_event(Quartz.kCGEventMouseMoved, x, y, 1))
    return {"ok": True, "x": x, "y": y}


async def mouse_click(x: int, y: int, button: int = 1) -> dict:
    if not _HAVE_QUARTZ:
        return {**_NO_QUARTZ, "x": x, "y": y, "button": button}
    _post(_mouse_event(Quartz.kCGEventMouseMoved, x, y, button))
    _post(_mouse_event(_DOWN.get(button, _DOWN[1]), x, y, button))
    _post(_mouse_event(_UP.get(button, _UP[1]), x, y, button))
    return {"ok": True, "x": x, "y": y, "button": button}


async def mouse_down(x: int, y: int, button: int = 1) -> dict:
    if not _HAVE_QUARTZ:
        return _NO_QUARTZ
    _post(_mouse_event(_DOWN.get(button, _DOWN[1]), x, y, button))
    return {"ok": True}


async def mouse_up(x: int, y: int, button: int = 1) -> dict:
    if not _HAVE_QUARTZ:
        return _NO_QUARTZ
    _post(_mouse_event(_UP.get(button, _UP[1]), x, y, button))
    return {"ok": True}


async def scroll(x: int, y: int, direction: str = "down", amount: int = 3) -> dict:
    if not _HAVE_QUARTZ:
        return _NO_QUARTZ
    await mouse_move(x, y)
    delta = 1 if direction == "up" else -1
    for _ in range(max(1, amount)):
        ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine,
                                                  1, delta)
        _post(ev)
    return {"ok": True}


# ── Keyboard ─────────────────────────────────────────────────────────────────

# xdotool-style names (what the phone sends) -> macOS virtual key codes.
_KEYCODES = {
    "return": 36, "enter": 36, "kp_enter": 76,
    "tab": 48, "space": 49, "backspace": 51, "delete": 117,
    "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "prior": 116, "page_up": 116,
    "next": 121, "page_down": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

_MODIFIER_FLAGS = {
    "ctrl": 1 << 18, "control": 1 << 18,
    "alt": 1 << 19, "option": 1 << 19,
    "shift": 1 << 17,
    "super": 1 << 20, "cmd": 1 << 20, "command": 1 << 20, "meta": 1 << 20,
}


async def key_type(text: str) -> dict:
    """Types by unicode payload rather than key code, so it doesn't depend on
    the Mac's keyboard layout."""
    if not _HAVE_QUARTZ:
        return _NO_QUARTZ
    for ch in text:
        for is_down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, is_down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            _post(ev)
    return {"ok": True}


async def key_press(key: str) -> dict:
    """Accepts 'Return', 'Escape', 'ctrl+c', 'cmd+q' ..."""
    if not _HAVE_QUARTZ:
        return _NO_QUARTZ
    parts = [p.strip().lower() for p in key.split("+") if p.strip()]
    if not parts:
        return {"ok": False, "error": "empty key"}
    mods = 0
    base = None
    for p in parts:
        if p in _MODIFIER_FLAGS:
            mods |= _MODIFIER_FLAGS[p]
        else:
            base = p
    if base is None:
        return {"ok": False, "error": f"no non-modifier key in: {key}"}

    code = _KEYCODES.get(base)
    if code is None and len(base) == 1 and not mods:
        return await key_type(base)
    if code is None:
        # Single characters with modifiers still need a key code; the unicode
        # path ignores modifier flags, which is why 'ctrl+c' can't go through
        # key_type. US-layout letters/digits map linearly enough to table here.
        code = _US_LAYOUT.get(base)
    if code is None:
        return {"ok": False, "error": f"unmapped key: {key}"}

    for is_down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, is_down)
        if mods:
            Quartz.CGEventSetFlags(ev, mods)
        _post(ev)
    return {"ok": True}


# US keyboard layout key codes, needed for modifier combos (see key_press).
_US_LAYOUT = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}


async def get_display_size() -> dict:
    if not _HAVE_QUARTZ:
        return {"ok": False, "width": 1280, "height": 1024}
    main = Quartz.CGMainDisplayID()
    w = int(Quartz.CGDisplayPixelsWide(main))
    h = int(Quartz.CGDisplayPixelsHigh(main))
    return {"ok": bool(w and h), "width": w or 1280, "height": h or 1024}


# ── Screen capture ───────────────────────────────────────────────────────────

def capture_spec(display: str, width: int, height: int, framerate: int) -> tuple[str, str, dict]:
    """(url, av-format, options) for the shared capture in webrtc_screen.

    ffmpeg's avfoundation addresses inputs by index, and the screen's index
    sits after any cameras, so it varies per Mac (commonly 1 with a built-in
    FaceTime camera, 0 on a Mac with none). List them with:
        ffmpeg -f avfoundation -list_devices true -i ""
    and set TRIXIE_AVF_SCREEN_INDEX if the default is wrong.
    """
    idx = os.environ.get("TRIXIE_AVF_SCREEN_INDEX", "1")
    opts = {
        "framerate": str(framerate),
        "capture_cursor": "1",
        "pixel_format": "uyvy422",   # what avfoundation screen capture emits
    }
    return f"{idx}:none", "avfoundation", opts


# ── Window management ────────────────────────────────────────────────────────

async def list_windows() -> list[dict]:
    if not _HAVE_QUARTZ:
        return []

    def _list():
        opts = (Quartz.kCGWindowListOptionOnScreenOnly |
                Quartz.kCGWindowListExcludeDesktopElements)
        out = []
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowLayer", 0) != 0:   # menu bar, dock, overlays
                continue
            title = w.get("kCGWindowName") or w.get("kCGWindowOwnerName") or ""
            if not title:
                continue
            b = w.get("kCGWindowBounds", {})
            out.append({
                "id": int(w.get("kCGWindowNumber", 0)),
                "id_hex": hex(int(w.get("kCGWindowNumber", 0))),
                "desktop": 0,
                "pid": int(w.get("kCGWindowOwnerPID", 0)),
                "app": w.get("kCGWindowOwnerName") or "",
                "x": int(b.get("X", 0)), "y": int(b.get("Y", 0)),
                "width": int(b.get("Width", 0)), "height": int(b.get("Height", 0)),
                "title": title,
            })
        return out

    return await asyncio.to_thread(_list)


async def get_window(wid: int) -> dict | None:
    for w in await list_windows():
        if w["id"] == wid:
            return w
    return None


async def get_active_window() -> dict | None:
    """The frontmost app's topmost window. CGWindowListCopyWindowInfo returns
    windows in front-to-back order, so the first match wins."""
    if not _HAVE_QUARTZ:
        return None
    try:
        from AppKit import NSWorkspace
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return None
        pid = int(front.processIdentifier())
    except Exception:
        return None
    for w in await list_windows():
        if w.get("pid") == pid:
            return w
    return None


async def activate_window(wid: int) -> dict:
    """Raises the owning application. macOS has no public per-window raise
    without Accessibility scripting, so this activates the app that owns it."""
    w = await get_window(wid)
    if not w:
        return {"ok": False, "id": wid, "error": "no such window"}
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(w["pid"])
    if app is None:
        return {"ok": False, "id": wid, "error": "owning app not running"}
    ok = bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
    return {"ok": ok, "id": wid}


async def close_window(wid: int) -> dict:
    """Closes via System Events, which needs Accessibility permission."""
    w = await get_window(wid)
    if not w:
        return {"ok": False, "id": wid, "error": "no such window"}
    script = (f'tell application "System Events" to tell process id {w["pid"]} '
              f'to click button 1 of window 1')
    rc, _, err = await _osascript(script)
    return {"ok": rc == 0, "id": wid, "error": err.strip() or None}


async def minimize_window(wid: int) -> dict:
    w = await get_window(wid)
    if not w:
        return {"ok": False, "id": wid, "error": "no such window"}
    rc, _, err = await _osascript(
        f'tell application "System Events" to tell process id {w["pid"]} '
        f'to set value of attribute "AXMinimized" of window 1 to true')
    return {"ok": rc == 0, "id": wid, "error": err.strip() or None}


async def maximize_window(wid: int) -> dict:
    w = await get_window(wid)
    if not w:
        return {"ok": False, "id": wid, "error": "no such window"}
    rc, _, err = await _osascript(
        f'tell application "System Events" to tell process id {w["pid"]} '
        f'to set value of attribute "AXFullScreen" of window 1 to true')
    return {"ok": rc == 0, "id": wid, "error": err.strip() or None}


# ── Audio (AppleScript) ──────────────────────────────────────────────────────

async def _osascript(script: str, timeout: int = 5) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        return 1, "", "timeout"
    except FileNotFoundError as e:
        return 1, "", str(e)


async def get_status() -> dict:
    rc, out, err = await _osascript(
        'set v to get volume settings\n'
        'return (output volume of v as text) & "," & (output muted of v as text)')
    if rc != 0:
        return {"sink": None, "source": None, "error": err.strip() or None}
    pct_s, _, muted_s = out.strip().partition(",")
    try:
        pct = int(float(pct_s))
    except ValueError:
        pct = 0
    sink = {"name": "default", "description": "System output",
            "volume_pct": pct, "muted": muted_s.strip().lower() == "true",
            "state": "running"}
    return {"sink": sink, "source": None}


async def set_volume(pct: int, target: str | None = None) -> dict:
    pct = max(0, min(100, pct))
    rc, _, err = await _osascript(f"set volume output volume {pct}")
    return {"ok": rc == 0, "target": "default", "pct": pct,
            "error": err.strip() or None}


async def set_mute(muted: bool, target: str | None = None) -> dict:
    rc, _, err = await _osascript(
        f"set volume output muted {'true' if muted else 'false'}")
    return {"ok": rc == 0, "target": "default", "muted": muted,
            "error": err.strip() or None}


async def list_sinks() -> list[dict]:
    st = await get_status()
    return [st["sink"]] if st.get("sink") else []


# ── Terminal ─────────────────────────────────────────────────────────────────

def shell_command(cmd: str) -> list[str]:
    shell = os.environ.get("SHELL") or shutil.which("zsh") or "/bin/sh"
    return [shell, "-lc", cmd]


def default_cwd() -> str:
    return os.path.expanduser("~")


# ── Probe ────────────────────────────────────────────────────────────────────

async def probe() -> dict:
    checks: dict[str, object] = {"impl": "Quartz/CGEvent + avfoundation", "ok": True}
    size = await get_display_size()
    checks["display"] = f"{size['width']}x{size['height']}" if size["ok"] else "unknown"
    checks["quartz"] = _HAVE_QUARTZ
    if not _HAVE_QUARTZ:
        checks["quartz_hint"] = "pip install pyobjc-framework-Quartz"
    if _HAVE_QUARTZ:
        # Both of these are user-granted and cannot be enabled from code; the
        # phone should be told plainly rather than seeing dead controls.
        try:
            checks["accessibility"] = bool(Quartz.AXIsProcessTrusted())
        except Exception:
            checks["accessibility"] = "unknown"
        try:
            checks["screen_recording"] = bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            checks["screen_recording"] = "unknown"
        if checks.get("accessibility") is False:
            checks["accessibility_hint"] = (
                "System Settings > Privacy & Security > Accessibility: allow the "
                "app running this gateway, or mouse/keyboard input silently does nothing")
        if checks.get("screen_recording") is False:
            checks["screen_recording_hint"] = (
                "System Settings > Privacy & Security > Screen Recording: allow the "
                "app running this gateway, or the stream is black")
    try:
        import psutil  # noqa: F401
        checks["system_info"] = True
    except Exception:
        checks["system_info"] = False
        checks["system_info_hint"] = "pip install psutil"
    return checks
