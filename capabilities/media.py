"""Media capability — yay --media (pipe IPC), Chameleon-Media-Center /
MPV-Media-Center (mpv --input-ipc-server JSON socket), and VLC (detection
only — no control channel confirmed available) — multi-session, multi-
backend.

Backend note: yay --media sessions have no player-reported position/
duration/volume/play-state at all (gstreamer side never exposed it over
the pipe protocol) — those fields are always None for "yay_media" sessions
and always populated for "mpv_ipc" ones, where mpv's own IPC socket gives
real values. Callers should treat absence as "unknown", not "zero".
"""

import asyncio
import os
import re
from pathlib import Path

from . import mpv_ipc

_MEDIA_DIR = Path("/mnt/sda2/YaYOS/layers/base/root/media")
if not _MEDIA_DIR.is_dir():
    # Union-fs stacking fallback: Fatdog64-derived distros (this box
    # included) mount under /aufs, standard Puppy Linux under /initrd —
    # no /aufs directory exists there at all. Try both.
    for _root in ("/aufs/devbase", "/initrd/devbase"):
        _candidate = Path(_root) / "YaYOS" / "layers" / "base" / "root" / "media"
        if _candidate.is_dir():
            _MEDIA_DIR = _candidate
            break
_DISPLAY    = os.environ.get("DISPLAY", ":0")
_MEDIA_EXTS = {".mp3", ".mp4", ".wav", ".ogg", ".flac", ".mkv",
               ".avi", ".webm", ".m4a", ".aac", ".opus"}

# stdin streams for sessions launched by this gateway process
_LAUNCHED: dict[int, asyncio.StreamWriter] = {}


# ── proc scanning ─────────────────────────────────────────────────────────────

async def _all_sessions() -> list[dict]:
    sessions = _yay_media_sessions()
    sessions += await _mpv_ipc_sessions()
    sessions += _vlc_sessions()
    return sessions


def _yay_media_sessions() -> list[dict]:
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
            "backend":      "yay_media",
            "title":        title,
            "source":       source,
            "device":       device,
            "current_file": current_file,
            "has_listen":   has_listen,
            "controllable": controllable,
            "writer_pid":   writer_pid,
            "audio_active": _audio_active(pid),
            "playing":      None,
            "position":     None,
            "duration":     None,
            "volume":       None,
        })
    return sessions


def _mpv_ipc_socket_for_pid(pid: int) -> str | None:
    """Re-derive the --input-ipc-server socket path from /proc each call
    rather than caching — matches this module's existing convention
    (_find_writer also re-scans /proc fresh every time) and stays correct
    across mpv restarts without needing invalidation logic."""
    try:
        raw = (Path(f"/proc/{pid}/cmdline")
               .read_bytes().replace(b"\x00", b" ").decode(errors="replace"))
    except OSError:
        return None
    if "mpv" not in raw:
        return None
    m = re.search(r"--input-ipc-server=(\S+)", raw)
    return m.group(1) if m else None


async def _mpv_ipc_sessions() -> list[dict]:
    sessions = []
    for p in sorted(Path("/proc").iterdir(), key=lambda x: x.name):
        if not p.name.isdigit():
            continue
        pid = int(p.name)
        try:
            comm = (p / "comm").read_text().strip()
        except OSError:
            continue
        if comm != "mpv":
            continue
        socket_path = _mpv_ipc_socket_for_pid(pid)
        if not socket_path:
            continue
        status = await mpv_ipc.get_status(socket_path)
        if status is None:
            continue  # mpv process exists but IPC socket isn't answering

        path = status.get("path")
        title = status.get("media_title") or (Path(path).name if path else None) or "mpv"
        # Identify the launching app (Chameleon-Media-Center.py, MPV-Media-
        # Center-*.py, or a bare manual mpv) from the parent process, purely
        # for a friendlier "source" label — control only ever needs the pid.
        source = "mpv"
        try:
            ppid_line = (Path(f"/proc/{pid}/status").read_text()
                         .splitlines())
            ppid = next((int(l.split()[1]) for l in ppid_line if l.startswith("PPid:")), None)
            if ppid:
                parent_raw = (Path(f"/proc/{ppid}/cmdline")
                              .read_bytes().replace(b"\x00", b" ").decode(errors="replace"))
                if "Chameleon-Media-Center" in parent_raw:
                    source = "chameleon-media-center"
                elif "MPV-Media-Center" in parent_raw:
                    source = "mpv-media-center"
        except OSError:
            pass

        sessions.append({
            "pid":          pid,
            "backend":      "mpv_ipc",
            "title":        title,
            "source":       source,
            "device":       None,
            "current_file": path,
            "has_listen":   True,
            "controllable": True,
            "writer_pid":   None,
            "audio_active": not bool(status.get("mute")) and not bool(status.get("pause")),
            "playing":      (not status.get("pause")) if not status.get("idle_active") else False,
            "position":     status.get("time_pos"),
            "duration":     status.get("duration"),
            # mpv's volume property is 0-100 (>100 possible with boost);
            # normalise to the same 0.0-1.0 scale set_volume() already uses
            # for yay_media sessions so callers don't need to know the
            # backend to interpret this field.
            "volume":       (status.get("volume") / 100.0) if status.get("volume") is not None else None,
        })
    return sessions


