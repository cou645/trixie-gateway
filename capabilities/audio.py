"""Audio capability — ALSA control via amixer (X11)."""

import asyncio
import re

# Chromebook audio controls (amixer scontrols)
_PLAYBACK_CTL = "Speaker"
_CAPTURE_CTL  = "Mic"

# amixer @DEFAULT_SINK@ passthrough alias
_SINK_ALIAS   = "@DEFAULT_SINK@"
_SRC_ALIAS    = "@DEFAULT_SOURCE@"


async def get_status() -> dict:
    sink   = await _amixer_info(_PLAYBACK_CTL)
    source = await _amixer_info(_CAPTURE_CTL)
    return {"sink": sink, "source": source}


async def set_volume(pct: int, target: str = _SINK_ALIAS) -> dict:
    pct = max(0, min(100, pct))
    ctl = _resolve(target)
    rc, _, err = await _run(["amixer", "set", ctl, f"{pct}%"])
    return {"ok": rc == 0, "target": ctl, "pct": pct, "error": err.strip() or None}


async def set_mute(muted: bool, target: str = _SINK_ALIAS) -> dict:
    ctl = _resolve(target)
    val = "mute" if muted else "unmute"
    rc, _, err = await _run(["amixer", "set", ctl, val])
    return {"ok": rc == 0, "target": ctl, "muted": muted, "error": err.strip() or None}


async def list_sinks() -> list[dict]:
    rc, out, _ = await _run(["amixer", "scontrols"])
    if rc != 0:
        return []
    sinks = []
    for line in out.splitlines():
        m = re.search(r"'([^']+)',(\d+)", line)
        if not m:
            continue
        name = m.group(1)
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _resolve(target: str) -> str:
    if target in (_SINK_ALIAS, ""):
        return _PLAYBACK_CTL
    if target == _SRC_ALIAS:
        return _CAPTURE_CTL
    return target


async def _amixer_info(ctl: str) -> dict | None:
    rc, out, _ = await _run(["amixer", "get", ctl])
    if rc != 0:
        return None
    # e.g. "Front Left: Playback 30 [60%] [-20.00dB] [on]"
    m = re.search(r"\[(\d+)%\].*\[(on|off)\]", out)
    if not m:
        return None
    pct   = int(m.group(1))
    muted = m.group(2) == "off"
    return {
        "target": ctl,
        "volume": pct / 100,
        "pct":    pct,
        "muted":  muted,
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
