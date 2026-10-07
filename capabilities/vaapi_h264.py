# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""Intel VAAPI hardware H.264 for the WebRTC screen share, with software fallback.

aiortc encodes with the ffmpeg bundled in PyAV, which has no h264_vaapi, so the
screen share ran libx264/libvpx on the CPU (~2 cores). VaapiH264Encoder pipes
NV12 frames into a system `ffmpeg -c:v h264_vaapi` and hands its Annex-B output
back to aiortc's own H.264 packetizer. install() swaps it in for aiortc's
H264Encoder and prefer_h264() puts H.264 first in the offer (VP8 stays listed).

Fallback: nothing changes unless vaapi_h264_ok() — only on an i915.enable_guc=3
boot (with execlists, i.e. enable_guc unset or 2, a VAAPI encode livelocks
irq/*-i915 on this PREEMPT_RT kernel and freezes the whole machine) and after a
0.2 s test encode passes. If ffmpeg dies mid-stream that encoder instance drops
back to aiortc's libx264 for the rest of the session.
"""
import logging
import queue
import subprocess
import threading
import time
import weakref

from aiortc import rtcrtpsender
from aiortc.codecs import h264 as _h264

LOG = logging.getLogger("trixie-gateway.vaapi")
DEVICE = "/dev/dri/renderD128"
KEYFRAME_EVERY = 30        # frames: a phone's keyframe request is met within ~1 s
RESTART_MIN_GAP = 5.0      # s between bitrate-driven encoder restarts
_ok = None


def vaapi_h264_ok() -> bool:
    """True if h264_vaapi is safe and works here (probed once)."""
    global _ok
    if _ok is None:
        _ok = False
        try:
            with open("/sys/module/i915/parameters/enable_guc") as f:
                guc = int(f.read())   # -1 = auto (HuC off); -1 & 3 is truthy, hence guc > 0
            if guc > 0 and guc & 3 == 3:
                _ok = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-vaapi_device", DEVICE,
                     "-f", "lavfi", "-i", "testsrc2=s=256x144:d=0.2", "-vf", "format=nv12,hwupload",
                     "-c:v", "h264_vaapi", "-low_power", "1", "-f", "null", "-"],
                    capture_output=True, timeout=15).returncode == 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        LOG.info("VAAPI H.264 for screen share: %s", "enabled" if _ok else "off (software encoder)")
    return _ok


def _read_access_units(stdout, out: queue.Queue):
    """Split ffmpeg's Annex-B stream at access unit delimiters (00 00 01 09)."""
    buf = b""
    while chunk := stdout.read1(1 << 16):
        buf += chunk
        while (i := buf.find(b"\x00\x00\x01\x09", 4)) != -1:
            cut = i - 1 if buf[i - 1] == 0 else i        # 4-byte start code
            out.put(buf[:cut])
            buf = buf[cut:]
    if buf:
        out.put(buf)


class VaapiH264Encoder(_h264.H264Encoder):
    def __init__(self):
        super().__init__()
        self._proc = None
        self._aus: queue.Queue = queue.Queue()
        self._size = None
        self._kbps = 0
        self._started = 0.0
        self._software = False

    def _stop(self):
        if self._proc:
            self._proc.kill()
            self._proc = None

    def _start(self, w, h):
        self._stop()
        self._kbps = self.target_bitrate // 1000
        self._proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "nv12", "-s", f"{w}x{h}", "-r", str(_h264.MAX_FRAME_RATE),
             "-i", "pipe:0", "-vaapi_device", DEVICE, "-vf", "hwupload",
             "-c:v", "h264_vaapi", "-low_power", "1", "-async_depth", "1", "-bf", "0",
             "-profile:v", "constrained_baseline", "-aud", "1", "-g", str(KEYFRAME_EVERY),
             "-rc_mode", "CBR", "-b:v", f"{self._kbps}k", "-maxrate", f"{self._kbps}k",
             "-bufsize", f"{self._kbps // 2}k",          # ~0.5 s: keeps frame sizes even
             "-flush_packets", "1", "-f", "h264", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._aus = queue.Queue()
        threading.Thread(target=_read_access_units, args=(self._proc.stdout, self._aus),
                         daemon=True).start()
        weakref.finalize(self, self._proc.kill)
        self._size, self._started = (w, h), time.monotonic()
        LOG.info("VAAPI H.264 encoder: %dx%d @ %d kbps", w, h, self._kbps)

    def _encode_frame(self, frame, force_keyframe):
        if self._software:
            yield from super()._encode_frame(frame, force_keyframe)
            return
        w, h = frame.width & ~1, frame.height & ~1
        drift = abs(self.target_bitrate // 1000 - self._kbps) / max(self._kbps, 1)
        if (self._proc is None or (w, h) != self._size
                or (drift > 0.3 and time.monotonic() - self._started > RESTART_MIN_GAP)):
            self._start(w, h)
        try:
            if self._proc.poll() is not None:   # died on its own: don't restart-loop a failing GPU
                raise OSError(f"ffmpeg exited with {self._proc.returncode}")
            self._proc.stdin.write(frame.reformat(width=w, height=h, format="nv12").to_ndarray().tobytes())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            LOG.warning("VAAPI encoder failed (%s) — falling back to software H.264", e)
            self._stop()
            self._software = True
            yield from super()._encode_frame(frame, force_keyframe)
            return
        # Output lags input by about one frame (an AU is complete when the next AUD
        # arrives); wait up to one frame interval for it, then take whatever is ready.
        try:
            aus = [self._aus.get(timeout=1 / _h264.MAX_FRAME_RATE)]
        except queue.Empty:
            return
        while not self._aus.empty():
            aus.append(self._aus.get_nowait())
        for au in aus:
            for nal in self._split_bitstream(au):
                if nal and nal[0] & 0x1F != 9:        # AUDs are only our frame markers
                    yield nal


def install():
    """Use VaapiH264Encoder for every H.264 sender, when VAAPI is safe here."""
    if not vaapi_h264_ok() or getattr(rtcrtpsender.get_encoder, "_vaapi", False):
        return
    original = rtcrtpsender.get_encoder

    def get_encoder(codec):
        if codec.mimeType.lower() == "video/h264":
            return VaapiH264Encoder()
        return original(codec)
    get_encoder._vaapi = True
    rtcrtpsender.get_encoder = get_encoder


def prefer_h264(pc, sender):
    """Put H.264 first in this video sender's offer so the phone picks the hardware
    path; VP8 etc. stay listed after it as fallbacks."""
    if not vaapi_h264_ok():
        return
    from aiortc.rtcrtpsender import RTCRtpSender
    codecs = RTCRtpSender.getCapabilities("video").codecs
    ordered = ([c for c in codecs if c.mimeType.lower() == "video/h264"]
               + [c for c in codecs if c.mimeType.lower() != "video/h264"])
    for t in pc.getTransceivers():
        if t.sender is sender:
            t.setCodecPreferences(ordered)
