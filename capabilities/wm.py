"""Window manager capability — EWMH control via wmctrl + xdotool (X11)."""

import asyncio
import os
import re

_DISPLAY = os.environ.get("DISPLAY", ":0")


async def _run(*args, timeout: int = 5) -> tuple[int, str, str]:
    env = {**os.environ, "DISPLAY": _DISPLAY}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        return 1, "", "timeout"
    except FileNotFoundError as e:
        return 1, "", str(e)


# ── windows ───────────────────────────────────────────────────────────────────

async def list_windows() -> list[dict]:
    rc, out, _ = await _run("wmctrl", "-l", "-G")
    if rc != 0:
        return []
    windows = []
    for line in out.splitlines():
        parts = line.split(None, 9)
        if len(parts) < 9:
            continue
        wid, desktop, px, py, pw, ph, host, *title_parts = parts
        title = " ".join(title_parts).strip() if title_parts else ""
        if desktop == "-1":   # sticky/panel
            continue
        windows.append({
            "id":      int(wid, 16),
            "id_hex":  wid,
            "desktop": int(desktop),
            "x": int(px), "y": int(py),
            "width": int(pw), "height": int(ph),
            "title": title,
        })
    return windows


async def get_active_window() -> dict | None:
    rc, out, _ = await _run("xdotool", "getactivewindow")
    if rc != 0 or not out.strip():
        return None
    wid = int(out.strip())
    return await get_window(wid)


async def get_window(wid: int) -> dict | None:
    wins = await list_windows()
    for w in wins:
        if w["id"] == wid:
            return w
    return None


async def find_windows(title: str = "", wm_class: str = "") -> list[dict]:
    wins = await list_windows()
    results = []
    for w in wins:
        if title    and title.lower()    not in w["title"].lower():
            continue
        if wm_class and wm_class.lower() not in w.get("class", "").lower():
            continue
        results.append(w)
    return results


# ── window actions ────────────────────────────────────────────────────────────

async def focus_window(wid: int) -> dict:
    rc, _, err = await _run("wmctrl", "-i", "-a", hex(wid))
    return {"ok": rc == 0, "error": err.strip() or None}


async def close_window(wid: int) -> dict:
    rc, _, err = await _run("wmctrl", "-i", "-c", hex(wid))
    return {"ok": rc == 0, "error": err.strip() or None}


async def move_resize_window(wid: int, x: int, y: int,
                             width: int, height: int) -> dict:
    # wmctrl -i -r <id> -e <gravity,x,y,w,h>
    spec = f"0,{x},{y},{width},{height}"
    rc, _, err = await _run("wmctrl", "-i", "-r", hex(wid), "-e", spec)
    return {"ok": rc == 0, "error": err.strip() or None}


async def maximize_window(wid: int) -> dict:
    rc, _, err = await _run(
        "wmctrl", "-i", "-r", hex(wid),
        "-b", "add,maximized_vert,maximized_horz",
    )
    return {"ok": rc == 0, "error": err.strip() or None}


async def restore_window(wid: int) -> dict:
    rc, _, err = await _run(
        "wmctrl", "-i", "-r", hex(wid),
        "-b", "remove,maximized_vert,maximized_horz",
    )
    return {"ok": rc == 0, "error": err.strip() or None}


async def minimize_window(wid: int) -> dict:
    rc, _, err = await _run("xdotool", "windowminimize", str(wid))
    return {"ok": rc == 0, "error": err.strip() or None}


async def fullscreen_window(wid: int, enable: bool = True) -> dict:
    action = "add" if enable else "remove"
    rc, _, err = await _run(
        "wmctrl", "-i", "-r", hex(wid), "-b", f"{action},fullscreen",
    )
    return {"ok": rc == 0, "error": err.strip() or None}


# ── desktops ──────────────────────────────────────────────────────────────────

async def list_desktops() -> list[dict]:
    rc, out, _ = await _run("wmctrl", "-d")
    if rc != 0:
        return []
    desktops = []
    for line in out.splitlines():
        parts = line.split(None, 8)
        if not parts:
            continue
        desktops.append({
            "id":      int(parts[0]),
            "current": parts[1] == "*" if len(parts) > 1 else False,
            "name":    parts[-1] if parts else "",
        })
    return desktops


async def get_current_desktop() -> int:
    rc, out, _ = await _run("wmctrl", "-d")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] == "*":
            return int(parts[0])
    return 0


async def set_desktop(n: int) -> dict:
    rc, _, err = await _run("wmctrl", "-s", str(n))
    return {"ok": rc == 0, "error": err.strip() or None}


async def move_window_to_desktop(wid: int, desktop: int) -> dict:
    rc, _, err = await _run("wmctrl", "-i", "-r", hex(wid), "-t", str(desktop))
    return {"ok": rc == 0, "error": err.strip() or None}


# ── tile ──────────────────────────────────────────────────────────────────────

async def tile_windows(cols: int = 2) -> dict:
    """Simple tiling: arrange visible windows in a grid."""
    wins = [w for w in await list_windows() if w["desktop"] >= 0]
    if not wins:
        return {"ok": False, "error": "no windows to tile"}

    # Get screen size from wmctrl -d
    rc, out, _ = await _run("wmctrl", "-d")
    screen_w, screen_h = 1920, 1080
    for line in out.splitlines():
        m = re.search(r"DG:\s*(\d+)x(\d+)", line)
        if m:
            screen_w, screen_h = int(m.group(1)), int(m.group(2))

    rows  = -(-len(wins) // cols)  # ceiling div
    cell_w = screen_w // cols
    cell_h = screen_h // rows

    for i, w in enumerate(wins):
        col = i % cols
        row = i // cols
        x = col * cell_w
        y = row * cell_h
        await restore_window(w["id"])
        await move_resize_window(w["id"], x, y, cell_w - 4, cell_h - 4)

    return {"ok": True, "tiled": len(wins), "grid": f"{cols}x{rows}"}
