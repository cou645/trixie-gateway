"""Audio capability.

Linux has two audio control surfaces and most desktops run both: a sound
server (PulseAudio, or PipeWire speaking the same protocol) on top of ALSA.
`pactl` is therefore the default here -- it is what the overwhelming majority
of desktops have, it follows the user's default output device, and its
percentages match what the desktop volume slider shows. ALSA/`amixer` is the
fallback for bare-ALSA setups with no sound server.

Control names are *not* hardcoded. The previous version asked amixer for a
control called "Speaker", which exists on some machines and not others (this
development Chromebook exposes 'Master' plus split 'Left Spk'/'Right Spk'
instead), so audio silently reported null. Now the backend and the control
names are both detected at first use.

Hardware-mute quirk (opt-out via TRIXIE_AUDIO_MUTE=sink): on boards that
expose split speaker switches -- the sof-rt5682 Chromebooks being the case in
hand -- muting is done on those ALSA switches rather than on the sound-server
sink. Muting the sink also kills its monitor source, which silences anything
recording or visualising the output (Chameleon Media Center's visualizer, in
practice). Muting the hardware switch stops the speakers while leaving the
stream, and therefore the monitor, intact.
"""

import asyncio
import os
import re

from platforms import backend as _backend

# amixer @DEFAULT_SINK@ passthrough alias (kept: the phone app sends these)
_SINK_ALIAS = "@DEFAULT_SINK@"
_SRC_ALIAS  = "@DEFAULT_SOURCE@"

# Playback controls worth trying, best first. 'Master' covers most desktops;
# the rest are common on laptops/SoC codecs with no Master control.
_PLAYBACK_CANDIDATES = ["Master", "PCM", "Speaker", "Speakers", "Headphone", "Digital"]
_CAPTURE_CANDIDATES  = ["Capture", "Mic", "Microphone", "Internal Mic"]

# Split speaker switches: muting these instead of the sink preserves the
# monitor source (see module docstring). They usually live on the real codec
# card rather than on the default mixer, so detection scans every card.
_SPLIT_MUTE_CANDIDATES = [("Left Spk", "Right Spk"), ("Speaker L", "Speaker R")]

_env = os.environ.get
_detected: dict | None = None


async def _detect() -> dict:
    """Work out once which tools and control names this host actually has."""
    global _detected
    if _detected is not None:
        return _detected

    forced = (_env("TRIXIE_AUDIO_BACKEND") or "auto").lower()
    have_pactl = (await _run(["pactl", "--version"]))[0] == 0
    have_amixer = (await _run(["amixer", "-v"]))[0] == 0

    if forced == "pactl":
        server = have_pactl
    elif forced == "amixer":
        server = False
    else:
        server = have_pactl

    controls = []
    if have_amixer:
        rc, out, _ = await _run(["amixer", "scontrols"])
        if rc == 0:
            controls = re.findall(r"'([^']+)'", out)

    playback = _first_present(_PLAYBACK_CANDIDATES, controls)
    capture = _first_present(_CAPTURE_CANDIDATES, controls)

    mute_mode = (_env("TRIXIE_AUDIO_MUTE") or "auto").lower()
    split, split_card = None, None
    manual = _env("TRIXIE_AUDIO_MUTE_CONTROLS")
    if manual:
        split = [c.strip() for c in manual.split(",") if c.strip()]
        split_card = _env("TRIXIE_AUDIO_CARD")
    elif mute_mode != "sink" and have_amixer:
        split, split_card = await _find_split_controls(controls)
    if mute_mode == "split" and split is None and playback:
        split = [playback]

    _detected = {
        "server": server,               # use pactl for volume/status
        "have_pactl": have_pactl,
        "have_amixer": have_amixer,
        "playback_ctl": playback,
        "capture_ctl": capture,
        "split_mute": split,            # ALSA switches used for muting, if any
        "split_card": split_card,       # card those switches live on, if not default
    }
    return _detected


