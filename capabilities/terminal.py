"""Terminal capability — execute shell commands on this host."""
import asyncio
import os

_DISPLAY = os.environ.get("DISPLAY", ":0")


async def exec_cmd(cmd: str, cwd: str | None = None, timeout: int = 30) -> dict:
    env = {**os.environ, "DISPLAY": _DISPLAY}
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return {
                "ok":         False,
                "returncode": -1,
                "stdout":     "",
                "stderr":     f"timeout after {timeout}s",
            }
        return {
            "ok":         proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout":     stdout.decode(errors="replace"),
            "stderr":     stderr.decode(errors="replace"),
        }
    except Exception as e:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(e)}
