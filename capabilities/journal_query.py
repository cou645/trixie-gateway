"""Journal query capability — append and search the semantic event journal."""

import json
import time
from pathlib import Path

KNOWN_TYPES: frozenset[str] = frozenset({
    "file_write", "file_delete", "proc_start", "proc_exit",
    "pkg_install", "pkg_remove", "layer_mount", "layer_umount",
    "net_connect", "net_disconnect", "wifi_connect", "wifi_disconnect",
    "ai_query", "capability_issue", "boot", "shutdown", "remaster",
    "brightness_change", "volume_change", "mute_change",
})


def append(path: Path, event_type: str, subject: str, detail: dict | None = None):
    if event_type not in KNOWN_TYPES:
        raise ValueError(f"unknown event type: {event_type!r}")
    entry = {
        "ts":      time.time(),
        "type":    event_type,
        "subject": subject,
        "detail":  detail or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def query(
    path: Path,
    since: float | None = None,
    event_type: str | None = None,
    limit: int = 10_000,
) -> list[dict]:
    if not path.exists():
        return []
    since_ts = since or 0.0
    results = []
    with path.open() as f:
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
    return results[-limit:]


def count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        return sum(1 for _ in f)