async def _find_split_controls(default_controls: list[str]) -> tuple[list[str] | None, str | None]:
    """Look for a split speaker-switch pair on the default mixer, then on each
    sound card. On the sof-rt5682 Chromebooks the default mixer only exposes
    'Master' (via the sound server) while the switches that actually cut the
    speakers sit on the codec card."""
    for pair in _SPLIT_MUTE_CANDIDATES:
        if all(c in default_controls for c in pair):
            return list(pair), None
    try:
        cards = re.findall(r"^\s*(\d+)\s+\[", _read_cards(), re.M)
    except Exception:
        cards = []
    for card in cards:
        rc, out, _ = await _run(["amixer", "-c", card, "scontrols"])
        if rc != 0:
            continue
        controls = re.findall(r"'([^']+)'", out)
        for pair in _SPLIT_MUTE_CANDIDATES:
            if all(c in controls for c in pair):
                return list(pair), card
    return None, None


def _read_cards() -> str:
    try:
        with open("/proc/asound/cards") as fh:
            return fh.read()
    except OSError:
        return ""


def _first_present(candidates: list[str], controls: list[str]) -> str | None:
    for c in candidates:
        if c in controls:
            return c
    return controls[0] if controls else None


# ── Public API ───────────────────────────────────────────────────────────────

async def get_status() -> dict:
    if _backend:
        return await _backend.get_status()

    d = await _detect()
    if d["server"]:
        sink = await _pactl_info("sink", "@DEFAULT_SINK@")
        source = await _pactl_info("source", "@DEFAULT_SOURCE@")
        if sink and d["split_mute"]:
            # Report what actually silences the speakers on this board.
            hw = await _amixer_info(d["split_mute"][0], card=d.get("split_card"))
            if hw:
                sink["muted"] = hw["muted"]
        if sink or source:
            return {"sink": sink, "source": source}
        # sound server present but answered nothing -- fall through to ALSA

    sink = await _amixer_info(d["playback_ctl"]) if d["playback_ctl"] else None
    source = await _amixer_info(d["capture_ctl"]) if d["capture_ctl"] else None
    if sink is None and source is None:
        return {"sink": None, "source": None,
                "error": "no usable audio control found (tried pactl and amixer)"}
    return {"sink": sink, "source": source}


async def set_volume(pct: int, target: str = _SINK_ALIAS) -> dict:
    if _backend:
        return await _backend.set_volume(pct, target)

    pct = max(0, min(100, pct))
    d = await _detect()
    if d["server"] and _is_alias(target):
        dev = "@DEFAULT_SOURCE@" if target == _SRC_ALIAS else "@DEFAULT_SINK@"
        kind = "source" if target == _SRC_ALIAS else "sink"
        rc, _, err = await _run(["pactl", f"set-{kind}-volume", dev, f"{pct}%"])
        if rc == 0:
            return {"ok": True, "target": dev, "pct": pct, "error": None}

    ctl = await _resolve(target)
    if not ctl:
        return {"ok": False, "target": target, "pct": pct,
                "error": "no audio control found"}
    rc, _, err = await _run(["amixer", "set", ctl, f"{pct}%"])
    return {"ok": rc == 0, "target": ctl, "pct": pct, "error": err.strip() or None}


async def set_mute(muted: bool, target: str = _SINK_ALIAS) -> dict:
    if _backend:
        return await _backend.set_mute(muted, target)

    d = await _detect()

    # Hardware switches first when present: keeps the monitor source alive.
    if d["split_mute"] and _is_alias(target) and target != _SRC_ALIAS:
        val = "mute" if muted else "unmute"
        card = ["-c", d["split_card"]] if d.get("split_card") else []
        results = [await _run(["amixer", *card, "set", c, val]) for c in d["split_mute"]]
        if all(rc == 0 for rc, _, _ in results):
            return {"ok": True, "target": ", ".join(d["split_mute"]),
                    "muted": muted, "error": None, "via": "alsa-switch"}

    if d["server"] and _is_alias(target):
        dev = "@DEFAULT_SOURCE@" if target == _SRC_ALIAS else "@DEFAULT_SINK@"
        kind = "source" if target == _SRC_ALIAS else "sink"
        rc, _, err = await _run(["pactl", f"set-{kind}-mute", dev, "1" if muted else "0"])
        if rc == 0:
            return {"ok": True, "target": dev, "muted": muted, "error": None}

    ctl = await _resolve(target)
    if not ctl:
        return {"ok": False, "target": target, "muted": muted,
                "error": "no audio control found"}
    rc, _, err = await _run(["amixer", "set", ctl, "mute" if muted else "unmute"])
    return {"ok": rc == 0, "target": ctl, "muted": muted, "error": err.strip() or None}


