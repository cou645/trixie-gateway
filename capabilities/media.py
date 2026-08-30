"""Media capability — yay --media control via pipe IPC (multi-session)."""

import asyncio
import os
import re
from pathlib import Path

_MEDIA_DIR  = Path("/mnt/sda2/YaYOS/layers/base/root/media")
if not _MEDIA_DIR.is_dir():
    # fatdog64-style /aufs stacking fallback (cross-distro)
    _MEDIA_DIR = Path("/aufs/devbase/YaYOS/layers/base/root/media")
_DISPLAY    = os.environ.get("DISPLAY", ":0")
_MEDIA_EXTS = {".mp3", ".mp4", ".wav", ".ogg", ".flac", ".mkv",
               ".avi", ".webm", ".m4a", ".aac", ".opus"}

# stdin streams for sessions launched by this gateway process
_LAUNCHED: dict[int, asyncio.StreamWriter] = {}


# ── proc scanning ─────────────────────────────────────────────────────────────

def _all_sessions() -> list[dict]:
    sessions = []
    for p in sorted(Path("/proc").iterdir(), key=lambda x: x.name):
        if not p.name.isdigit():
            continue
        try:
            raw = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if "yay" not in raw or "--media" not in raw:
            continue

        pid        = int(p.name)
        has_listen = "--listen" in raw

        # title: match --title=... or --title "..." (possibly quoted, possibly multi-word)
        title_m = re.search(r"--title[= ]['\"]?([^'\"]+?)['\"]?(?:\s+--|$|\s+yay)", raw)
        if not title_m:
            title_m = re.search(r"--title[= ](\S+)", raw)
        title = title_m.group(1).strip() if title_m else None

        device_m = re.search(r"--media-device[= ](\S+)", raw)
        device   = device_m.group(1) if device_m else None

        source       = _classify_source(device)
        current_file = _open_media_file(pid)
        if not title and current_file:
            title = Path(current_file).name
        if not title:
            title = source

        # controllable if: launched by us, has a proc writer, or was started with --listen
        writer       = _LAUNCHED.get(pid)
        writer_pid   = None if writer else _find_writer(pid)
        controllable = (writer is not None) or (writer_pid is not None) or has_listen

        sessions.append({
            "pid":          pid,
            "title":        title,
            "source":       source,
            "device":       device,
            "current_file": current_file,
            "has_listen":   has_listen,
            "controllable": controllable,
            "writer_pid":   writer_pid,
            "audio_active": _audio_active(pid),
        })
    return sessions


def _classify_source(device: str | None) -> str:
    if device is None:       return "file"
    if device == "adb":      return "adb"
    if device.startswith("screen"): return "screen"
    if "v4l2" in device or device.startswith("/dev/video"): return "webcam"
    return "device"


def _open_media_file(pid: int) -> str | None:
    try:
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                link = os.readlink(str(fd))
                if Path(link).suffix.lower() in _MEDIA_EXTS:
                    return link
            except OSError:
                continue
    except OSError:
        pass
    return None


def _audio_active(pid: int) -> bool:
    try:
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                if os.readlink(str(fd)).startswith("/dev/snd"):
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def _find_writer(player_pid: int) -> int | None:
    try:
        stdin_link = os.readlink(f"/proc/{player_pid}/fd/0")
    except OSError:
        return None
    m = re.search(r"pipe:\[(\d+)\]", stdin_link)
    if not m:
        return None
    inode = m.group(1)
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or p.name == str(player_pid):
            continue
        try:
            for fd in (p / "fd").iterdir():
                if not os.access(str(fd), os.W_OK):
                    continue
                try:
                    if f"pipe:[{inode}]" in os.readlink(str(fd)):
                        return int(p.name)
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return None


