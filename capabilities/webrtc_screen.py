"""WebRTC screen streaming capability — X11 display + audio → phone via aiortc.

Both the video and audio capture are SHARED across peers: one ffmpeg
x11grab and one ffmpeg ALSA capture run regardless of how many peers are
connected, refcounted to start on the first peer and stop once the last
one disconnects. Previously each peer got its own independent capture
(both video and audio) -- fine for 1-2 peers, but N peers meant N
concurrent full-desktop x11grab processes hammering the X server plus N
ALSA capture processes on the same device. Public API unchanged:
create_offer/set_answer/close_peer/list_peers all behave the same from
gateway.py's point of view.

Trade-off from sharing one capture: all peers get the same resolution/
framerate/audio device, set by whichever peer connects first while the
capture is idle. A peer requesting different settings while the shared
capture is already running gets the existing stream's settings instead
(logged, not an error) -- reasonable for this gateway's actual use
(one phone client at a time, occasionally two), revisit if multi-
resolution fan-out is ever needed.
"""

import asyncio
import fractions
import logging
import os
import subprocess
import time

import av
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack, VideoStreamTrack

from platforms import backend as _backend

LOG = logging.getLogger("trixie-gateway.webrtc")


def _clone_frame(frame: av.VideoFrame) -> av.VideoFrame:
    """Returns an independent copy of frame, safe for one peer to mutate
    (.pts/.time_base) without racing every other peer sharing the same
    source frame -- see X11ScreenTrack.recv().

    Without this, every X11ScreenTrack.recv() mutated .pts/.time_base
    directly on the ONE shared _latest_frame from SharedX11Capture. Fine
    for one peer; with two or more, their independent RTCRtpSenders each
    encode that same object on their own background thread, racing --
    confirmed as a real crash (av.error.InvalidDataError) in the
    Desktop-Classroom prototype's identical code, via a real two-peer
    WebRTC test (see its streaming/test_wall.py and streaming/capture.py).

    frame.reformat() looked like the obvious way to get a copy, but it
    short-circuits and returns the *same* object whenever nothing would
    actually change (same width/height/format) -- confirmed via a quick
    interactive check, not documented behavior to rely on. This copies
    each plane's raw bytes into a fresh VideoFrame instead, which works
    regardless of pixel format (including x11grab's native bgr0, which
    av's to_ndarray()/from_ndarray() don't support)."""
    clone = av.VideoFrame(width=frame.width, height=frame.height,
                           format=frame.format.name)
    for i, plane in enumerate(frame.planes):
        clone.planes[i].update(bytes(plane))
    return clone

_DISPLAY   = os.environ.get("DISPLAY", ":0")
_FRAMERATE = 20
_WIDTH     = 1280
_HEIGHT    = 1024

# Active peer connections keyed by id
_peers: dict[str, RTCPeerConnection] = {}


# ── Shared video capture ─────────────────────────────────────────────────────

