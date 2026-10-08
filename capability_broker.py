# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# Licensed under the PolyForm Strict License 1.0.0
# (https://polyformproject.org/licenses/strict/1.0.0/): free for personal,
# non-commercial use; no redistribution, modified versions or sale.
# Commercial licences: cou645@gmail.com
# Donations via PayPal: cou645@gmail.com
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
import secrets
import time
from pathlib import Path


class CapabilityBroker:
    KEY_PATH = Path.home() / ".config" / "trixie-gateway" / "gateway-key"
    # Every issued token is recorded here, so it can be listed, revoked and
    # expire when unused (ISO 27001 gap G4). Signed tokens alone could not be
    # withdrawn before their 10-year "exp".
    TOKENS_PATH = Path.home() / ".config" / "trixie-gateway" / "tokens.json"
    IDLE_LIMIT = 90 * 24 * 3600     # a device unused for 90 days must re-pair

    def __init__(self, config: dict):
        self._key = self._load_or_create_key()
        self._config = config
        try:
            self._tokens = json.loads(self.TOKENS_PATH.read_text())
        except (OSError, ValueError):
            self._tokens = {}
        self._saved_at = 0.0

    def _save_tokens(self, force=True):
        now = time.time()
        if not force and now - self._saved_at < 60:   # last_used: once a minute
            return
        self._saved_at = now
        tmp = self.TOKENS_PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._tokens, f, indent=1)
        os.replace(tmp, self.TOKENS_PATH)

    @staticmethod
    def _jti(token: str, payload: dict) -> str:
        # tokens issued before the registry carry no jti: key them by hash
        return payload.get("jti") or hashlib.sha256(token.encode()).hexdigest()[:16]

    def _check(self, token: str) -> tuple[str, dict] | None:
        """(jti, payload) of a valid, registered, unrevoked, recently used token."""
        payload = self._verify(token) if token else None
        if not payload or payload.get("exp", 0) < time.time():
            return None
        jti, now = self._jti(token, payload), time.time()
        entry = self._tokens.get(jti)
        if entry is None:
            if payload.get("jti"):        # issued by us but not on record: refuse
                return None
            # pre-registry token (already paired phones): adopt it, keep terminal
            entry = self._tokens[jti] = {"sub": payload.get("sub", "unknown"),
                                         "issued": payload.get("iat", now),
                                         "terminal": True, "revoked": False,
                                         "last_used": now}
            self._save_tokens()
        if entry.get("revoked") or now - entry.get("last_used", now) > self.IDLE_LIMIT:
            return None
        entry["last_used"] = now
        self._save_tokens(force=False)
        return jti, payload

    def tokens(self) -> list[dict]:
        return [dict(v, jti=k) for k, v in self._tokens.items()]

    def set_token(self, jti: str, revoked: bool | None = None,
                  terminal: bool | None = None) -> bool:
        entry = self._tokens.get(jti)
        if entry is None:
            return False
        if revoked is not None:
            entry["revoked"] = bool(revoked)
        if terminal is not None:
            entry["terminal"] = bool(terminal)
        self._save_tokens()
        return True

    def renew(self, token: str, ttl_seconds: int) -> str | None:
        """A fresh token for the same device, scope and terminal permission;
        the old one is revoked so only one stays live per renewal."""
        c = self._check(token)
        if not c:
            return None
        jti, payload = c
        new = self.issue(payload.get("scope", "chat"), ttl_seconds, payload.get("sub", "unknown"))
        new_jti = self._verify(new)["jti"]
        self._tokens[new_jti]["terminal"] = self._tokens[jti].get("terminal", False)
        self._tokens[jti]["revoked"] = True
        self._save_tokens()
        return new

    def can_terminal(self, token: str) -> bool:
        c = self._check(token)
        return bool(c and self._tokens[c[0]].get("terminal"))

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
        jti = secrets.token_hex(8)
        payload = {
            "sub": subject,
            "scope": scope,
            "exp": int(time.time()) + ttl_seconds,
            "iat": int(time.time()),
            "jti": jti,
        }
        # new devices start without the terminal; the PC owner grants it on /admin
        self._tokens[jti] = {"sub": subject, "issued": payload["iat"], "terminal": False,
                             "revoked": False, "last_used": payload["iat"]}
        self._save_tokens()
        return self._sign(payload)

    def subject(self, token: str) -> str | None:
        """The device_name a token was issued to (the "sub" claim from
        issue()), or None if the token doesn't verify. Server-derived, so
        a caller can't claim to be a different device than it paired as."""
        c = self._check(token)
        return c[1].get("sub") if c else None

    def validate(self, token: str, scope: str) -> bool:
        if not token:
            # Allow unauthenticated access if config says so (dev mode)
            return self._config.get("allow_unauthenticated", False)
        c = self._check(token)
        if not c:
            return False
        token_scope = c[1].get("scope", "")
        # "admin" scope grants everything; specific scope must match
        return token_scope == "admin" or token_scope == scope


if __name__ == "__main__":
    import tempfile
    d = Path(tempfile.mkdtemp())
    CapabilityBroker.KEY_PATH, CapabilityBroker.TOKENS_PATH = d / "k", d / "t.json"
    b = CapabilityBroker({})
    t = b.issue("admin", 3600, "Phone")
    assert b.validate(t, "device") and b.subject(t) == "Phone"
    assert not b.can_terminal(t)                       # new device: no terminal
    jti = b.tokens()[0]["jti"]
    b.set_token(jti, terminal=True); assert b.can_terminal(t)
    b.set_token(jti, revoked=True)
    assert not b.validate(t, "device") and b.subject(t) is None
    # legacy token (no jti): adopted, keeps terminal, revocable
    legacy = b._sign({"sub": "Old", "scope": "admin", "exp": int(time.time()) + 99, "iat": 1})
    assert b.validate(legacy, "device") and b.can_terminal(legacy)
    lj = [x["jti"] for x in b.tokens() if x["sub"] == "Old"][0]
    b.set_token(lj, revoked=True); assert not b.validate(legacy, "device")
    # idle for over 90 days
    t2 = b.issue("admin", 3600, "Idle"); j2 = b._jti(t2, b._verify(t2))
    b._tokens[j2]["last_used"] -= b.IDLE_LIMIT + 1
    assert not b.validate(t2, "device")
    # signed with our key but not on record (registry lost): refused
    t3 = b.issue("admin", 3600, "Gone"); b._tokens.pop(b._jti(t3, b._verify(t3)))
    assert not b.validate(t3, "device")
    assert CapabilityBroker({}).tokens()                # persisted
    assert oct((d / "t.json").stat().st_mode)[-3:] == "600"
    assert not b.validate("", "device")
    t4 = b.issue("admin", 3600, "Renew"); b.set_token(b._jti(t4, b._verify(t4)), terminal=True)
    t5 = b.renew(t4, 7200)
    assert b.subject(t5) == "Renew" and b.can_terminal(t5) and not b.validate(t4, "device")
    print("capability_broker selftest OK")