async def list_sinks() -> list[dict]:
    if _backend:
        return await _backend.list_sinks()

    d = await _detect()
    if d["server"]:
        sinks = await _pactl_sinks()
        if sinks:
            return sinks

    rc, out, _ = await _run(["amixer", "scontrols"])
    if rc != 0:
        return []
    sinks = []
    for name in re.findall(r"'([^']+)'", out):
        info = await _amixer_info(name)
        if info:
            sinks.append({
                "name":        name,
                "description": name,
                "state":       "running",
                "volume_pct":  info["pct"],
                "mute":        info["muted"],
            })
    return sinks


async def describe() -> dict:
    """Which backend and controls are in use — surfaced by /yay/capabilities
    so a machine with unusual controls can be diagnosed without shell access."""
    if _backend:
        return {"impl": "platform-backend"}
    d = await _detect()
    return {
        "impl": "pactl" if d["server"] else "amixer",
        "have_pactl": d["have_pactl"],
        "have_amixer": d["have_amixer"],
        "playback_ctl": d["playback_ctl"],
        "capture_ctl": d["capture_ctl"],
        "mute_via": d["split_mute"] or ("sink" if d["server"] else d["playback_ctl"]),
        "mute_card": d.get("split_card"),
    }


# ── helpers ───────────────────────────────────────────────────────────────────

def _is_alias(target: str) -> bool:
    return target in (_SINK_ALIAS, _SRC_ALIAS, "")


async def _resolve(target: str) -> str | None:
    d = await _detect()
    if target in (_SINK_ALIAS, ""):
        return d["playback_ctl"]
    if target == _SRC_ALIAS:
        return d["capture_ctl"]
    return target


async def _pactl_info(kind: str, dev: str) -> dict | None:
    rc, out, _ = await _run(["pactl", f"get-{kind}-volume", dev])
    if rc != 0:
        return None
    m = re.search(r"/\s*(\d+)%", out)
    if not m:
        return None
    pct = int(m.group(1))
    rc_m, out_m, _ = await _run(["pactl", f"get-{kind}-mute", dev])
    muted = "yes" in out_m.lower() if rc_m == 0 else False
    rc_n, name, _ = await _run(["pactl", f"get-default-{kind}"])
    return {
        "target": name.strip() if rc_n == 0 else dev,
        "name":   name.strip() if rc_n == 0 else dev,
        "volume": pct / 100,
        "pct":    pct,
        "volume_pct": pct,
        "muted":  muted,
        "mute":   muted,
        "state":  "running",
    }


async def _pactl_sinks() -> list[dict]:
    rc, out, _ = await _run(["pactl", "list", "short", "sinks"])
    if rc != 0:
        return []
    _, default_name, _ = await _run(["pactl", "get-default-sink"])
    default_name = default_name.strip()
    sinks = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        state = parts[-1].lower() if len(parts) > 4 else "unknown"
        info = await _pactl_info("sink", name)
        sinks.append({
            "name":        name,
            "description": name,
            "state":       state,
            "default":     name == default_name,
            "volume_pct":  info["pct"] if info else 0,
            "mute":        info["muted"] if info else False,
        })
    return sinks


async def _amixer_info(ctl: str, card: str | None = None) -> dict | None:
    rc, out, _ = await _run(["amixer", *(["-c", card] if card else []), "get", ctl])
    if rc != 0:
        return None
    # e.g. "Front Left: Playback 30 [60%] [-20.00dB] [on]"
    m = re.search(r"\[(\d+)%\].*?\[(on|off)\]", out)
    if m:
        pct, muted = int(m.group(1)), m.group(2) == "off"
    else:
        # Switch-only controls (e.g. 'Left Spk') have no percentage at all.
        m_sw = re.search(r"\[(on|off)\]", out)
        if not m_sw:
            return None
        pct, muted = 100, m_sw.group(1) == "off"
    return {
        "target": ctl,
        "name":   ctl,
        "volume": pct / 100,
        "pct":    pct,
        "volume_pct": pct,
        "muted":  muted,
        "mute":   muted,
        "state":  "running",
    }


async def _run(cmd: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        return 1, "", "timeout"
    except FileNotFoundError:
        return 1, "", f"{cmd[0]}: not found"