class SharedX11Capture:
    """One ffmpeg x11grab pipeline, shared by any number of X11ScreenTrack
    consumers. Refcounted by the caller (create_offer/_close)."""

    def __init__(self):
        self._display = None
        self._width = None
        self._height = None
        self._framerate = None

        self._container = None
        self._stream = None
        self._task: asyncio.Task | None = None

        self._latest_frame = None
        self._frame_seq = 0
        self._new_frame = asyncio.Event()
        self.refcount = 0

    @property
    def running(self) -> bool:
        return self._task is not None

    def _open(self):
        # x11grab on Linux; gdigrab (Windows) / avfoundation (macOS) via the
        # platform backend. Same PyAV pipeline either way -- only the input
        # url, format and options differ.
        if _backend:
            url, fmt, opts = _backend.capture_spec(
                self._display, self._width, self._height, self._framerate)
        else:
            url = f"{self._display}.0+0,0"
            fmt = "x11grab"
            opts = {
                "framerate":   str(self._framerate),
                "video_size":  f"{self._width}x{self._height}",
                "draw_mouse":  "1",
                "show_region": "0",
            }
        self._container = av.open(url, format=fmt, options=opts)
        self._stream = next(
            s for s in self._container.streams if s.type == "video"
        )

    async def acquire(self, display: str, width: int, height: int, framerate: int) -> None:
        self.refcount += 1
        if self._task is not None:
            if (width, height, framerate) != (self._width, self._height, self._framerate):
                LOG.warning(
                    "webrtc: peer requested %dx%d@%d but shared capture is "
                    "already running at %dx%d@%d -- serving existing stream",
                    width, height, framerate,
                    self._width, self._height, self._framerate)
            return
        self._display, self._width, self._height, self._framerate = (
            display, width, height, framerate)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._open)
        self._task = asyncio.ensure_future(self._capture_loop())
        LOG.info("webrtc: shared video capture started (%dx%d@%d)",
                  width, height, framerate)

    async def release(self) -> None:
        self.refcount = max(0, self.refcount - 1)
        if self.refcount == 0 and self._task is not None:
            self._task.cancel()
            self._task = None
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:
                    pass
                self._container = None
            LOG.info("webrtc: shared video capture stopped")

    async def _capture_loop(self) -> None:
        loop = asyncio.get_event_loop()
        frame_interval = 1.0 / self._framerate
        try:
            while True:
                t0 = time.monotonic()
                frame = await loop.run_in_executor(None, self._grab_one)
                if frame is not None:
                    self._latest_frame = frame
                    self._frame_seq += 1
                    self._new_frame.set()
                    self._new_frame.clear()
                elapsed = time.monotonic() - t0
                await asyncio.sleep(max(0.0, frame_interval - elapsed))
        except asyncio.CancelledError:
            pass

    def _grab_one(self):
        try:
            pkt = next(self._container.demux(self._stream))
            frames = list(pkt.decode())
            if frames:
                return frames[0]
        except (StopIteration, av.AVError):
            # re-open on error (e.g. display geometry change)
            try:
                self._container.close()
            except Exception:
                pass
            self._open()
        return None

    async def wait_for_frame(self, after_seq: int):
        while self._frame_seq <= after_seq or self._latest_frame is None:
            await self._new_frame.wait()
        return self._latest_frame, self._frame_seq


_shared_video = SharedX11Capture()


class X11ScreenTrack(VideoStreamTrack):
    """Per-peer handle onto the shared video capture."""

    kind = "video"

    def __init__(self, framerate: int = _FRAMERATE):
        super().__init__()
        self._time_base = fractions.Fraction(1, framerate)
        self._pts = 0
        self._last_seq_seen = -1

    async def recv(self):
        frame, seq = await _shared_video.wait_for_frame(self._last_seq_seen)
        self._last_seq_seen = seq
        frame = _clone_frame(frame)
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += 1
        return frame


# ── Shared audio capture (ALSA loopback) ─────────────────────────────────────

_AUDIO_DEV = "hw:Loopback,1"   # capture side of snd-aloop (card 3, device 1)
_AUDIO_RATE = 48000
_AUDIO_CH   = 2
_FRAME_SMPL = 960               # 20 ms @ 48 kHz


def _find_audio_device() -> str:
    """Return the ALSA loopback capture device, or None if unavailable."""
    try:
        import re
        cards = open("/proc/asound/cards").read()
        m = re.search(r"^\s*(\d+)\s+\[Loopback", cards, re.MULTILINE)
        if m:
            return f"hw:{m.group(1)},1"
    except Exception:
        pass
    return _AUDIO_DEV


class SharedAlsaCapture:
    """One ffmpeg ALSA-capture subprocess, fanned out to any number of
    AlsaAudioTrack consumers via a per-consumer queue. Audio, unlike
    video, can't just serve "the latest sample" to new consumers --
    each one needs every sample in order, so fan-out is per-consumer
    queues fed by one shared reader loop, not a single cached value."""

    def __init__(self):
        self._device = None
        self._proc: subprocess.Popen | None = None
        self._task: asyncio.Task | None = None
        self._queues: set[asyncio.Queue] = set()
        self.refcount = 0

    async def acquire(self, device: str) -> None:
        self.refcount += 1
        if self._task is not None:
            return
        self._device = device
        self._proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "quiet",
             "-f", "alsa", "-i", self._device,
             "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CH),
             "-f", "s16le", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._task = asyncio.ensure_future(self._read_loop())
        LOG.info("webrtc: shared audio capture started (%s)", device)

    async def release(self) -> None:
        self.refcount = max(0, self.refcount - 1)
        if self.refcount == 0 and self._task is not None:
            self._task.cancel()
            self._task = None
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
            LOG.info("webrtc: shared audio capture stopped")

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    async def _read_loop(self) -> None:
        needed = _FRAME_SMPL * _AUDIO_CH * 2  # 16-bit = 2 bytes/sample
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, self._proc.stdout.read, needed)
                if not data or len(data) < needed:
                    await asyncio.sleep(0.05)
                    continue
                for q in list(self._queues):
                    if q.full():
                        try:
                            q.get_nowait()  # drop oldest rather than block a slow peer
                        except asyncio.QueueEmpty:
                            pass
                    q.put_nowait(data)
        except asyncio.CancelledError:
            pass


