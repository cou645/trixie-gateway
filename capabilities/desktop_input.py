"""Desktop input capability — inject pointer/keyboard events into X11 via xdotool."""

import asyncio
import os

_DISPLAY = os.environ.get("DISPLAY", ":0")


async def _run(cmd: list[str]) -> bool:
    env = {**os.environ, "DISPLAY": _DISPLAY}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=2)
        return proc.returncode == 0
    except Exception:
        return False


async def _focus_at_pointer() -> None:
    """Pre-focus the window under the pointer before sending button 1 events.

    Openbox (click-to-focus) passively grabs button 1 on every unfocused client
    window. That grab intercepts xdotool/XTEST button 1 events — the WM consumes
    the click for focus and the application never sees it. Button 3 has no such
    grab, which is why right-click always works. By focusing first the grab is
    gone and the subsequent click reaches the app normally.
    """
    env = {**os.environ, "DISPLAY": _DISPLAY}
    try:
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "getmouselocation", "--shell",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=1)
        wid = None
        for line in out.decode().splitlines():
            if line.startswith("WINDOW="):
                wid = line.split("=", 1)[1].strip()
                break
        if wid and wid not in ("0", ""):
            await _run(["xdotool", "windowfocus", "--sync", wid])
    except Exception:
        pass


async def mouse_move(x: int, y: int) -> dict:
    ok = await _run(["xdotool", "mousemove", str(x), str(y)])
    return {"ok": ok, "x": x, "y": y}


async def mouse_click(x: int, y: int, button: int = 1) -> dict:
    await _run(["xdotool", "mousemove", str(x), str(y)])
    await _focus_at_pointer()
    ok = await _run(["xdotool", "click", "--clearmodifiers", str(button)])
    return {"ok": ok, "x": x, "y": y, "button": button}


async def mouse_down(x: int, y: int, button: int = 1) -> dict:
    await _run(["xdotool", "mousemove", str(x), str(y)])
    await _focus_at_pointer()
    ok = await _run(["xdotool", "mousedown", str(button)])
    return {"ok": ok}


async def mouse_up(x: int, y: int, button: int = 1) -> dict:
    ok = await _run(["xdotool", "mousemove", str(x), str(y),
                     "mouseup", str(button)])
    return {"ok": ok}


async def scroll(x: int, y: int, direction: str = "down", amount: int = 3) -> dict:
    # xdotool button 4=scroll-up, 5=scroll-down
    btn = "4" if direction == "up" else "5"
    cmds = ["xdotool", "mousemove", str(x), str(y)]
    for _ in range(amount):
        cmds += ["click", btn]
    ok = await _run(cmds)
    return {"ok": ok}


async def key_type(text: str) -> dict:
    ok = await _run(["xdotool", "type", "--clearmodifiers", "--", text])
    return {"ok": ok}


async def key_press(key: str) -> dict:
    """key: xdotool key name e.g. 'Return', 'ctrl+c', 'super', 'Escape'."""
    ok = await _run(["xdotool", "key", "--clearmodifiers", key])
    return {"ok": ok}


async def get_display_size() -> dict:
    import re
    # Try xdpyinfo first
    try:
        proc = await asyncio.create_subprocess_exec(
            "xdpyinfo", "-display", _DISPLAY,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        m = re.search(r"dimensions:\s+(\d+)x(\d+)\s+pixels",
                      stdout.decode(errors="replace"))
        if m:
            return {"ok": True, "width": int(m.group(1)), "height": int(m.group(2))}
    except Exception:
        pass
    # Fallback: xrandr
    try:
        proc = await asyncio.create_subprocess_exec(
            "xrandr", "--display", _DISPLAY,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        m = re.search(r"current (\d+) x (\d+)", stdout.decode(errors="replace"))
        if m:
            return {"ok": True, "width": int(m.group(1)), "height": int(m.group(2))}
    except Exception:
        pass
    return {"ok": False, "width": 1280, "height": 1024}


async def check_xdotool() -> dict:
    ok = await _run(["xdotool", "version"])
    return {"available": ok}
