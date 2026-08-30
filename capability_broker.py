"""
Capability token broker — scoped, time-limited, HMAC-signed tokens.

Token format (base64url-encoded JSON envelope):
  { "sub": "...", "scope": "chat", "exp": 1234567890, "sig": "<hmac-sha256>" }

The signing key is generated at first run and stored in ~/.config/trixie-gateway/gateway-key.
Tokens are stateless — no database needed, validated by signature + expiry.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path


class CapabilityBroker:
    KEY_PATH = Path.home() / ".config" / "trixie-gateway" / "gateway-key"

    def __init__(self, config: dict):
        self._key = self._load_or_create_key()
        self._config = config

    def _load_or_create_key(self) -> bytes:
        self.KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if self.KEY_PATH.exists():
            return self.KEY_PATH.read_bytes()
        key = os.urandom(32)
        self.KEY_PATH.write_bytes(key)
        self.KEY_PATH.chmod(0o600)
        return key

    def _sign(self, payload: dict) -> str:
        body = json.dumps(payload, sort_keys=True).encode()
        sig = hmac.new(self._key, body, hashlib.sha256).hexdigest()
        envelope = {**payload, "sig": sig}
        return base64.urlsafe_b64encode(json.dumps(envelope).encode()).decode()

    def _verify(self, token: str) -> dict | None:
        try:
            envelope = json.loads(base64.urlsafe_b64decode(token + "=="))
            sig = envelope.pop("sig", None)
            expected = hmac.new(
                self._key,
                json.dumps(envelope, sort_keys=True).encode(),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(sig or "", expected):
                return None
            return envelope
        except Exception:
            return None

    def issue(self, scope: str, ttl_seconds: int = 3600, subject: str = "unknown") -> str:
        payload = {
            "sub": subject,
            "scope": scope,
            "exp": int(time.time()) + ttl_seconds,
            "iat": int(time.time()),
        }
        return self._sign(payload)

    def validate(self, token: str, scope: str) -> bool:
        if not token:
            # Allow unauthenticated access if config says so (dev mode)
            return self._config.get("allow_unauthenticated", False)
        payload = self._verify(token)
        if not payload:
            return False
        if payload.get("exp", 0) < time.time():
            return False
        token_scope = payload.get("scope", "")
        # "admin" scope grants everything; specific scope must match
        return token_scope == "admin" or token_scope == scope
