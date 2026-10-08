# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
"""
Semantic journal — typed OS event stream for AI consumption.

Events are appended to ~/.config/trixie-gateway/journal.jsonl (one JSON object per line).
The journal consumer reads from /proc/kmsg or inotify watches on key paths
to auto-generate events. AI queries can filter by event type and time range.

Event schema:
  { "ts": 1234567890.123, "type": "file_write|proc_exit|pkg_install|...",
    "subject": "...", "detail": {...} }
"""

import hashlib
import json
import os
import time
from pathlib import Path


JOURNAL_PATH = Path.home() / ".config" / "trixie-gateway" / "journal.jsonl"

KNOWN_TYPES = {
    "file_write", "file_delete", "proc_start", "proc_exit",
    "pkg_install", "pkg_remove", "layer_mount", "layer_umount",
    "net_connect", "net_disconnect", "wifi_connect", "wifi_disconnect",
    "ai_query", "capability_issue", "boot", "shutdown", "remaster",
    "brightness_change", "volume_change", "mute_change",
    "media_open", "media_source", "media_kill",
    "app_launch", "app_kill",
    "pm",
    "lock", "unlock",
    "firewall_rule_add", "firewall_rule_del", "firewall_policy",
    "firewall_flush", "firewall_preset",
    "bluetooth_power", "bluetooth_connect", "bluetooth_disconnect",
    "bluetooth_pair", "bluetooth_remove",
    "terminal_exec",
    "webrtc_offer", "webrtc_close",
    "device_paired", "clipboard_set", "clipboard_delete", "clipboard_save",
    # security events (ISO 27001 gap G7)
    "auth_refused", "pair_failed", "device_revoked", "terminal_permission",
    "whitelist_change", "clipboard_clear",
}


class SemanticJournal:
    def __init__(self, config: dict):
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._path = Path(config.get("path", JOURNAL_PATH))
        self._max_events = config.get("max_events", 100_000)
        # ISO 27001 gap G7: owner-only file, rotated by size, and each entry
        # carries the hash of the line before it, so an edited or deleted
        # line breaks the chain (verify()).
        self._max_bytes = config.get("max_bytes", 20 * 1024 * 1024)
        self._keep = config.get("keep_files", 5)
        if self._path.exists():
            os.chmod(self._path, 0o600)
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        try:
            with self._path.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                lines = f.read().splitlines()
            return hashlib.sha256(lines[-1]).hexdigest() if lines else ""
        except OSError:
            return ""

    def _rotate(self):
        if not self._path.exists() or self._path.stat().st_size < self._max_bytes:
            return
        for i in range(self._keep - 1, 0, -1):
            older = self._path.with_name(f"{self._path.name}.{i}")
            if older.exists():
                older.replace(self._path.with_name(f"{self._path.name}.{i + 1}"))
        self._path.replace(self._path.with_name(self._path.name + ".1"))

    def append(self, event_type: str, subject: str, detail: dict | None = None):
        assert event_type in KNOWN_TYPES, f"unknown event type: {event_type}"
        entry = {
            "ts": time.time(),
            "type": event_type,
            "subject": subject,
            "detail": detail or {},
            "prev": self._prev,
        }
        self._rotate()
        line = json.dumps(entry)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(line + "\n")
        self._prev = hashlib.sha256(line.encode()).hexdigest()

    def verify(self) -> tuple[bool, int]:
        """(chain intact, number of lines checked) for the current file.
        Lines written before chaining existed carry no "prev" and are skipped."""
        prev, n = None, 0
        with self._path.open("rb") as f:
            for raw in f.read().splitlines():
                n += 1
                try:
                    ev = json.loads(raw)
                except ValueError:
                    return False, n
                if "prev" in ev and prev is not None and ev["prev"] != prev:
                    return False, n
                prev = hashlib.sha256(raw).hexdigest()
        return True, n

    def query(self, since: str | None = None, event_type: str | None = None) -> list[dict]:
        if not self._path.exists():
            return []
        since_ts = float(since) if since else 0.0
        results = []
        with self._path.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                    if ev.get("ts", 0) < since_ts:
                        continue
                    if event_type and ev.get("type") != event_type:
                        continue
                    results.append(ev)
                except json.JSONDecodeError:
                    continue
        return results[-10_000:]  # cap response size

    def count(self) -> int:
        if not self._path.exists():
            return 0
        with self._path.open() as f:
            return sum(1 for _ in f)


if __name__ == "__main__":
    import tempfile
    d = Path(tempfile.mkdtemp())
    j = SemanticJournal({"path": d / "j.jsonl", "max_bytes": 600, "keep_files": 2})
    for i in range(3):
        j.append("terminal_exec", f"echo {i}")
    assert j.verify() == (True, 3) and oct((d / "j.jsonl").stat().st_mode)[-3:] == "600"
    lines = (d / "j.jsonl").read_text().splitlines()
    (d / "j.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")   # delete one
    assert not j.verify()[0]
    j2 = SemanticJournal({"path": d / "k.jsonl", "max_bytes": 600, "keep_files": 2})
    for i in range(20):
        j2.append("auth_refused", "/x" * 10)
    assert (d / "k.jsonl.1").exists() and not (d / "k.jsonl.3").exists()
    print("semantic_journal selftest OK")
