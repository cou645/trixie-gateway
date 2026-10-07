# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""Loopback WebRTC call through the real aiortc sender path: proves the VAAPI
encoder is used, H.264 is negotiated, frames decode, and a dead ffmpeg falls
back to software mid-call. Run: /root/pyqt6-venv/bin/python test_vaapi_h264.py
(needs an i915.enable_guc=3 boot for the hardware half; otherwise checks fallback)."""
import asyncio
import fractions
import sys
import os

import av
import numpy as np
from aiortc import RTCPeerConnection, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vaapi_h264 as vh


class Pattern(VideoStreamTrack):
    """Moving 1280x720 test pattern in bgr0-like bgra, like x11grab frames."""
    def __init__(self):
        super().__init__()
        self.n = 0

    async def recv(self):
        pts, tb = await self.next_timestamp()
        img = np.zeros((720, 1280, 4), np.uint8)
        img[:, (self.n * 8) % 1280:(self.n * 8) % 1280 + 80, 1] = 255
        img[(self.n * 4) % 720:(self.n * 4) % 720 + 40, :, 2] = 255
        self.n += 1
        f = av.VideoFrame.from_ndarray(img, format="bgra")
        f.pts, f.time_base = pts, tb
        return f


async def call(seconds, kill_ffmpeg_at=None):
    sender_pc, receiver_pc = RTCPeerConnection(), RTCPeerConnection()
    sender = sender_pc.addTrack(Pattern())
    vh.prefer_h264(sender_pc, sender)
    got = {"frames": 0, "size": None}

    @receiver_pc.on("track")
    def on_track(track):
        async def pull():
            while True:
                try:
                    f = await track.recv()
                except MediaStreamError:      # call hung up
                    return
                got["frames"] += 1
                got["size"] = (f.width, f.height)
        asyncio.ensure_future(pull())

    await sender_pc.setLocalDescription(await sender_pc.createOffer())
    await receiver_pc.setRemoteDescription(sender_pc.localDescription)
    await receiver_pc.setLocalDescription(await receiver_pc.createAnswer())
    await sender_pc.setRemoteDescription(receiver_pc.localDescription)
    codec = [l for l in receiver_pc.localDescription.sdp.splitlines() if l.startswith("a=rtpmap")][0]

    enc = None
    for t in range(seconds * 10):
        await asyncio.sleep(0.1)
        enc = getattr(sender, "_RTCRtpSender__encoder", None) or enc
        if kill_ffmpeg_at is not None and t == kill_ffmpeg_at * 10 and enc is not None and enc._proc:
            enc._proc.kill()
            got["frames_at_kill"] = got["frames"]
    await sender_pc.close(); await receiver_pc.close()
    return codec, enc, got


async def main():
    hw = vh.vaapi_h264_ok()
    vh.install()
    codec, enc, got = await call(4)
    print(f"negotiated: {codec} | encoder: {type(enc).__name__} | received {got['frames']} frames {got['size']}")
    if hw:
        assert "H264" in codec, codec
        assert type(enc).__name__ == "VaapiH264Encoder" and not enc._software
        assert got["frames"] >= 60 and got["size"] == (1280, 720), got
        codec, enc, got = await call(5, kill_ffmpeg_at=2)
        after = got["frames"] - got["frames_at_kill"]
        print(f"ffmpeg killed at 2s: software fallback={enc._software}, {after} frames received after the kill")
        assert enc._software and after >= 45, got
    else:
        assert type(enc).__name__ != "VaapiH264Encoder" and got["frames"] >= 60, got
    print("OK —", "hardware H.264 path + fallback verified" if hw else "VAAPI off: unchanged software path verified")


asyncio.run(main())
