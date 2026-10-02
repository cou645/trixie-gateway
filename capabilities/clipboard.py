# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# All rights reserved.
#
# This software is proprietary. No licence is granted to use, copy, modify,
# distribute, sublicense or sell it, in whole or in part, without the prior
# written permission of the copyright holder. Licensing: cou645@gmail.com
#
# Copyright is held by the author personally. CHAMELEON-AI-AGENT LTD has not
# paid for this software or for the hours spent writing it, and holds no
# rights in it.
# Donations via PayPal: cou645@gmail.com
"""Clipboard capability — read/write the host's system clipboard.

Tries X11's xclip first (this gateway runs under DISPLAY=:0, see
trixie-gateway.service), then xsel, then Wayland's wl-clipboard for
portability to other gateway installs.
"""

import asyncio

_READ_CMDS = (
    ["xclip", "-selection", "clipboard", "-o"],
    ["xsel", "--clipboard", "--output"],
    ["wl-paste", "--no-newline"],
)
_WRITE_CMDS = (
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
    ["wl-copy"],
)


async def _run_capture(cmd: list[str]):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out, err


async def _run_feed(cmd: list[str], input_bytes: bytes):
    # xclip (and, for the same X11-selection-ownership reason, xsel)
    # forks into a background process that keeps serving the selection
    # after this call returns — with stdout/stderr as PIPEs, that child
    # inherits and holds them open, so communicate() would hang forever
    # waiting for a close that never comes. DEVNULL sidesteps it: we only
    # need this call's own exit code, not its (empty) output.
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate(input_bytes)
    return proc.returncode


async def get() -> dict:
    for cmd in _READ_CMDS:
        try:
            rc, out, err = await _run_capture(cmd)
        except FileNotFoundError:
            continue
        if rc == 0:
            return {"ok": True, "text": out.decode(errors="replace")}
    return {"ok": False, "error": "no clipboard tool found (wl-clipboard/xclip/xsel)"}


async def set_text(text: str) -> dict:
    for cmd in _WRITE_CMDS:
        try:
            rc = await _run_feed(cmd, text.encode())
        except FileNotFoundError:
            continue
        if rc == 0:
            return {"ok": True}
    return {"ok": False, "error": "no clipboard tool found (wl-clipboard/xclip/xsel)"}


# ── Shared clipboard history (PC as the hub) ──────────────────────────────────
# Every device's pushes and the PC's own copies, newest first, kept across
# gateway restarts. Locked items are never trimmed or deleted; the rest is
# capped at MAX_ITEMS. The same text again moves to the top (keeps its lock)
# instead of duplicating. The file can hold anything copied (passwords
# too), so it is owner-only (0600).

import json
import os
import time
import uuid

HISTORY = os.path.expanduser("~/.config/trixie-gateway/clipboard.json")
SAVE_DIR = os.path.expanduser("~/Documents/TrXi-clipboard")
MAX_ITEMS = 50


class History:
    def __init__(self, path=HISTORY, max_items=MAX_ITEMS):
        self.path, self.max_items = path, max_items
        try:
            with open(path, encoding="utf-8") as f:
                self.items = [i for i in json.load(f) if i.get("text")]
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def latest_text(self):
        return self.items[0]["text"] if self.items else None

    def add(self, text, device, key=None, to=None):
        old = next((i for i in self.items if i["text"] == text), None)
        if old:
            self.items.remove(old)
        item = {"id": old["id"] if old else uuid.uuid4().hex[:12],
                "device": device, "text": text, "ts": time.time(),
                "locked": bool(old and old.get("locked"))}
        if key:                       # who, whatever they get named later
            item["key"] = key
        if to:                        # only these devices / @groups
            item["to"] = list(to)
        self.items.insert(0, item)
        unlocked = [i for i in self.items if not i.get("locked")]
        for extra in unlocked[self.max_items:]:
            self.items.remove(extra)
        self._save()
        return item

    def get(self, item_id):
        return next((i for i in self.items if i["id"] == item_id), None)

    def delete(self, item_id):
        """'ok' | 'locked' | 'missing'"""
        item = self.get(item_id)
        if item is None:
            return "missing"
        if item.get("locked"):
            return "locked"
        self.items.remove(item)
        self._save()
        return "ok"

    def lock(self, item_id, locked=True):
        item = self.get(item_id)
        if item is None:
            return None
        item["locked"] = bool(locked)
        self._save()
        return item

    def save_file(self, item_id, folder=SAVE_DIR):
        """Write the item to a text file on the PC; returns its path."""
        item = self.get(item_id)
        if item is None:
            return None
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(item["ts"]))
        dev = "".join(c if c.isalnum() or c in "-_" else "_"
                      for c in item["device"])[:40]
        path = os.path.join(folder, "%s_%s.txt" % (stamp, dev))
        with open(path, "w", encoding="utf-8") as f:
            f.write(item["text"])
        return path


def _selftest():
    import tempfile
    d = tempfile.mkdtemp()
    h = History(os.path.join(d, "c.json"), max_items=3)
    a = h.add("one", "phoneA")
    h.add("two", "phoneB")
    assert [i["text"] for i in h.items] == ["two", "one"]
    h.lock(a["id"])
    for t in ("three", "four", "five"):
        h.add(t, "PC")
    assert [i["text"] for i in h.items] == ["five", "four", "three", "one"]
    assert h.delete(a["id"]) == "locked" and h.get(a["id"])
    again = h.add("one", "phoneC")             # same text: to the top
    assert again["id"] == a["id"] and again["locked"] and \
        [i["text"] for i in h.items][0] == "one" and len(h.items) == 4
    assert oct(os.stat(h.path).st_mode)[-3:] == "600"
    assert [i["text"] for i in History(h.path).items] == \
        [i["text"] for i in h.items]               # survives a restart
    p = h.save_file(a["id"], os.path.join(d, "saved"))
    assert open(p).read() == "one" and "phoneC" in p
    h.lock(a["id"], False)
    assert h.delete(a["id"]) == "ok" and h.delete("nope") == "missing"
    print("clipboard history selftest OK")


if __name__ == "__main__":
    _selftest()
