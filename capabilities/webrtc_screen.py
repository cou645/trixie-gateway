"""WebRTC screen streaming capability — X11 display + audio → phone via aiortc."""

import asyncio
import fractions
import logging
import os
import subprocess
import time

import av
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole

LOG = logging.getLogger("trixie-gateway.webrtc")

_DISPLAY   = os.environ.get("DISPLAY", ":0")
_FRAMERATE = 20
_WIDTH     = 1280
_HEIGHT    = 1024

# Active peer connections keyed by id
_peers: dict[str, RTCPeerConnection] = {}


class X11ScreenTrack(VideoStreamTrack):
    """Captures the X11 display via FFmpeg x11grab and emits VideoFrames."""

    kind = "video"

    def __init__(self, display: str = _DISPLAY,
                 width: int = _WIDTH, height: int = _HEIGHT,
                 framerate: int = _FRAMERATE):
        super().__init__()
        self._display   = display
        self._width     = width
        self._height    = height
        self._framerate = framerate
        self._container = None
        self._stream    = None
        self._pts       = 0
        self._time_base = fractions.Fraction(1, framerate)

    def _open(self):
        opts = {
            "framerate":    str(self._framerate),
            "video_size":   f"{self._width}x{self._height}",
            "draw_mouse":   "1",
            "show_region":  "0",
        }
        self._container = av.open(
            f"{self._display}.0+0,0",
            format="x11grab",
            options=opts,
        )
        self._stream = next(
            s for s in self._container.streams if s.type == "video"
        )

    async def recv(self):
        await asyncio.sleep(1.0 / self._framerate)

        loop = asyncio.get_event_loop()
        frame = await loop.run_in_executor(None, self._grab_frame)

        frame.pts      = self._pts
        frame.time_base = self._time_base
        self._pts      += 1
        return frame

    def _grab_frame(self):
        if self._container is None:
            self._open()
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
            self._container = None
            self._open()
            return self._grab_frame()

    def stop(self):
        super().stop()
        if self._container:
            try:
                self._container.close()
            except Exception:
                pass
            self._container = None


# ── Audio capture (ALSA loopback) ────────────────────────────────────────────

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


class AlsaAudioTrack(AudioStreamTrack):
    """Captures ALSA loopback device and emits 20-ms AudioFrames."""

    kind = "audio"

    def __init__(self, device: str = _AUDIO_DEV):
        super().__init__()
        self._device = device
        self._proc   = None
        self._pts    = 0

    def _start(self):
        self._proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "quiet",
             "-f", "alsa", "-i", self._device,
             "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CH),
             "-f", "s16le", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    async def recv(self):
        if self._proc is None:
            self._start()

        needed = _FRAME_SMPL * _AUDIO_CH * 2  # 16-bit = 2 bytes/sample
        loop   = asyncio.get_event_loop()
        data   = await loop.run_in_executor(None, self._proc.stdout.read, needed)

        if not data or len(data) < needed:
            # Pipe closed — restart
            self._proc = None
            await asyncio.sleep(0.05)
            return await self.recv()

        frame = av.AudioFrame(format="s16", layout="stereo", samples=_FRAME_SMPL)
        frame.sample_rate = _AUDIO_RATE
        frame.pts         = self._pts
        frame.time_base   = fractions.Fraction(1, _AUDIO_RATE)
        frame.planes[0].update(data)
        self._pts += _FRAME_SMPL
        return frame

    def stop(self):
        super().stop()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


# ── public API ────────────────────────────────────────────────────────────────

async def create_offer(
    width: int  = _WIDTH,
    height: int = _HEIGHT,
    fps: int    = _FRAMERATE,
    audio: bool = True,
) -> dict:
    """Create a WebRTC peer, attach X11 screen + audio tracks, return SDP offer."""
    pc = RTCPeerConnection()
    peer_id = f"peer-{int(time.time()*1000)}"
    _peers[peer_id] = pc

    track = X11ScreenTrack(display=_DISPLAY, width=width, height=height, framerate=fps)
    pc.addTrack(track)

    if audio:
        try:
            dev = _find_audio_device()
            audio_track = AlsaAudioTrack(device=dev)
            pc.addTrack(audio_track)
            LOG.info("webrtc [%s] audio track on %s", peer_id, dev)
        except Exception as e:
            LOG.warning("webrtc [%s] audio unavailable: %s", peer_id, e)

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


async def _close(peer_id: str):
    pc = _peers.pop(peer_id, None)
    if pc:
        try:
            await pc.close()
        except Exception:
            pass
