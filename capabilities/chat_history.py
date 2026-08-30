"""Chat history — save and load named conversations on this host."""

import json
import time
import uuid
from pathlib import Path

_HISTORY_DIR = Path.home() / ".config" / "trixie-gateway" / "chat-history"


def _ensure_dir():
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _conv_path(conv_id: str) -> Path:
    return _HISTORY_DIR / f"{conv_id}.json"


def list_conversations() -> list[dict]:
    _ensure_dir()
    convs = []
    for f in sorted(_HISTORY_DIR.glob("*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            convs.append({
                "id":         data["id"],
                "title":      data["title"],
                "model":      data.get("model", ""),
                "saved_at":   data["saved_at"],
                "msg_count":  len(data.get("messages", [])),
            })
        except Exception:
            continue
    return convs


def save_conversation(title: str, model: str, messages: list[dict]) -> dict:
    _ensure_dir()
    conv_id = str(uuid.uuid4())[:8]
    data = {
        "id":       conv_id,
        "title":    title.strip() or "Untitled",
        "model":    model,
        "saved_at": int(time.time()),
        "messages": messages,
    }
    _conv_path(conv_id).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return {"ok": True, "id": conv_id}


def load_conversation(conv_id: str) -> dict | None:
    p = _conv_path(conv_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def delete_conversation(conv_id: str) -> dict:
    p = _conv_path(conv_id)
    if not p.exists():
        return {"ok": False, "error": "not found"}
    p.unlink()
    return {"ok": True}
