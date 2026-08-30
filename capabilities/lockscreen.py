"""Lockscreen capability — remote lock/unlock via Chameleon lockscreen.py."""

import asyncio
import os
import signal
from pathlib import Path

_LOCKSCREEN_PY = Path("/root/lockscreen.py")
_PID_FILE      = Path("/run/chameleon-lock.pid")
_DISPLAY       = os.environ.get("DISPLAY", ":0")


async def get_status() -> dict:
    pid = _read_pid()
    if pid and _process_alive(pid):
        return {"locked": True, "pid": pid}
    _pid_file_cleanup()
    return {"locked": False, "pid": None}


async def lock() -> dict:
    status = await get_status()
    if status["locked"]:
        return {"ok": True, "already_locked": True, "pid": status["pid"]}

    env = {**os.environ, "DISPLAY": _DISPLAY}
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", str(_LOCKSCREEN_PY), "--lock",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _PID_FILE.write_text(str(proc.pid))
        return {"ok": True, "pid": proc.pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def unlock() -> dict:
    pid = _read_pid()
    if not pid or not _process_alive(pid):
        _pid_file_cleanup()
        return {"ok": True, "already_unlocked": True}
    try:
        os.kill(pid, signal.SIGTERM)
        _pid_file_cleanup()
        return {"ok": True, "pid": pid}
    except ProcessLookupError:
        _pid_file_cleanup()
        return {"ok": True, "already_unlocked": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── helpers ───────────────────────────────────────────────────────────────────

def _read_pid() -> int | None:
    try:
        return int(_PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _pid_file_cleanup():
    _PID_FILE.unlink(missing_ok=True)
