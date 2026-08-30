"""
Semantic journal — typed OS event stream for AI consumption.

Events are appended to ~/.config/trixie-gateway/journal.jsonl (one JSON object per line).
The journal consumer reads from /proc/kmsg or inotify watches on key paths
to auto-generate events. AI queries can filter by event type and time range.

Event schema:
  { "ts": 1234567890.123, "type": "file_write|proc_exit|pkg_install|...",
    "subject": "...", "detail": {...} }
"""

import json
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
    "lock", "unlock",
    "firewall_rule_add", "firewall_rule_del", "firewall_policy",
    "firewall_flush", "firewall_preset",
    "bluetooth_power", "bluetooth_connect", "bluetooth_disconnect",
    "bluetooth_pair", "bluetooth_remove",
    "terminal_exec",
    "webrtc_offer", "webrtc_close",
}


class SemanticJournal:
    def __init__(self, config: dict):
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._path = Path(config.get("path", JOURNAL_PATH))
        self._max_events = config.get("max_events", 100_000)

    def append(self, event_type: str, subject: str, detail: dict | None = None):
        assert event_type in KNOWN_TYPES, f"unknown event type: {event_type}"
        entry = {
            "ts": time.time(),
            "type": event_type,
            "subject": subject,
            "detail": detail or {},
        }
        with self._path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

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
