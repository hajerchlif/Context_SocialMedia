"""Common connector interface. Every connector returns pseudonymised `Batch` objects."""
from __future__ import annotations

import datetime as dt
import logging

from ..models import Batch
from ..pseudo import Pseudonymizer

log = logging.getLogger(__name__)


class Connector:
    platform: str = "base"
    # seed kinds this platform understands for discovery
    query_kinds: tuple[str, ...] = ("query",)

    def __init__(self, settings, pseudo: Pseudonymizer):
        self.s = settings
        self.p = pseudo

    def enabled(self) -> tuple[bool, str]:
        return True, ""

    def uid(self, raw: str) -> str:
        return self.p.uid(self.platform, raw)

    def in_range(self, ts: dt.datetime | None) -> bool:
        if ts is None:
            return True
        return self.s.date_from <= ts <= self.s.date_to

    # --- time-windowed discovery (optional) ----------------------------------
    def supports_window(self, kind: str) -> bool:
        """True if search_window() can target a date range natively for this seed kind."""
        return False

    def search_window(self, query: str, kind: str, since, until, limit: int) -> Batch:
        raise NotImplementedError

    # --- to implement -------------------------------------------------------
    def search(self, query: str, kind: str, limit: int) -> Batch:
        """Discovery: posts/accounts matching a query/hashtag/community. Fills discovered_users."""
        return Batch()

    def resolve(self, kind: str, value: str) -> str | None:
        """Turn a manual seed (handle, acct, channel) into the raw id used by timeline()."""
        return None

    def sample(self, raw_id: str) -> dict:
        """Small profile + a few recent posts (texts) for the LLM relevance check."""
        return {"bio": "", "sample": []}

    def timeline(self, raw_id: str, max_posts: int) -> Batch:
        """Backfill the user's posts between DATE_FROM and DATE_TO."""
        return Batch()

    def follows(self, raw_id: str, limit: int) -> Batch:
        """Accounts followed by the user (snowball). Fills discovered_users + follow edges."""
        return Batch()

    def thread(self, raw_root: str) -> Batch:
        """Full conversation tree under a root post."""
        return Batch()
