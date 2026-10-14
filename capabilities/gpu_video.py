# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# All rights reserved.
#
# This software is proprietary. No licence is granted to use, copy, modify,
# distribute, sublicense or sell it, in whole or in part, without the prior
# written permission of the copyright holder. Licensing: cou645@gmail.com
#
# Copyright is held by the author personally. CHAMELEON-AI-AGENT LTD has not
# paid for this software or for the hours spent writing it, and holds no
# rights in it.
"""gpu_video — which GPU path ffmpeg and mpv use on this machine, for every
Chameleon app (CMC, Kameleon Presenter, Classroom, trixie-gateway,
ppt_to_pseudo_video). Apps ask here instead of hard-coding one vendor, so the
same code runs on Intel, AMD and NVIDIA, and on the CPU when no GPU path works.

Canonical copy: /aufs/devbase/CMC/gpu_video.py. Each app ships its own copy
next to it (shared-module convention) — copy it again after changing it.

Backends (Linux), chosen once per process and probed with tiny test runs:
  vaapi  Intel (i915) and AMD (amdgpu/radeonsi): decode, scale_vaapi, h264_vaapi.
         Intel only with i915.enable_guc=3 — HuC + GuC submission; otherwise a
         VAAPI encode can livelock irq/*-i915 and hang the whole machine.
  cuda   NVIDIA: NVDEC decode, scale_cuda, NVENC encode.
Windows / macOS: no decode/scale chains (software), mpv "auto-safe", and the
first H.264 encoder that passes a probe (nvenc, amf, qsv, videotoolbox).
Only the Intel VAAPI path is tested on real hardware here; AMD and NVIDIA
follow ffmpeg's documented options and need testing on such a machine.
Run this file to see what it picked and to check every pipeline end to end.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile

VENDORS = {"0x8086": "intel", "0x1002": "amd", "0x10de": "nvidia"}
MPV_HWDEC_CODECS = "h264,vc1,hevc,vp8,vp9,av1,prores,mpeg2video"   # mpv leaves MPEG-2 (DVD, DVB-SD) out
_cache: dict = {}


def _run(args, timeout=15) -> bool:
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
                              capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _once(key, fn):
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]


def _intel_safe() -> bool:
    try:
        with open("/sys/module/i915/parameters/enable_guc") as f:
            guc = int(f.read())        # -1 = auto (HuC off); -1 & 3 is truthy, hence guc > 0
        return guc > 0 and guc & 3 == 3
    except (OSError, ValueError):
        return False


def render_device():
    """(vendor, /dev/dri/renderD*) of the first GPU this code knows, Linux only."""
    def find():
        if not sys.platform.startswith("linux"):
            return None, None
        for node in sorted(glob.glob("/sys/class/drm/renderD*")):
            try:
                with open(os.path.join(node, "device", "vendor")) as f:
                    vendor = VENDORS.get(f.read().strip())
            except OSError:
                continue
            if vendor:
                return vendor, "/dev/dri/" + os.path.basename(node)
        return None, None
    return _once("device", find)


def vendor():
    return render_device()[0]


def device():
    return render_device()[1] or "/dev/dri/renderD128"


def backend():
    """"vaapi", "cuda" or None: the GPU path for decoding, scaling and encoding."""
    def pick():
        v = vendor()
        if v == "intel" and not _intel_safe():
            return None
        if v in ("intel", "amd") and _run(_vaapi_encode_probe()):
            return "vaapi"
        if v == "nvidia" and _run(["-f", "lavfi", "-i", "testsrc2=s=256x144:d=0.2",
                                   "-c:v", "h264_nvenc", "-f", "null", "-"]):
            return "cuda"
        return None
    return _once("backend", pick)


def _vaapi_encode_probe(low_power=None, b_frames=False):
    lp = (vendor() == "intel") if low_power is None else low_power
    return ["-vaapi_device", device(), "-f", "lavfi", "-i", "testsrc2=s=256x144:d=0.3",
            "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
            *(["-low_power", "1"] if lp else []), *(["-bf", "2"] if b_frames else []),
            "-f", "null", "-"]


def gpu_ok() -> bool:
    """True when decode, scale and encode all go through the GPU here."""
    return backend() is not None


def encoder(codec="h264"):
    """ffmpeg's hardware encoder for "h264" or "hevc" here, or None (use the CPU)."""
    def pick():
        b = backend()
        if b == "vaapi":
            return f"{codec}_vaapi"
        if b == "cuda":
            return f"{codec}_nvenc"
        if sys.platform.startswith("linux"):
            return None
        for name in (f"{codec}_nvenc", f"{codec}_amf", f"{codec}_qsv", f"{codec}_videotoolbox"):
            if _run(["-f", "lavfi", "-i", "testsrc2=s=256x144:d=0.2", "-pix_fmt", "nv12",
                     "-c:v", name, "-f", "null", "-"]):
                return name
        return None
    return _once("encoder-" + codec, pick)


