"""Proves the shared-capture fix in webrtc_screen.py: N peers, exactly
one ffmpeg x11grab (and one ffmpeg ALSA capture, if audio requested).

Runs entirely in-process against the real public API (create_offer/
set_answer/close_peer), with each "viewer" being a real aiortc
RTCPeerConnection acting as a WebRTC client -- exercises the actual
offer/answer/ICE/media path, not just the Python objects.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from aiortc import RTCPeerConnection, RTCSessionDescription

import webrtc_screen as ws


async def wait_ice_complete(pc: RTCPeerConnection) -> None:
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_change():
        if pc.iceGatheringState == "complete":
            done.set()

    await asyncio.wait_for(done.wait(), timeout=10)


async def make_viewer(name: str, frame_counts: dict):
    result = await ws.create_offer(width=1280, height=720, fps=15, audio=True)
    peer_id = result["peer_id"]

    pc = RTCPeerConnection()
    frame_counts[name] = 0

    @pc.on("track")
    def on_track(track):
        async def counter():
            try:
                while True:
                    await track.recv()
                    frame_counts[name] += 1
            except Exception:
                pass
        asyncio.ensure_future(counter())

    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=result["sdp"], type=result["type"]))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await wait_ice_complete(pc)

    await ws.set_answer(peer_id, pc.localDescription.sdp, pc.localDescription.type)

    return pc, peer_id


async def main():
    frame_counts: dict[str, int] = {}

    pc_a, id_a = await make_viewer("viewer_a", frame_counts)
    print(f"viewer_a connected, peer_id={id_a}, "
          f"video refcount={ws._shared_video.refcount}, "
          f"video running={ws._shared_video.running}")

    pc_b, id_b = await make_viewer("viewer_b", frame_counts)
    print(f"viewer_b connected, peer_id={id_b}, "
          f"video refcount={ws._shared_video.refcount}, "
          f"video running={ws._shared_video.running}")

    await asyncio.sleep(4)

    print(f"frame_counts after 4s: {frame_counts}")

    peers_now = await ws.list_peers()
    print(f"list_peers(): {peers_now}")

    assert ws._shared_video.refcount == 2, (
        f"expected refcount 2 (two active peers), got {ws._shared_video.refcount}")
    assert frame_counts["viewer_a"] > 0, "viewer_a received no video frames"
    assert frame_counts["viewer_b"] > 0, "viewer_b received no video frames"

    print("PASS (video): one shared x11grab capture served both viewers, "
          "refcount tracked correctly")

    await ws.close_peer(id_a)
    print(f"after closing viewer_a: video refcount={ws._shared_video.refcount}, "
          f"running={ws._shared_video.running}")
    assert ws._shared_video.running, "capture should stay up -- viewer_b still connected"

    await ws.close_peer(id_b)
    print(f"after closing viewer_b: video refcount={ws._shared_video.refcount}, "
          f"running={ws._shared_video.running}")
    assert not ws._shared_video.running, "capture should stop -- no peers left"

    print("PASS (lifecycle): capture stayed up while a peer remained, "
          "stopped once the last one disconnected")

    await pc_a.close()
    await pc_b.close()


if __name__ == "__main__":
    asyncio.run(main())
