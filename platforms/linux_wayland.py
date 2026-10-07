# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""Linux/Wayland backend: input via evdev/uinput, window listing via
ext-foreign-toplevel-list-v1, screen capture via ext-image-copy-capture-v1 +
ext-image-capture-source-v1.

Mirrors platforms/windows.py and platforms/macos.py's contract so
capabilities/desktop_input.py, capabilities/wm.py, and
capabilities/webrtc_screen.py delegate to this module unchanged, the same
way they already delegate to those two.

Why evdev instead of a Wayland input protocol: wlroots has no
virtual-pointer/virtual-keyboard protocol built into this box's compositor
(mango 0.15.1 -- checked its actual protocol list, not assumed), and
`ydotool` isn't packaged here. evdev's UInput talks straight to the kernel
via /dev/uinput, which every compositor (X11 or Wayland, any of them)
reads input from -- so this is actually MORE portable than a Wayland
protocol would have been, at the cost of needing uinput device access
(same requirement trixie-overlay already has for its stylus support).

Absolute mouse positioning: UInput normally synthesizes *relative* motion.
The device declared below advertises EV_ABS (like a touchscreen/tablet)
instead, which libinput maps 1:1 onto screen space -- the same trick
VNC/RDP-style remote-control tools use.
"""

import asyncio
import logging
import time

LOG = logging.getLogger("trixie-gateway.platforms.linux_wayland")

try:
    from evdev import UInput, AbsInfo, ecodes as e
    _HAVE_EVDEV = True
except Exception as exc:  # missing package or no /dev/uinput access
    LOG.error("evdev unavailable (%s); desktop input will not work. "
              "apt install python3-evdev, and ensure this process can "
              "read/write /dev/uinput.", exc)
    UInput = AbsInfo = e = None
    _HAVE_EVDEV = False

_NO_EVDEV = {"ok": False, "error": "python3-evdev not available / no /dev/uinput access"}

# Display size the virtual pointer's EV_ABS range is built against. Filled
# in by _device() on first use (see get_display_size below) -- the range
# has to be fixed at device-creation time, so a size query happens once,
# lazily, rather than on every gateway start regardless of whether input
# is ever used.
_display_size: tuple[int, int] | None = None
_uinput: "UInput | None" = None


def _probe_display_size() -> tuple[int, int]:
    """Real screen size, queried once via the Wayland client (see
    linux_wayland_client.py) rather than guessed -- an absolute-positioning
    device with the wrong range either can't reach part of the screen or
    maps clicks to the wrong point on it."""
    try:
        from platforms.linux_wayland_client import get_client
        w, h = get_client().output_size()
        if w and h:
            return w, h
    except Exception as exc:
        LOG.warning("could not query real display size (%s); falling back "
                    "to 1920x1080 for the virtual pointer's coordinate "
                    "range", exc)
    return 1920, 1080


def _device() -> "UInput | None":
    global _uinput, _display_size
    if not _HAVE_EVDEV:
        return None
    if _uinput is not None:
        return _uinput
    _display_size = _probe_display_size()
    w, h = _display_size
    caps = {
        e.EV_KEY: [e.BTN_LEFT, e.BTN_MIDDLE, e.BTN_RIGHT] + list(_KEYCODES.values())
                  + list(_MODIFIERS.values()) + list(_US_LAYOUT.values()),
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(value=0, min=0, max=max(w - 1, 1), fuzz=0, flat=0, resolution=0)),
            (e.ABS_Y, AbsInfo(value=0, min=0, max=max(h - 1, 1), fuzz=0, flat=0, resolution=0)),
        ],
        e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL],
    }
    # Without INPUT_PROP_POINTER, libinput has no unambiguous signal that an
    # EV_ABS device is mouse-like (moves the system cursor) rather than a
    # direct-touch device (touchscreen/tablet semantics -- no persistent
    # cursor rendered at all, since touch doesn't have hover). Missing this
    # was a real bug: key_type/key_press worked (pure EV_KEY, unaffected),
    # but mouse_move's cursor genuinely never appeared on screen even though
    # the ABS events were being delivered correctly.
    _uinput = UInput(caps, name="trixie-gateway-virtual-input",
                      input_props=[e.INPUT_PROP_POINTER])
    # libinput/the compositor attach a freshly-created uinput device via a
    # udev/seat round-trip that isn't instant -- events written immediately
    # after open are silently dropped. One-time cost since _uinput is reused
    # for the life of the process. Confirmed necessary and sufficient by
    # live-testing against mango: 0s delay dropped every event, 1.5s did not.
    time.sleep(1.5)
    return _uinput


# ── Mouse ────────────────────────────────────────────────────────────────────

_BUTTONS = {}  # populated below once `e` exists


def _init_button_map():
    global _BUTTONS
    _BUTTONS = {1: e.BTN_LEFT, 2: e.BTN_MIDDLE, 3: e.BTN_RIGHT}


if _HAVE_EVDEV:
    _init_button_map()


async def mouse_move(x: int, y: int) -> dict:
    if not _HAVE_EVDEV:
        return {**_NO_EVDEV, "x": x, "y": y}

    def _do():
        dev = _device()
        dev.write(e.EV_ABS, e.ABS_X, x)
        dev.write(e.EV_ABS, e.ABS_Y, y)
        dev.syn()
    await asyncio.to_thread(_do)
    return {"ok": True, "x": x, "y": y}


async def mouse_click(x: int, y: int, button: int = 1) -> dict:
    if not _HAVE_EVDEV:
        return {**_NO_EVDEV, "x": x, "y": y, "button": button}

    def _do():
        dev = _device()
        dev.write(e.EV_ABS, e.ABS_X, x)
        dev.write(e.EV_ABS, e.ABS_Y, y)
        dev.syn()
        btn = _BUTTONS.get(button, e.BTN_LEFT)
        dev.write(e.EV_KEY, btn, 1)
        dev.syn()
        dev.write(e.EV_KEY, btn, 0)
        dev.syn()
    await asyncio.to_thread(_do)
    return {"ok": True, "x": x, "y": y, "button": button}


async def mouse_down(x: int, y: int, button: int = 1) -> dict:
    if not _HAVE_EVDEV:
        return _NO_EVDEV

    def _do():
        dev = _device()
        dev.write(e.EV_ABS, e.ABS_X, x)
        dev.write(e.EV_ABS, e.ABS_Y, y)
        dev.write(e.EV_KEY, _BUTTONS.get(button, e.BTN_LEFT), 1)
        dev.syn()
    await asyncio.to_thread(_do)
    return {"ok": True}


async def mouse_up(x: int, y: int, button: int = 1) -> dict:
    if not _HAVE_EVDEV:
        return _NO_EVDEV

    def _do():
        dev = _device()
        dev.write(e.EV_ABS, e.ABS_X, x)
        dev.write(e.EV_ABS, e.ABS_Y, y)
        dev.write(e.EV_KEY, _BUTTONS.get(button, e.BTN_LEFT), 0)
        dev.syn()
    await asyncio.to_thread(_do)
    return {"ok": True}


async def scroll(x: int, y: int, direction: str = "down", amount: int = 3) -> dict:
    if not _HAVE_EVDEV:
        return _NO_EVDEV

    def _do():
        dev = _device()
        dev.write(e.EV_ABS, e.ABS_X, x)
        dev.write(e.EV_ABS, e.ABS_Y, y)
        dev.syn()
        delta = 1 if direction == "up" else -1
        for _ in range(max(1, amount)):
            dev.write(e.EV_REL, e.REL_WHEEL, delta)
            dev.syn()
    await asyncio.to_thread(_do)
    return {"ok": True}


# ── Keyboard ─────────────────────────────────────────────────────────────────

# xdotool-style names (what the phone sends, see capabilities/desktop_input.py)
# -> evdev key codes. Populated lazily since e.KEY_* need _HAVE_EVDEV first.
_KEYCODES = {}
_MODIFIERS = {}
_US_LAYOUT = {}


def _init_key_maps():
    global _KEYCODES, _MODIFIERS, _US_LAYOUT
    _KEYCODES = {
        "return": e.KEY_ENTER, "enter": e.KEY_ENTER, "kp_enter": e.KEY_KPENTER,
        "tab": e.KEY_TAB, "space": e.KEY_SPACE, "backspace": e.KEY_BACKSPACE,
        "delete": e.KEY_DELETE, "escape": e.KEY_ESC, "esc": e.KEY_ESC,
        "left": e.KEY_LEFT, "right": e.KEY_RIGHT, "down": e.KEY_DOWN, "up": e.KEY_UP,
        "home": e.KEY_HOME, "end": e.KEY_END,
        "prior": e.KEY_PAGEUP, "page_up": e.KEY_PAGEUP,
        "next": e.KEY_PAGEDOWN, "page_down": e.KEY_PAGEDOWN,
        "f1": e.KEY_F1, "f2": e.KEY_F2, "f3": e.KEY_F3, "f4": e.KEY_F4,
        "f5": e.KEY_F5, "f6": e.KEY_F6, "f7": e.KEY_F7, "f8": e.KEY_F8,
        "f9": e.KEY_F9, "f10": e.KEY_F10, "f11": e.KEY_F11, "f12": e.KEY_F12,
    }
    _MODIFIERS = {
        "ctrl": e.KEY_LEFTCTRL, "control": e.KEY_LEFTCTRL,
        "alt": e.KEY_LEFTALT,
        "shift": e.KEY_LEFTSHIFT,
        "super": e.KEY_LEFTMETA, "meta": e.KEY_LEFTMETA, "cmd": e.KEY_LEFTMETA,
    }
    # US-layout only -- evdev synthesizes physical keycodes, not unicode
    # codepoints, so (unlike macOS's CGEventKeyboardSetUnicodeString path
    # in platforms/macos.py) there's no layout-independent way to type
    # arbitrary text. Covers the common case (ASCII); non-ASCII text
    # returns ok:false rather than silently mistyping.
    _lower = "abcdefghijklmnopqrstuvwxyz"
    _lower_codes = [e.KEY_A, e.KEY_B, e.KEY_C, e.KEY_D, e.KEY_E, e.KEY_F, e.KEY_G,
                    e.KEY_H, e.KEY_I, e.KEY_J, e.KEY_K, e.KEY_L, e.KEY_M, e.KEY_N,
                    e.KEY_O, e.KEY_P, e.KEY_Q, e.KEY_R, e.KEY_S, e.KEY_T, e.KEY_U,
                    e.KEY_V, e.KEY_W, e.KEY_X, e.KEY_Y, e.KEY_Z]
    _US_LAYOUT.update(dict(zip(_lower, _lower_codes)))
    _digits = "1234567890"
    _digit_codes = [e.KEY_1, e.KEY_2, e.KEY_3, e.KEY_4, e.KEY_5,
                     e.KEY_6, e.KEY_7, e.KEY_8, e.KEY_9, e.KEY_0]
    _US_LAYOUT.update(dict(zip(_digits, _digit_codes)))
    _US_LAYOUT.update({
        " ": e.KEY_SPACE, "\n": e.KEY_ENTER, "\t": e.KEY_TAB,
        "-": e.KEY_MINUS, "=": e.KEY_EQUAL, "[": e.KEY_LEFTBRACE,
        "]": e.KEY_RIGHTBRACE, "\\": e.KEY_BACKSLASH, ";": e.KEY_SEMICOLON,
        "'": e.KEY_APOSTROPHE, "`": e.KEY_GRAVE, ",": e.KEY_COMMA,
        ".": e.KEY_DOT, "/": e.KEY_SLASH,
    })


if _HAVE_EVDEV:
    _init_key_maps()

# Shifted symbol -> (base key) for the US layout, used by key_type/key_press
# so e.g. "A" or "!" presses shift+key rather than failing to map.
_SHIFT_MAP = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}


def _char_keycode(ch: str) -> tuple[int | None, bool]:
    """(keycode, needs_shift) for a single character, or (None, False)."""
    if ch.isalpha() and ch.isupper():
        return _US_LAYOUT.get(ch.lower()), True
    if ch in _SHIFT_MAP:
        return _US_LAYOUT.get(_SHIFT_MAP[ch]), True
    return _US_LAYOUT.get(ch), False


async def key_type(text: str) -> dict:
    if not _HAVE_EVDEV:
        return _NO_EVDEV

    def _do():
        dev = _device()
        unmapped = []
        for ch in text:
            code, shift = _char_keycode(ch)
            if code is None:
                unmapped.append(ch)
                continue
            if shift:
                dev.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
            dev.write(e.EV_KEY, code, 1)
            dev.syn()
            dev.write(e.EV_KEY, code, 0)
            if shift:
                dev.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
            dev.syn()
        return unmapped
    unmapped = await asyncio.to_thread(_do)
    if unmapped:
        return {"ok": False,
                "error": f"no US-layout mapping for: {''.join(unmapped)!r}"}
    return {"ok": True}


async def key_press(key: str) -> dict:
    """Accepts 'Return', 'Escape', 'ctrl+c', 'super', ... (same names the
    X11 path takes via xdotool -- see capabilities/desktop_input.py)."""
    if not _HAVE_EVDEV:
        return _NO_EVDEV

    parts = [p.strip().lower() for p in key.split("+") if p.strip()]
    if not parts:
        return {"ok": False, "error": "empty key"}
    mod_codes = []
    base = None
    for p in parts:
        if p in _MODIFIERS:
            mod_codes.append(_MODIFIERS[p])
        else:
            base = p
    if base is None:
        # A bare modifier, e.g. key_press("super") to open a launcher.
        if mod_codes:
            def _do_mod():
                dev = _device()
                for c in mod_codes:
                    dev.write(e.EV_KEY, c, 1)
                dev.syn()
                for c in reversed(mod_codes):
                    dev.write(e.EV_KEY, c, 0)
                dev.syn()
            await asyncio.to_thread(_do_mod)
            return {"ok": True}
        return {"ok": False, "error": f"no non-modifier key in: {key}"}

    code = _KEYCODES.get(base)
    if code is None and len(base) == 1 and not mod_codes:
        return await key_type(base)
    if code is None:
        code, needs_shift = _char_keycode(base)
        if needs_shift:
            mod_codes.append(e.KEY_LEFTSHIFT)
    if code is None:
        return {"ok": False, "error": f"unmapped key: {key}"}

    def _do():
        dev = _device()
        for c in mod_codes:
            dev.write(e.EV_KEY, c, 1)
        dev.write(e.EV_KEY, code, 1)
        dev.syn()
        dev.write(e.EV_KEY, code, 0)
        for c in reversed(mod_codes):
            dev.write(e.EV_KEY, c, 0)
        dev.syn()
    await asyncio.to_thread(_do)
    return {"ok": True}


async def get_display_size() -> dict:
    w, h = _probe_display_size()
    return {"ok": bool(w and h), "width": w or 1280, "height": h or 1024}


# ── Window management ────────────────────────────────────────────────────────
#
# Deliberately built against wlr-foreign-toplevel-management-unstable-v1, not
# ext-foreign-toplevel-list-v1 -- the ext- protocol only lists toplevels (its
# handle interface has no activate/close/minimize/maximize requests at all,
# confirmed by reading its generated bindings). The wlr- one is what mango
# actually built control support against (see its protocols/meson.build) and
# provides listing + control in a single manager. No geometry or per-window
# desktop id exists in this protocol either way -- capabilities/wm.py already
# reports move_resize_window/set_desktop etc. as unsupported unconditionally
# for any backend, so that gap needs no handling here.

def _get_client():
    from platforms.linux_wayland_client import get_client
    return get_client()


def _entry_to_window(e: dict) -> dict:
    return {
        "id": e["id"],
        "id_hex": hex(e["id"]),
        "desktop": 0,
        "pid": 0,
        "app": e.get("app_id", ""),
        "x": 0, "y": 0, "width": 0, "height": 0,
        "title": e.get("title", ""),
    }


async def list_windows() -> list[dict]:
    try:
        entries = await asyncio.to_thread(_get_client().list_toplevels)
        return [_entry_to_window(e) for e in entries]
    except Exception as exc:
        LOG.warning("list_windows failed: %s", exc)
        return []


async def get_window(wid: int) -> dict | None:
    try:
        e = await asyncio.to_thread(_get_client().get_toplevel, wid)
        return _entry_to_window(e) if e else None
    except Exception as exc:
        LOG.warning("get_window failed: %s", exc)
        return None


async def get_active_window() -> dict | None:
    try:
        entries = await asyncio.to_thread(_get_client().list_toplevels)
        for e in entries:
            if "activated" in e.get("state", set()):
                return _entry_to_window(e)
    except Exception as exc:
        LOG.warning("get_active_window failed: %s", exc)
    return None


async def activate_window(wid: int) -> dict:
    try:
        ok = await asyncio.to_thread(_get_client().activate_toplevel, wid)
    except Exception as exc:
        return {"ok": False, "id": wid, "error": str(exc)}
    return {"ok": ok, "id": wid, "error": None if ok else "no such window or no seat"}


async def close_window(wid: int) -> dict:
    try:
        ok = await asyncio.to_thread(_get_client().close_toplevel, wid)
    except Exception as exc:
        return {"ok": False, "id": wid, "error": str(exc)}
    return {"ok": ok, "id": wid, "error": None if ok else "no such window"}


async def minimize_window(wid: int) -> dict:
    try:
        ok = await asyncio.to_thread(_get_client().set_toplevel_minimized, wid, True)
    except Exception as exc:
        return {"ok": False, "id": wid, "error": str(exc)}
    return {"ok": ok, "id": wid, "error": None if ok else "no such window"}


async def maximize_window(wid: int) -> dict:
    try:
        ok = await asyncio.to_thread(_get_client().set_toplevel_maximized, wid, True)
    except Exception as exc:
        return {"ok": False, "id": wid, "error": str(exc)}
    return {"ok": ok, "id": wid, "error": None if ok else "no such window"}


# ── Screen capture ────────────────────────────────────────────────────────────
#
# No ffmpeg demuxer exists for ext-image-copy-capture-v1 -- capabilities/
# webrtc_screen.py detects this module's capture_frame (vs. the X11 path's
# capture_spec) and pulls raw bgra frames from here instead of opening an
# av container. See linux_wayland_client.py's capture_frame_sync for the
# actual shm-buffer negotiation.

def capture_frame() -> tuple[bytes, int, int, int] | None:
    """(bgra bytes, width, height, stride) for one frame, or None on
    failure. Synchronous/blocking -- callers run it in a thread/executor,
    same as SharedX11Capture._grab_one() already does for ffmpeg reads."""
    try:
        return _get_client().capture_frame_sync()
    except Exception as exc:
        LOG.warning("wayland capture_frame failed: %s", exc)
        return None


# ── Capability probe (/yay/capabilities) ────────────────────────────────────

async def probe() -> dict:
    checks: dict[str, object] = {
        "impl": "evdev/uinput + wlr-foreign-toplevel-management + ext-image-copy-capture",
        "ok": True,
        # Permanent gaps vs. X11, not fixable here -- see wm.py, which
        # already returns unsupported_result() for all of these regardless
        # of backend. Surfaced so the phone app can hide the controls
        # instead of showing ones that silently no-op.
        "caveats": [
            "windows.move_resize: no window geometry protocol under Wayland",
            "windows.fullscreen/restore: no equivalent request in "
            "wlr-foreign-toplevel-management",
            "windows.desktops: no workspace protocol wired up",
        ],
    }
    checks["evdev"] = _HAVE_EVDEV
    if not _HAVE_EVDEV:
        checks["ok"] = False
        checks["evdev_hint"] = ("pip install evdev, and ensure this process can "
                                 "read/write /dev/uinput")
    size = await get_display_size()
    checks["display"] = f"{size['width']}x{size['height']}" if size["ok"] else "unknown"
    try:
        client = _get_client()
        checks["seat"] = client.seat is not None
        checks["window_management"] = client.toplevel_manager is not None
        checks["screen_capture"] = bool(client.capture_manager
                                          and client.capture_source_manager and client.shm)
    except Exception as exc:
        checks["ok"] = False
        checks["wayland_client_error"] = str(exc)
    return checks