def b_frames_ok() -> bool:
    """Whether the hardware H.264 encoder can do B-frames (Intel: only the
    full-power encoder, the low-power one ignores -bf)."""
    def probe():
        enc = encoder()
        if enc == "h264_vaapi":
            return _run(_vaapi_encode_probe(low_power=False, b_frames=True))
        return enc is not None
    return _once("bframes", probe)


def encode_args(codec="h264", kbps=None, qp=None, b_frames=False, vbr_max=None):
    """Hardware encoder args: constant bitrate (kbps), VBR (kbps + vbr_max), or
    constant quality (qp, close to x264's CRF scale). b_frames: 2 B-frames
    when the encoder can (better picture per bit, ~2 frames more delay — for
    platform streams, not low-latency ones). [] when there is no hardware
    encoder: the caller uses its software encoder. Feed it frames through
    upload() first."""
    enc = encoder(codec)
    if not enc:
        return []
    bf = b_frames and b_frames_ok()
    if enc.endswith("_vaapi"):
        intel = vendor() == "intel"
        rc = (["-rc_mode", "VBR", "-b:v", f"{kbps}k", "-maxrate", f"{vbr_max}k"] if vbr_max else
              ["-rc_mode", "CBR", "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps}k"]
              if kbps else ["-rc_mode", "CQP", "-qp", str(qp)])
        mode = (["-low_power", "0" if bf else "1"] if intel else []) + ["-bf", "2" if bf else "0"]
        return ["-vaapi_device", device(), "-c:v", enc, *mode, *rc]
    if enc.endswith("_nvenc"):
        rc = (["-rc", "vbr", "-b:v", f"{kbps}k", "-maxrate", f"{vbr_max}k"] if vbr_max else
              ["-rc", "cbr", "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps}k"]
              if kbps else ["-rc", "constqp", "-qp", str(qp)])
        return ["-c:v", enc, "-preset", "p4", "-tune", "hq" if bf else "ll",
                "-bf", "2" if bf else "0", *rc]
    rc = (["-b:v", f"{kbps}k", "-maxrate", f"{vbr_max or kbps}k"] if kbps else ["-q:v", str(qp)])
    return ["-c:v", enc, *(["-bf", "2"] if bf else []), *rc]


def realtime_h264_args(kbps, keyframe_every=30):
    """Hardware H.264 for real-time WebRTC (trixie-gateway, Classroom): no
    B-frames, constrained baseline, an access-unit delimiter per frame (the
    callers split the stream at them), constant bitrate with a ~0.5 s buffer
    so frame sizes stay even. [] when there's no hardware encoder."""
    enc = encoder()
    gop = ["-g", str(keyframe_every), "-aud", "1"]
    cbr = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps // 2}k"]
    if enc == "h264_vaapi":
        return ["-vaapi_device", device(), "-c:v", enc,
                *(["-low_power", "1"] if vendor() == "intel" else []),
                "-async_depth", "1", "-bf", "0", "-profile:v", "constrained_baseline",
                *gop, "-rc_mode", "CBR", *cbr]
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-delay", "0",
                "-bf", "0", "-profile:v", "baseline", *gop, "-rc", "cbr", *cbr]
    return []


def upload(src="nv12"):
    """Filters that turn software frames into what encode_args() wants. src:
    "nv12"/"yuv" (any YUV frame) or "rgb" (bgr0/bgra screen grabs — converted on
    the GPU; out_range=tv keeps their colours). "" means none needed."""
    b = backend()
    if b == "vaapi":
        return "hwupload,scale_vaapi=format=nv12:out_range=tv" if src == "rgb" else "format=nv12,hwupload"
    if b == "cuda":
        return "" if src == "rgb" else "format=nv12"    # NVENC takes bgr0/nv12 as they are
    return "format=nv12" if encoder() else ""


def filter_device_args():
    """Global ffmpeg options naming the GPU for filters that upload frames to it
    (hwupload / hwupload_cuda in gpu_scale(upload_first=True) and upload())."""
    b = backend()
    if b == "vaapi":
        return ["-vaapi_device", device()]
    if b == "cuda":
        return ["-init_hw_device", "cuda=gpu", "-filter_hw_device", "gpu"]
    return []


