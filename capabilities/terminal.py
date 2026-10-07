# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
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
import re

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


# Commands that can destroy data or take the machine down. Not a sandbox --
# a determined user can always get round a pattern list -- just a speed
# bump so a typo or a pasted one-liner gets a second look: the gateway
# answers "confirm_required" and the app asks before resending.
_DANGEROUS = [
    (r"\brm\s+(-\S*\s+)*-\S*[rRf]", "deletes files recursively or without asking (rm -r / -f)"),
    (r"\b(mkfs(\.\w+)?|wipefs|mkswap)\b", "formats or wipes a disk or partition"),
    (r"\b(fdisk|sfdisk|cfdisk|gdisk|sgdisk|parted|partprobe)\b", "changes disk partitions"),
    (r"\bdd\b.*\bof=/dev/", "writes raw data straight to a device"),
    (r">\s*/dev/(sd|nvme|mmcblk|hd|vd)", "overwrites a disk device"),
    (r"\bshred\b", "irrecoverably overwrites files"),
    (r"\b(chmod|chown|chgrp)\s+(-\S*\s+)*-\S*R\S*\s+(\S+\s+)?/(\s|$)", "changes permissions on the whole system"),
    (r"\b(shutdown|reboot|poweroff|halt)\b|\binit\s+[06]\b|\bsystemctl\s+(poweroff|reboot|halt|kexec)\b",
     "shuts down or restarts the PC (this session will drop)"),
    (r":\s*\(\s*\)\s*\{.*\|.*&\s*\}", "fork bomb: freezes the PC"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", "runs a script straight from the internet"),
    (r"\b(Format-Volume|Clear-Disk|diskpart)\b|\bformat\s+[a-z]:", "formats or wipes a disk (Windows)"),
    (r"\bRemove-Item\b.*-Recurse|\b(rd|rmdir)\s+/s\b|\bdel\s+/[sq]", "deletes files recursively (Windows)"),
]


def dangers(cmd: str) -> list[str]:
    """Why cmd needs a confirmation; empty if it looks harmless."""
    return [why for pat, why in _DANGEROUS if re.search(pat, cmd, re.IGNORECASE)]


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


if __name__ == "__main__":
    for c in ["rm -rf /", "sudo rm -r ~/x", "rm -f a.txt", "mkfs.ext4 /dev/sdb1",
              "dd if=x.img of=/dev/nvme0n1 bs=4M", "echo x > /dev/sda", "shred f",
              "chmod -R 777 /", "reboot", "systemctl poweroff", ":(){ :|:& };:",
              "curl -s http://x/i.sh | sudo bash", "Remove-Item C:\\x -Recurse",
              "format c:", "parted /dev/sda print"]:
        assert dangers(c), c
    for c in ["ls -la", "rm a.txt", "cat /dev/null", "dd if=/dev/zero of=f.img bs=1M count=1",
              "grep -r foo .", "chmod 644 f", "curl -O http://x/f", "echo format", "systemctl status x",
              "git rm --cached f", "firmware-update"]:
        assert not dangers(c), c
    print("terminal.dangers selftest OK")