def _vlc_sessions() -> list[dict]:
    """Detection only — VLC isn't installed on this box as of writing so
    this is unverified against a live instance. No control channel is
    wired up (would need --extraintf rc/http enabled at launch, or the
    D-Bus MPRIS2 interface VLC exposes by default on most distro builds —
    neither confirmed available here). Sessions show up as not
    controllable until one of those is actually implemented and tested."""
    sessions = []
    for p in sorted(Path("/proc").iterdir(), key=lambda x: x.name):
        if not p.name.isdigit():
            continue
        pid = int(p.name)
        try:
            comm = (p / "comm").read_text().strip()
        except OSError:
            continue
        if comm != "vlc":
            continue
        try:
            raw = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            raw = ""
        args = [a for a in raw.split(" ") if a and not a.startswith("-")]
        current_file = args[-1] if len(args) > 1 else None
        title = Path(current_file).name if current_file else "VLC"

        sessions.append({
            "pid":          pid,
            "backend":      "vlc",
            "title":        title,
            "source":       "vlc",
            "device":       None,
            "current_file": current_file,
            "has_listen":   False,
            "controllable": False,
            "writer_pid":   None,
            "audio_active": _audio_active(pid),
            "playing":      None,
            "position":     None,
            "duration":     None,
            "volume":       None,
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


# ── public API ────────────────────────────────────────────────────────────────

async def list_sessions() -> list[dict]:
    return await _all_sessions()


async def get_status(pid: int | None = None) -> dict:
    sessions = await _all_sessions()
    if pid is not None:
        s = next((s for s in sessions if s["pid"] == pid), None)
        return s or {"running": False, "pid": pid}
    return {"sessions": sessions, "count": len(sessions)}


async def _find_session(pid: int | None) -> dict | None:
    sessions = await _all_sessions()
    if pid is not None:
        return next((s for s in sessions if s["pid"] == pid), None)
    return next((s for s in sessions if s["controllable"]), None)


async def _cmd(cmd: str, pid: int | None = None) -> dict:
    """yay_media (gstreamer) control path — pipe-based text commands."""
    session = await _find_session(pid)
    if session is None:
        return {"ok": False, "error": f"no media session with pid {pid}"} \
            if pid is not None else {"ok": False, "error": "no controllable media session found"}
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


async def _mpv_cmd(action: str, pid: int, **kwargs) -> dict:
    """mpv_ipc (CMC / MPV-Media-Center) control path — JSON socket commands."""
    socket_path = _mpv_ipc_socket_for_pid(pid)
    if socket_path is None:
        return {"ok": False, "pid": pid, "error": "mpv IPC socket not found for this pid"}
    if action == "play":
        ok = await mpv_ipc.set_property(socket_path, "pause", False)
    elif action == "pause":
        ok = await mpv_ipc.set_property(socket_path, "pause", True)
    elif action == "stop":
        ok = await mpv_ipc.run_command(socket_path, "stop")
    elif action == "volume":
        ok = await mpv_ipc.set_property(socket_path, "volume", kwargs["value"] * 100.0)
    elif action == "seek":
        ok = await mpv_ipc.run_command(socket_path, "seek", kwargs["value"], "absolute")
    elif action == "open":
        ok = await mpv_ipc.run_command(socket_path, "loadfile", kwargs["path"], "replace")
    else:
        return {"ok": False, "pid": pid, "error": f"unknown mpv action: {action}"}
    if not ok:
        return {"ok": False, "pid": pid, "error": "mpv IPC command failed"}
    return {"ok": True, "pid": pid}


async def _dispatch(pid: int | None, yay_cmd: str, mpv_action: str, **mpv_kwargs) -> dict:
    """Route a control action to the right backend based on which kind of
    session `pid` (or the default controllable session) actually is."""
    session = await _find_session(pid)
    if session is None:
        return {"ok": False, "error": f"no media session with pid {pid}"} \
            if pid is not None else {"ok": False, "error": "no controllable media session found"}
    if session["backend"] == "mpv_ipc":
        return await _mpv_cmd(mpv_action, session["pid"], **mpv_kwargs)
    if session["backend"] == "yay_media":
        return await _cmd(yay_cmd, session["pid"])
    return {"ok": False, "pid": session["pid"],
            "error": f"backend '{session['backend']}' has no control channel wired up yet"}


async def play(pid: int | None = None)  -> dict: return await _dispatch(pid, "play",  "play")
async def pause(pid: int | None = None) -> dict: return await _dispatch(pid, "pause", "pause")
async def stop(pid: int | None = None)  -> dict: return await _dispatch(pid, "stop",  "stop")

async def set_volume(v: float, pid: int | None = None) -> dict:
    v = max(0.0, min(1.0, v))
    return await _dispatch(pid, f"volume {v:.2f}", "volume", value=v)

async def seek(seconds: float, pid: int | None = None) -> dict:
    return await _dispatch(pid, f"seek {int(seconds)}", "seek", value=seconds)

async def open_file(path: str, pid: int | None = None) -> dict:
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"file not found: {path}"}

    if pid is not None:
        # Caller wants to replace the file in a specific running session
        result = await _dispatch(pid, f"open {p}", "open", path=str(p))
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


def _parent_pid(pid: int) -> int | None:
    try:
        lines = Path(f"/proc/{pid}/status").read_text().splitlines()
        return next((int(l.split()[1]) for l in lines if l.startswith("PPid:")), None)
    except OSError:
        return None


async def close_app(pid: int) -> dict:
    """Close the whole GUI app behind a media session, not just its mpv
    process. For Chameleon-Media-Center / MPV-Media-Center, mpv is embedded
    inside the app's own window — quitting mpv alone used to leave the app
    running with a dead, unrespawned player and nothing further playable
    from either the app or the gateway until it was restarted by hand.
    Closing the parent app is the only control action that leaves things
    in a working state, so this is what "kill"/"close" now does for those
    sources; every other session type has no separate GUI shell to
    preserve, so closing the player IS closing the app there."""
    import signal as _signal
    session = next((s for s in await _all_sessions() if s["pid"] == pid), None)
    if session is None:
        return {"ok": False, "error": f"no media session with pid {pid}"}

    if session["backend"] != "mpv_ipc" or session["source"] not in (
            "chameleon-media-center", "mpv-media-center"):
        return await kill_session(pid)

    ppid = _parent_pid(pid)
    if ppid is None:
        return {"ok": False, "error": "could not resolve parent app pid"}
    try:
        os.kill(ppid, _signal.SIGTERM)
        return {"ok": True, "pid": ppid, "closed": "app"}
    except ProcessLookupError:
        return {"ok": True, "pid": ppid, "closed": "app"}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}


async def kill_session(pid: int) -> dict:
    import signal as _signal
    session = next((s for s in await _all_sessions() if s["pid"] == pid), None)
    if session is None:
        return {"ok": False, "error": f"no media session with pid {pid}"}

    # CMC / MPV-Media-Center: mpv is embedded in the app's own window, so
    # IPC-quitting it alone leaves the app half-broken (see close_app's
    # docstring). Always close the whole app for these instead — the only
    # two outcomes are "the UI closes" or "nothing happens", never a
    # half-dead embedded player.
    if session["backend"] == "mpv_ipc" and session["source"] in (
            "chameleon-media-center", "mpv-media-center"):
        return await close_app(pid)

    # mpv IPC has its own graceful "quit" command — prefer it over SIGTERM
    # so mpv can clean up (release the audio device, close cleanly) instead
    # of just dying. This path is only reached for bare/manually-launched
    # mpv now, where there's no separate app window to worry about.
    if session["backend"] == "mpv_ipc":
        socket_path = _mpv_ipc_socket_for_pid(pid)
        if socket_path and await mpv_ipc.run_command(socket_path, "quit"):
            return {"ok": True, "pid": pid}
        # fall through to SIGTERM if IPC quit didn't work

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