_shared_audio = SharedAlsaCapture()


class AlsaAudioTrack(AudioStreamTrack):
    """Per-peer handle onto the shared audio capture."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._queue = _shared_audio.subscribe()
        self._pts = 0

    async def recv(self):
        data = await self._queue.get()
        frame = av.AudioFrame(format="s16", layout="stereo", samples=_FRAME_SMPL)
        frame.sample_rate = _AUDIO_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _AUDIO_RATE)
        frame.planes[0].update(data)
        self._pts += _FRAME_SMPL
        return frame

    def stop(self):
        super().stop()
        _shared_audio.unsubscribe(self._queue)


# ── public API (unchanged signatures) ────────────────────────────────────────

async def create_offer(
    width: int   = _WIDTH,
    height: int  = _HEIGHT,
    fps: int     = _FRAMERATE,
    audio: bool  = True,
    video: bool  = True,
) -> dict:
    """Create a WebRTC peer, attach shared X11 screen + audio tracks, return
    SDP offer. video=False skips the screen-capture track entirely (not
    just hiding it — the ffmpeg x11grab pipeline never starts) for an
    audio-only peer, e.g. "listen to the desktop on my phone" from the
    Audio screen, as opposed to the full remote-desktop screen share."""
    if not video and not audio:
        return {"ok": False, "error": "at least one of video/audio must be true"}

    pc = RTCPeerConnection()
    peer_id = f"peer-{int(time.time()*1000)}"
    _peers[peer_id] = pc

    _peer_video[peer_id] = video
    if video:
        await _shared_video.acquire(_DISPLAY, width, height, fps)
        track = X11ScreenTrack(framerate=fps)
        pc.addTrack(track)

    peer_has_audio = False
    if audio:
        try:
            dev = _find_audio_device()
            await _shared_audio.acquire(dev)
            audio_track = AlsaAudioTrack()
            pc.addTrack(audio_track)
            peer_has_audio = True
            LOG.info("webrtc [%s] audio track on %s", peer_id, dev)
        except Exception as e:
            LOG.warning("webrtc [%s] audio unavailable: %s", peer_id, e)

    _peer_audio[peer_id] = peer_has_audio

    @pc.on("connectionstatechange")
    async def on_state():
        LOG.info("webrtc [%s] state: %s", peer_id, pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await _close(peer_id)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    # Wait for ICE gathering to complete (host-only, Tailscale = fast)
    deadline = time.monotonic() + 5.0
    while pc.iceGatheringState != "complete" and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    return {
        "ok":      True,
        "peer_id": peer_id,
        "sdp":     pc.localDescription.sdp,
        "type":    pc.localDescription.type,
        "width":   width,
        "height":  height,
        "fps":     fps,
    }


async def set_answer(peer_id: str, sdp: str, sdp_type: str = "answer") -> dict:
    """Receive SDP answer from the Flutter client."""
    pc = _peers.get(peer_id)
    if pc is None:
        return {"ok": False, "error": f"unknown peer: {peer_id}"}
    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
    return {"ok": True, "peer_id": peer_id, "state": pc.connectionState}


async def close_peer(peer_id: str) -> dict:
    """Close and clean up a peer connection."""
    if peer_id not in _peers:
        return {"ok": False, "error": f"unknown peer: {peer_id}"}
    await _close(peer_id)
    return {"ok": True, "peer_id": peer_id}


async def list_peers() -> list[dict]:
    return [
        {"peer_id": pid, "state": pc.connectionState}
        for pid, pc in _peers.items()
    ]


_peer_audio: dict[str, bool] = {}
_peer_video: dict[str, bool] = {}


async def _close(peer_id: str):
    pc = _peers.pop(peer_id, None)
    if pc is None:
        # Already closed -- pc.close() below fires connectionstatechange,
        # whose handler calls _close() again for the same peer_id. Without
        # this guard that re-entrant call would double-release the shared
        # capture refcounts for one logical peer disconnect.
        return
    had_audio = _peer_audio.pop(peer_id, False)
    had_video = _peer_video.pop(peer_id, True)  # True: pre-existing peers from before this flag existed
    try:
        await pc.close()
    except Exception:
        pass
    if had_video:
        await _shared_video.release()
    if had_audio:
        await _shared_audio.release()
