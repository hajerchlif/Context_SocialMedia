"""X, Facebook/Instagram (Meta Content Library) and TikTok Research API.

These platforms forbid unauthenticated scraping and protect content behind login walls / anti-bot
systems. This collector does NOT log into accounts or bypass those protections. The connectors are
placeholders that switch on only when you obtain official researcher access (tokens in .env);
implement the official client in this file at that point.
"""
from __future__ import annotations

import os

from .base import Connector


class _Official(Connector):
    env_var = ""
    how = ""

    def enabled(self):
        if not os.getenv(self.env_var):
            return False, f"disabled: requires official researcher access ({self.how})"
        return False, f"{self.env_var} set, but the official client is not implemented yet"


class XConnector(_Official):
    platform, env_var, how = "x", "X_BEARER_TOKEN", "X API — paid tiers / academic programme"


class MetaConnector(_Official):
    platform, env_var, how = "meta", "META_CONTENT_LIBRARY_TOKEN", "Meta Content Library via ICPSR/SOMAR"


class TikTokConnector(_Official):
    platform, env_var, how = "tiktok", "TIKTOK_RESEARCH_CLIENT_KEY", "TikTok Research API application"
