"""Pseudonymisation at ingestion: HMAC-SHA256(secret, platform:id) -> 24 hex chars."""
from __future__ import annotations

import hashlib
import hmac
import re

_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.\-]+(?:@[A-Za-z0-9_.\-]+)?)")


class Pseudonymizer:
    def __init__(self, secret: str):
        if not secret:
            raise ValueError("HMAC secret required")
        self._key = secret.encode()

    def uid(self, platform: str, raw_id: str) -> str:
        msg = f"{platform}:{raw_id.lower().lstrip('@')}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()[:24]

    def rewrite_mentions(self, platform: str, text: str, known: dict[str, str] | None = None) -> str:
        """Replace @handle in text by @user_<uid>. `known` maps handle -> stable raw id (DID, account id)."""
        if not text:
            return text
        known = {k.lower().lstrip("@"): v for k, v in (known or {}).items()}

        def repl(m: re.Match) -> str:
            handle = m.group(1).rstrip(".").lower()
            raw = known.get(handle, handle)
            return f"@user_{self.uid(platform, raw)}"

        return _MENTION_RE.sub(repl, text)
