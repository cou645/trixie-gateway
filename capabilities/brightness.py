"""Brightness capability — screen and keyboard backlight via sysfs."""

from pathlib import Path

_BACKLIGHT = Path("/sys/class/backlight")
_LEDS      = Path("/sys/class/leds")

_SCREEN_NAMES = ("intel_backlight", "acpi_video0", "amdgpu_bl0", "nvidia_backlight")


def get() -> dict:
    result: dict = {"screen": None, "kbd": None}
    dev = _screen_dev()
    if dev:
        try:
            result["screen"] = {"device": dev.name, "pct": _read_pct(dev)}
        except Exception:
            pass
    kbd = _kbd_dev()
    if kbd:
        try:
            result["kbd"] = {"device": kbd.name, "pct": _read_pct(kbd)}
        except Exception:
            pass
    return result


def set_screen(pct: int) -> dict:
    dev = _screen_dev()
    if not dev:
        raise FileNotFoundError("no screen backlight device found")
    clamped = max(1, min(100, pct))  # floor at 1 — never fully blank via this API
    _write_pct(dev, clamped)
    return {"ok": True, "device": dev.name, "pct": clamped}


def set_kbd(pct: int) -> dict:
    dev = _kbd_dev()
    if not dev:
        raise FileNotFoundError("no keyboard backlight device found")
    clamped = max(0, min(100, pct))
    _write_pct(dev, clamped)
    return {"ok": True, "device": dev.name, "pct": clamped}


# ── helpers ───────────────────────────────────────────────────────────────────

def _screen_dev() -> Path | None:
    if not _BACKLIGHT.exists():
        return None
    for name in _SCREEN_NAMES:
        p = _BACKLIGHT / name
        if p.exists():
            return p
    candidates = sorted(_BACKLIGHT.glob("*"))
    return candidates[0] if candidates else None


def _kbd_dev() -> Path | None:
    if not _LEDS.exists():
        return None
    for p in sorted(_LEDS.glob("*kbd_backlight*")):
        if (p / "brightness").exists():
            return p
    return None


def _read_pct(dev: Path) -> int:
    cur  = int((dev / "brightness").read_text().strip())
    maxv = int((dev / "max_brightness").read_text().strip())
    return round(cur * 100 / maxv) if maxv else 0


def _write_pct(dev: Path, pct: int):
    maxv = int((dev / "max_brightness").read_text().strip())
    val  = max(0, min(maxv, round(pct * maxv / 100)))
    (dev / "brightness").write_text(str(val))
