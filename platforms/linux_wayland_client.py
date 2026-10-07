# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""Shared, lazily-started Wayland client connection.

The Wayland protocol needs a live connection + a dispatch loop, unlike the
one-shot subprocess calls the rest of this codebase's capabilities use --
so unlike platforms/windows.py or platforms/macos.py (stateless, call a
Win32/Quartz function and get an answer back), the Wayland backend needs
one shared object that owns the connection, used by desktop-size lookups
(linux_wayland.py), window listing, and screen capture alike.

Binding pattern (registry.dispatcher["global"], registry.bind(name, Class,
version), display.roundtrip()) matches trixie-overlay/overlay.py's proven
working Wayland client -- same account, same box, already confirmed to
work against this compositor.
"""

import logging
import mmap
import os
import struct
import threading
import time

from pywayland.client import Display

from wlproto.wayland import WlOutput, WlSeat, WlShm
from wlproto.ext_image_copy_capture_v1.ext_image_copy_capture_manager_v1 import (
    ExtImageCopyCaptureManagerV1,
)
from wlproto.ext_image_capture_source_v1.ext_output_image_capture_source_manager_v1 import (
    ExtOutputImageCaptureSourceManagerV1,
)
from wlproto.wlr_foreign_toplevel_management_unstable_v1.zwlr_foreign_toplevel_manager_v1 import (
    ZwlrForeignToplevelManagerV1,
)

LOG = logging.getLogger("trixie-gateway.platforms.linux_wayland_client")

_WL_OUTPUT_MODE_CURRENT = 0x1

# wl_shm.format enum values this client will accept, in preference order.
# Both are packed 32-bit little-endian (byte order B,G,R,A/X) -- same memory
# layout av/PyAV calls "bgra", so either maps to that format name with no
# repacking. xrgb8888 preferred since its 4th byte is defined padding rather
# than a possibly-meaningful alpha channel the video track ignores anyway.
_SHM_XRGB8888 = 1
_SHM_ARGB8888 = 0
_PREFERRED_SHM_FORMATS = (_SHM_XRGB8888, _SHM_ARGB8888)

# zwlr_foreign_toplevel_handle_v1.state enum (wire value -> name). Window
# management uses this protocol, not ext-foreign-toplevel-list-v1 -- the
# ext- one is read-only (list only, no activate/close/minimize/maximize
# requests exist on it at all), confirmed by reading its generated bindings
# against the live mango session. wlr-foreign-toplevel-management is the
# protocol mango actually built control support against (see its
# protocols/meson.build).
_TOPLEVEL_STATE = {0: "maximized", 1: "minimized", 2: "activated", 3: "fullscreen"}


class WaylandClient:
    def __init__(self):
        self._display = Display()
        self._display.connect()

        self.seat = None               # WlSeatProxy | None
        self.toplevel_manager = None   # ZwlrForeignToplevelManagerV1Proxy | None
        self.capture_manager = None    # ExtImageCopyCaptureManagerV1Proxy | None
        self.capture_source_manager = None  # ExtOutputImageCaptureSourceManagerV1Proxy | None
        self.shm = None                # WlShmProxy | None
        self._outputs = []             # [{"proxy":..., "width":0, "height":0}]
        self._toplevels: dict[int, dict] = {}   # synthetic id -> entry
        self._next_toplevel_id = 1
        self._state_lock = threading.Lock()   # guards _toplevels / _next_toplevel_id

        self._capture_session = None   # ExtImageCopyCaptureSessionV1Proxy | None
        self._capture_constraints = None  # {"width","height","formats","done": Event}
        self._capture_buffer = None    # {"fd","mmap","proxy","width","height","stride","format"}
        self._capture_lock = threading.Lock()  # serializes capture_frame_sync callers

        registry = self._display.get_registry()
        registry.dispatcher["global"] = self._on_global
        self._display.roundtrip()  # collect all globals
        self._display.roundtrip()  # let bound wl_output/toplevel objects send their state

        missing = [n for n, o in (
            ("wl_seat", self.seat),
            ("zwlr_foreign_toplevel_manager_v1", self.toplevel_manager),
            ("ext_image_copy_capture_manager_v1", self.capture_manager),
            ("ext_output_image_capture_source_manager_v1", self.capture_source_manager),
        ) if o is None]
        if missing:
            LOG.warning(
                "compositor does not offer: %s -- the corresponding "
                "capability will report unsupported rather than fail "
                "confusingly", ", ".join(missing))

        self._running = True
        self._thread = threading.Thread(
            target=self._pump, name="wayland-client", daemon=True)
        self._thread.start()

    def _on_global(self, registry, name, interface, version):
        if interface == "wl_output":
            output = registry.bind(name, WlOutput, min(version, 3))
            entry = {"proxy": output, "width": 0, "height": 0}
            output.dispatcher["mode"] = lambda o, flags, w, h, refresh, _e=entry: (
                self._on_output_mode(_e, flags, w, h))
            self._outputs.append(entry)
        elif interface == "wl_seat":
            self.seat = registry.bind(name, WlSeat, min(version, 7))
        elif interface == "zwlr_foreign_toplevel_manager_v1":
            self.toplevel_manager = registry.bind(name, ZwlrForeignToplevelManagerV1, version)
            self.toplevel_manager.dispatcher["toplevel"] = self._on_toplevel
            self.toplevel_manager.dispatcher["finished"] = lambda *_a: None
        elif interface == "ext_image_copy_capture_manager_v1":
            self.capture_manager = registry.bind(
                name, ExtImageCopyCaptureManagerV1, version)
        elif interface == "ext_output_image_capture_source_manager_v1":
            self.capture_source_manager = registry.bind(
                name, ExtOutputImageCaptureSourceManagerV1, version)
        elif interface == "wl_shm":
            self.shm = registry.bind(name, WlShm, min(version, 1))

    @staticmethod
    def _on_output_mode(entry, flags, width, height):
        if flags & _WL_OUTPUT_MODE_CURRENT:
            entry["width"], entry["height"] = width, height

    def _on_toplevel(self, manager, handle):
        with self._state_lock:
            sid = self._next_toplevel_id
            self._next_toplevel_id += 1
        entry = {"id": sid, "handle": handle, "title": "", "app_id": "",
                  "state": set(), "closed": False}
        with self._state_lock:
            self._toplevels[sid] = entry
        handle.dispatcher["title"] = lambda h, t, _e=entry: _e.__setitem__("title", t)
        handle.dispatcher["app_id"] = lambda h, a, _e=entry: _e.__setitem__("app_id", a)
        handle.dispatcher["state"] = lambda h, s, _e=entry: _e.__setitem__(
            "state", self._decode_state(s))
        handle.dispatcher["closed"] = lambda h, _e=entry: _e.__setitem__("closed", True)
        handle.dispatcher["done"] = lambda h: None
        handle.dispatcher["output_enter"] = lambda h, o: None
        handle.dispatcher["output_leave"] = lambda h, o: None

    @staticmethod
    def _decode_state(raw: bytes) -> set[str]:
        """zwlr_foreign_toplevel_handle_v1.state's event arg is a packed
        array of little-endian uint32 enum values, not a bitmask -- unpack
        it rather than treating raw as flags."""
        n = len(raw) // 4
        return {_TOPLEVEL_STATE.get(v, str(v))
                for v, in struct.iter_unpack("<I", raw[:n * 4])}

    def list_toplevels(self) -> list[dict]:
        with self._state_lock:
            return [dict(e) for e in self._toplevels.values() if not e["closed"]]

    def get_toplevel(self, sid: int) -> dict | None:
        with self._state_lock:
            e = self._toplevels.get(sid)
            return dict(e) if e and not e["closed"] else None

    def _handle_for(self, sid: int):
        with self._state_lock:
            e = self._toplevels.get(sid)
            return e["handle"] if e and not e["closed"] else None

    def activate_toplevel(self, sid: int) -> bool:
        handle = self._handle_for(sid)
        if handle is None or self.seat is None:
            return False
        handle.activate(self.seat)
        self._display.flush()
        return True

    def close_toplevel(self, sid: int) -> bool:
        handle = self._handle_for(sid)
        if handle is None:
            return False
        handle.close()
        self._display.flush()
        return True

    def set_toplevel_maximized(self, sid: int, maximized: bool) -> bool:
        handle = self._handle_for(sid)
        if handle is None:
            return False
        handle.set_maximized() if maximized else handle.unset_maximized()
        self._display.flush()
        return True

    def set_toplevel_minimized(self, sid: int, minimized: bool) -> bool:
        handle = self._handle_for(sid)
        if handle is None:
            return False
        handle.set_minimized() if minimized else handle.unset_minimized()
        self._display.flush()
        return True

    def _pump(self):
        while self._running:
            try:
                self._display.dispatch(block=True)
            except Exception as exc:
                LOG.error("wayland dispatch loop stopped: %s", exc)
                self._running = False

    # ── Screen capture (ext-image-copy-capture-v1 + ext-image-capture-source-v1) ──
    #
    # No ffmpeg demuxer exists for this protocol pair -- frames arrive as
    # shm buffers the client allocates itself. One session against the
    # first output, reused across frames; the buffer is reallocated only
    # when the session reports a size change.

    def _ensure_capture_session(self) -> bool:
        if self._capture_session is not None:
            return True
        if not (self.capture_manager and self.capture_source_manager
                and self.shm and self._outputs):
            return False
        source = self.capture_source_manager.create_source(self._outputs[0]["proxy"])
        session = self.capture_manager.create_session(source, 0)  # options=0: no cursor paint
        constraints = {"width": 0, "height": 0, "formats": [], "done": threading.Event()}

        def _on_buffer_size(s, w, h, _c=constraints):
            _c["width"], _c["height"] = w, h

        session.dispatcher["buffer_size"] = _on_buffer_size
        session.dispatcher["shm_format"] = lambda s, f, _c=constraints: _c["formats"].append(f)
        session.dispatcher["dmabuf_device"] = lambda s, d: None
        session.dispatcher["dmabuf_format"] = lambda s, f, m: None
        session.dispatcher["done"] = lambda s, _c=constraints: _c["done"].set()
        session.dispatcher["stopped"] = lambda s: LOG.warning(
            "wayland capture session stopped by compositor")
        self._capture_session = session
        self._capture_constraints = constraints
        self._display.flush()
        if not constraints["done"].wait(timeout=3.0):
            LOG.warning("wayland capture session constraints timed out")
            return False
        return True

    def _alloc_capture_buffer(self) -> bool:
        c = self._capture_constraints
        w, h = c["width"], c["height"]
        if not w or not h:
            return False
        fmt = next((f for f in _PREFERRED_SHM_FORMATS if f in c["formats"]),
                   c["formats"][0] if c["formats"] else None)
        if fmt is None:
            return False
        stride = w * 4
        size = stride * h
        fd = os.memfd_create("trixie-gateway-capture", os.MFD_CLOEXEC)
        try:
            os.ftruncate(fd, size)
            pool = self.shm.create_pool(fd, size)
            buf_proxy = pool.create_buffer(0, w, h, stride, fmt)
            pool.destroy()
            self._display.flush()
            mm = mmap.mmap(fd, size)
        finally:
            os.close(fd)  # mapping (and the compositor's dup'd fd) outlive this
        old = self._capture_buffer
        self._capture_buffer = {"mmap": mm, "proxy": buf_proxy,
                                  "width": w, "height": h, "stride": stride, "format": fmt}
        if old is not None:
            try:
                old["mmap"].close()
                old["proxy"].destroy()
            except Exception:
                pass
        return True

    def capture_frame_sync(self, timeout: float = 2.0) -> tuple[bytes, int, int, int] | None:
        """One synchronous capture -> (bgra bytes, width, height, stride), or
        None on failure/timeout. Call from a worker thread (blocks on a
        threading.Event set by the pump thread's dispatch loop)."""
        with self._capture_lock:
            if not self._ensure_capture_session():
                return None
            c = self._capture_constraints
            buf = self._capture_buffer
            if buf is None or buf["width"] != c["width"] or buf["height"] != c["height"]:
                if not self._alloc_capture_buffer():
                    return None
                buf = self._capture_buffer

            result = {"event": threading.Event(), "ok": False, "reason": None}
            frame = self._capture_session.create_frame()
            frame.dispatcher["transform"] = lambda f, t: None
            frame.dispatcher["damage"] = lambda f, x, y, w, h: None
            frame.dispatcher["presentation_time"] = lambda f, hi, lo, ns: None

            def _on_ready(f, _r=result):
                _r["ok"] = True
                _r["event"].set()

            def _on_failed(f, reason, _r=result):
                _r["reason"] = reason
                _r["event"].set()

            frame.dispatcher["ready"] = _on_ready
            frame.dispatcher["failed"] = _on_failed
            frame.attach_buffer(buf["proxy"])
            frame.damage_buffer(0, 0, buf["width"], buf["height"])
            frame.capture()
            self._display.flush()

            got = result["event"].wait(timeout=timeout)
            frame.destroy()
            self._display.flush()
            if not got:
                LOG.warning("wayland capture frame timed out")
                return None
            if not result["ok"]:
                if result["reason"] == 1:  # buffer_constraints: size changed, drop stale buffer
                    self._capture_buffer = None
                return None

            buf["mmap"].seek(0)
            data = buf["mmap"].read(buf["stride"] * buf["height"])
            return data, buf["width"], buf["height"], buf["stride"]

    def output_size(self) -> tuple[int, int]:
        """Pixel size of the first output. Multi-monitor setups only get
        the first output's size for now -- desktop_input.py's coordinate
        space is already single-display (see get_display_size elsewhere)."""
        for o in self._outputs:
            if o["width"] and o["height"]:
                return o["width"], o["height"]
        return 0, 0


_client: WaylandClient | None = None
_lock = threading.Lock()


def get_client() -> WaylandClient:
    global _client
    with _lock:
        if _client is None:
            _client = WaylandClient()
        return _client