async def _send(cmd: str, pid: int) -> bool:
    # 1. Try asyncio stdin stream (gateway-launched session)
    writer = _LAUNCHED.get(pid)
    if writer:
        try:
            writer.write((cmd + "\n").encode())
            await writer.drain()
            return True
        except Exception:
            _LAUNCHED.pop(pid, None)

    # 2. Fall back to proc pipe scanning
    writer_pid = _find_writer(pid)
    if writer_pid is None:
        return False
    fd_dir = Path(f"/proc/{writer_pid}/fd")
    try:
        for fd_path in fd_dir.iterdir():
            if not os.access(str(fd_path), os.W_OK):
                continue
            try:
                if not os.readlink(str(fd_path)).startswith("pipe:"):
                    continue
                with open(str(fd_path), "w") as f:
                    f.write(cmd + "\n")
                    f.flush()
                return True
            except OSError:
                continue
    except OSError:
        pass
    return False


# ── resolve session ───────────────────────────────────────────────────────────

def _resolve_pid(pid: int | None) -> int | None:
    sessions = _all_sessions()
    if pid is not None:
        return pid if any(s["pid"] == pid for s in sessions) else None
    # default: first controllable
    for s in sessions:
        if s["controllable"]:
            return s["pid"]
    return None


# ── public API ────────────────────────────────────────────────────────────────

async def list_sessions() -> list[dict]:
    return _all_sessions()


async def get_status(pid: int | None = None) -> dict:
    sessions = _all_sessions()
    if pid is not None:
        s = next((s for s in sessions if s["pid"] == pid), None)
        return s or {"running": False, "pid": pid}
    return {"sessions": sessions, "count": len(sessions)}


async def _cmd(cmd: str, pid: int | None = None) -> dict:
    sessions = _all_sessions()
    if pid is not None:
        session = next((s for s in sessions if s["pid"] == pid), None)
        if session is None:
            return {"ok": False, "error": f"no media session with pid {pid}"}
    else:
        session = next((s for s in sessions if s["controllable"]), None)
        if session is None:
            return {"ok": False, "error": "no controllable media session found"}
    target = session["pid"]
    ok = await _send(cmd, target)
    if not ok:
        if (session.get("has_listen")
                and target not in _LAUNCHED
                and not session.get("writer_pid")):
            return {"ok": False, "pid": target,
                    "error": "session pipe closed — kill and relaunch via app"}
        return {"ok": False, "pid": target, "error": "command send failed"}
    return {"ok": True, "pid": target}


async def play(pid: int | None = None)  -> dict: return await _cmd("play",  pid)
async def pause(pid: int | None = None) -> dict: return await _cmd("pause", pid)
async def stop(pid: int | None = None)  -> dict: return await _cmd("stop",  pid)

async def set_volume(v: float, pid: int | None = None) -> dict:
    return await _cmd(f"volume {max(0.0, min(1.0, v)):.2f}", pid)

async def seek(seconds: float, pid: int | None = None) -> dict:
    return await _cmd(f"seek {int(seconds)}", pid)