def decode_args(phone=False):
    """ffmpeg input options that keep decoded frames on the GPU (scale them
    with gpu_scale(), which also downloads them). phone: Android screenrecord
    H.264 — often plain Baseline, which VAAPI rejects unless told to decode
    it as Constrained Baseline (Android never uses Baseline-only features)."""
    b = backend()
    lenient = ["-hwaccel_flags", "allow_profile_mismatch"] if phone else []
    if b == "vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_device", device(), "-hwaccel_output_format", "vaapi",
                *lenient]
    if b == "cuda":
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", *lenient]
    return []


def gpu_scale(w, h, fit="decrease", out="bgra", upload_first=False, rgb_in=False):
    """Filter chain: scale GPU frames to w x h (fit: "decrease" letterbox,
    "increase" fill, None exact) and download them as software `out` frames
    ("bgra" — Qt's RGB32 — or "nv12"). upload_first: the input frames are
    software ones (screen grabs) to send up first; rgb_in: they are RGB.
    None when there's no GPU path (use a software scale)."""
    b = backend()
    aspect = f":force_original_aspect_ratio={fit}:force_divisible_by=2" if fit else ""
    if b == "vaapi":
        rng = ":out_range=tv" if rgb_in and out == "nv12" else ""
        return ((f"hwupload," if upload_first else "")
                + f"scale_vaapi=w={w}:h={h}{aspect}:format={out}{rng},hwdownload,format={out}")
    if b == "cuda":
        # scale_cuda's YUV output is the documented one: NV12, then BGRA on the
        # CPU if asked (cheap at the already-small size)
        return ((f"hwupload_cuda," if upload_first else "")
                + f"scale_cuda=w={w}:h={h}{aspect}:format=nv12,hwdownload,format=nv12"
                + (f",format={out}" if out != "nv12" else ""))
    return None


def mpv_hwdec(copy=False):
    """mpv --hwdec: frames stay on the GPU, or copy=True to bring them back to
    RAM (needed while software video filters run)."""
    b = backend()
    if b == "vaapi":
        return "vaapi-copy" if copy else "vaapi"
    if b == "cuda":
        return "nvdec-copy" if copy else "nvdec"
    if sys.platform.startswith("linux"):
        return "no" if vendor() == "intel" else ("auto-copy-safe" if copy else "auto-safe")
    return "auto-copy-safe" if copy else "auto-safe"


def describe():
    return (f"vendor={vendor()} device={device()} backend={backend()} "
            f"h264={encoder()} hevc={encoder('hevc')} b-frames={b_frames_ok()} mpv={mpv_hwdec()}")


if __name__ == "__main__":
    print(describe())
    if not gpu_ok():
        assert encode_args(kbps=1000) == [] or not sys.platform.startswith("linux")
        print("no GPU path: every app uses its software pipeline")
        sys.exit(0)
    tmp = tempfile.mkdtemp()
    clip = os.path.join(tmp, "in.mp4")
    assert _run(["-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=1", "-pix_fmt", "yuv420p",
                 "-c:v", "libx264", clip]), "can't make the test clip"

    def first_pixel(args, fmt, w, x, y):
        out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args,
                              "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", fmt, "-"],
                             capture_output=True, timeout=30).stdout
        bpp = 4 if fmt == "bgra" else 3
        i = (y * w + x) * bpp
        return list(out[i:i + bpp])

    # 1. decode + scale + download (players: CMC phone panels, Presenter, Classroom)
    px = first_pixel([*decode_args(), "-i", clip, "-vf", gpu_scale(320, 180)], "bgra", 320, 5, 90)
    assert px[2] > 200 and px[0] < 60 and px[1] < 60, f"decode+scale: red bar came out {px}"
    # 2. screen-grab style RGB frames scaled on the GPU (CMC panel grab, Presenter capture)
    px = first_pixel(["-f", "lavfi", "-i", "testsrc2=s=640x360:d=1,format=bgr0",
                      *filter_device_args(),
                      "-vf", gpu_scale(320, 180, out="nv12", upload_first=True, rgb_in=True)],
                     "rgb24", 320, 5, 90)
    assert px[0] > 200 and px[1] < 60 and px[2] < 60, f"rgb upload+scale: red bar came out {px}"
    # 3. encode from YUV and from RGB, plus B-frames when available (Live Stream, gateway, Join)
    for src, vin in (("nv12", "format=yuv420p"), ("rgb", "format=bgr0")):
        out = os.path.join(tmp, f"enc-{src}.mp4")
        up = upload(src)
        assert _run(["-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d=1,{vin}",
                     *(["-vf", up] if up else []), *encode_args(kbps=2000, b_frames=True), out]), \
            f"encode from {src} failed"
        px = first_pixel(["-i", out], "rgb24", 640, 10, 180)
        assert px[0] > 190 and px[1] < 70 and px[2] < 70, f"encode from {src}: red bar came out {px}"
    print("all GPU pipelines OK:", describe())
