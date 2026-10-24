"""Polite HTTP client: per-host rate limit, retries with backoff, Retry-After, robots.txt for web pages."""
from __future__ import annotations

import logging
import random
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


class Blocked(Exception):
    """Raised when a site blocks us (CAPTCHA, login wall, persistent 403/429). We stop, never bypass."""


class QuotaExhausted(Blocked):
    """Daily API quota used up: stop this platform for the current cycle (nothing is marked as failed)."""


class PoliteClient:
    def __init__(self, user_agent: str, rps: float = 2.0, timeout: float = 30.0, respect_robots: bool = False):
        self.client = httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
        self.min_interval = 1.0 / max(rps, 0.01)
        self.respect_robots = respect_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._lock = threading.Lock()
        self.user_agent = user_agent

    def _wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last.get(host, 0)
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots[base] = rp
        return self._robots[base].can_fetch(self.user_agent, url)

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, retries: int = 5) -> httpx.Response:
        if not self.allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        for attempt in range(retries):
            self._wait(host)
            try:
                r = self.client.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                sleep = min(60, 2 ** attempt) + random.random()
                log.warning("network error %s (%s), retry in %.1fs", url, e, sleep)
                time.sleep(sleep)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after") or r.headers.get("ratelimit-reset")
                sleep = _retry_after(ra) if ra else min(120, 2 ** attempt * 5) + random.random()
                log.warning("HTTP %s on %s, sleeping %.0fs", r.status_code, host, sleep)
                time.sleep(sleep)
                continue
            remaining = r.headers.get("ratelimit-remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 5:
                reset = _retry_after(r.headers.get("ratelimit-reset", "30"))
                log.info("rate limit nearly exhausted on %s, pausing %.0fs", host, reset)
                time.sleep(reset)
            return r
        raise Blocked(f"giving up on {url} after {retries} attempts")

    def post(self, url: str, json: dict) -> httpx.Response:
        self._wait(urlparse(url).netloc)
        return self.client.post(url, json=json)


def _retry_after(value: str) -> float:
    try:
        v = float(value)
        # ratelimit-reset may be an epoch timestamp
        if v > 10_000_000:
            return max(1.0, v - time.time())
        return max(1.0, v)
    except (TypeError, ValueError):
        return 30.0
