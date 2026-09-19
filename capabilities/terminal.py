"""Terminal capability — execute shell commands on this host.

This is a one-shot exec per call, not a persistent shell session: no
PTY is attached, cwd doesn't carry over between calls (the caller must
resend it), and nothing that needs a real interactive terminal --
`claude`, `kimi`'s interactive mode, a plain shell you could `cd`
around in -- can work through it. That's a structural limit, not a bug;
fixing it for real needs a PTY-backed session capability instead. What
IS fixed here is making one-shot use actually usable: a sensible
default cwd instead of `/` (the gateway's own default working dir),
and a clear hint instead of claude/kimi's confusing raw error when
called bare with no prompt.
"""
import asyncio
import os

from platforms import backend as _backend

_DISPLAY = os.environ.get("DISPLAY", ":0")
_DEFAULT_CWD = os.path.expanduser("~")  # /root, not / -- see module docstring

# claude/kimi launched with no arguments try to start their normal
# interactive chat UI, which needs a real TTY. This exec API has no PTY,
# so the CLI itself falls back to non-interactive --print mode and then
# fails with "Input must be provided either through stdin or as a prompt
# argument" -- accurate, but opaque if you don't already know why.
# Catching the bare invocation here gives a hint instead of that raw error.
_BARE_INTERACTIVE_HINT = {
    "claude": 'claude needs a prompt here (no interactive terminal available) -- '
              'try: claude --print "your question"',
    "kimi":   'kimi needs a prompt here (no interactive terminal available) -- '
              'try: kimi --print "your question" (check kimi --help for its exact flag)',
}


async def exec_cmd(cmd: str, cwd: str | None = None, timeout: int = 30) -> dict:
    bare = cmd.strip()
    if bare in _BARE_INTERACTIVE_HINT:
        return {"ok": False, "returncode": -1, "stdout": "",
                "stderr": _BARE_INTERACTIVE_HINT[bare]}

    env = {**os.environ, "DISPLAY": _DISPLAY}
    try:
        if _backend:
            # create_subprocess_shell would run cmd through /bin/sh, which does
            # not exist on Windows; the backend picks PowerShell / the login
            # shell instead.
            proc = await asyncio.create_subprocess_exec(
                *_backend.shell_command(cmd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or _backend.default_cwd(),
                env=env,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or _DEFAULT_CWD,
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