async def open_file(path: str, pid: int | None = None) -> dict:
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"file not found: {path}"}

    if pid is not None:
        # Caller wants to replace the file in a specific running session via stdin
        result = await _cmd(f"open {p}", pid)
        if result.get("ok"):
            _record_played(path)
        return result

    # No target session — launch a fresh window via yay-media-open
    env = {**os.environ, "DISPLAY": _DISPLAY}
    proc = await asyncio.create_subprocess_exec(
        "yay-media-open", str(p), env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _record_played(path)
    return {"ok": True, "pid": proc.pid, "path": path}


_HISTORY_FILE = Path.home() / ".config" / "trixie-gateway" / "media-history.json"
_HISTORY_MAX  = 50


def _read_history() -> list[dict]:
    try:
        import json
        return json.loads(_HISTORY_FILE.read_text())
    except Exception:
        return []


def _write_history(entries: list[dict]):
    import json
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def _record_played(path: str):
    import time
    p       = Path(path)
    entries = [e for e in _read_history() if e.get("path") != path]
    try:
        stat = p.stat()
        size = stat.st_size
    except OSError:
        size = 0
    entries.insert(0, {
        "path":      path,
        "name":      p.name,
        "size":      size,
        "played_at": int(time.time()),
    })
    _write_history(entries[:_HISTORY_MAX])


async def list_recent() -> list[dict]:
    history = _read_history()
    # Filter out paths that no longer exist
    valid   = [e for e in history if Path(e["path"]).exists()]
    if len(valid) != len(history):
        _write_history(valid)
    # If history empty, fall back to directory scan so Library isn't blank
    if not valid:
        files = []
        for d in [_MEDIA_DIR, Path.home() / "Music", Path.home() / "Videos"]:
            if not d.exists():
                continue
            for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.suffix.lower() in _MEDIA_EXTS:
                    stat = f.stat()
                    files.append({
                        "path":      str(f),
                        "name":      f.name,
                        "size":      stat.st_size,
                        "played_at": int(stat.st_mtime),
                    })
        return files[:_HISTORY_MAX]
    return valid


async def clear_recent() -> dict:
    _write_history([])
    return {"ok": True}


async def browse(path: str | None = None) -> dict:
    target = Path(path) if path else Path.home()
    if not target.exists():
        return {"error": f"not found: {target}"}
    if not target.is_dir():
        return {"error": f"not a directory: {target}"}

    dirs, files = [], []
    try:
        for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if item.name.startswith("."):
                continue
            try:
                if item.is_dir() and os.access(str(item), os.R_OK):
                    dirs.append({"name": item.name, "path": str(item), "type": "dir"})
                elif item.is_file() and item.suffix.lower() in _MEDIA_EXTS:
                    stat = item.stat()
                    files.append({
                        "name": item.name,
                        "path": str(item),
                        "type": "file",
                        "size": stat.st_size,
                    })
            except OSError:
                continue
    except PermissionError as e:
        return {"error": str(e)}

    parent = str(target.parent) if target != Path(target.root) else None
    return {
        "path":    str(target),
        "parent":  parent,
        "entries": dirs + files,
    }


async def kill_session(pid: int) -> dict:
    import signal as _signal
    sessions = _all_sessions()
    if not any(s["pid"] == pid for s in sessions):
        return {"ok": False, "error": f"no media session with pid {pid}"}
    writer = _LAUNCHED.pop(pid, None)
    if writer:
        try:
            writer.close()
        except Exception:
            pass
    try:
        os.kill(pid, _signal.SIGTERM)
        return {"ok": True, "pid": pid}
    except ProcessLookupError:
        return {"ok": True, "pid": pid}   # already gone
    except PermissionError as e:
        return {"ok": False, "error": str(e)}


async def launch_source(source: str, path: str | None = None) -> dict:
    env = {**os.environ, "DISPLAY": _DISPLAY}

    if source == "file" and path:
        # yay-media-open manages its own stdin pipe
        proc = await asyncio.create_subprocess_exec(
            "yay-media-open", path, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _record_played(path)
        return {"ok": True, "source": "file", "path": path, "pid": proc.pid}

    # For live sources, hold the stdin pipe ourselves
    cmd = ["yay", "--media", "--media-controls", "--editable",
           "--listen", "--width=700", "--height=480"]
    if source == "screen":
        cmd += ["--media-device=screen", "--title=Screen Capture"]
    elif source == "webcam":
        cmd += [f"--media-device={path or '/dev/video0'}", "--title=Webcam"]
    elif source == "adb":
        cmd += ["--media-device=adb", "--title=Android Screen (ADB)"]
    elif source == "ym":
        # bare yay-media-open: opens default media window, no device
        proc = await asyncio.create_subprocess_exec(
            "yay-media-open",
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if proc.stdin:
            _LAUNCHED[proc.pid] = proc.stdin
        return {"ok": True, "source": "ym", "pid": proc.pid}
    else:
        return {"ok": False, "error": f"unknown source: {source}"}

    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if proc.stdin:
        _LAUNCHED[proc.pid] = proc.stdin
    return {"ok": True, "source": source, "pid": proc.pid}
