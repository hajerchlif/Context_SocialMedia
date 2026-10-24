"""Time balancing: the same budget for every month between DATE_FROM and DATE_TO.

Without this, every source is biased towards "now" (timelines and searches are newest-first).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .models import Batch


def month_key(ts: dt.datetime | None) -> str | None:
    return ts.strftime("%Y-%m") if ts else None


def month_windows(date_from: dt.datetime, date_to: dt.datetime) -> list[tuple[str, dt.datetime, dt.datetime]]:
    """[(\"2023-01\", start, end), ...] — end is exclusive and clipped to date_to."""
    out = []
    cur = dt.datetime(date_from.year, date_from.month, 1, tzinfo=dt.timezone.utc)
    while cur <= date_to:
        nxt = dt.datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=dt.timezone.utc)
        out.append((cur.strftime("%Y-%m"), max(cur, date_from), min(nxt, date_to)))
        cur = nxt
    return out


def cap_batch(b: Batch, per_bucket: int, by_author: bool = False) -> Batch:
    """Keep at most `per_bucket` posts per month (or per author-month). Items linked to dropped posts
    (edges, thread roots, discovered accounts) are dropped too; users and follow edges are kept."""
    if per_bucket <= 0:
        return b
    counts: dict[tuple, int] = defaultdict(int)
    kept = []
    for p in b.posts:
        key = (p.author_uid if by_author else "", month_key(p.created_at))
        if counts[key] < per_bucket:
            counts[key] += 1
            kept.append(p)
    kept_ids = {p.post_uid for p in kept}
    kept_roots = {p.root_post_uid for p in kept} | kept_ids
    linked = {p.author_uid for p in kept} | {m for p in kept for m in p.mentions_uid}
    return Batch(
        users=b.users,
        posts=kept,
        edges=[e for e in b.edges if not e.post_uid or e.post_uid in kept_ids],
        discovered_users=[(r, u) for r, u in b.discovered_users if u in linked or not b.posts],
        discovered_roots=[(r, u) for r, u in b.discovered_roots if u in kept_roots],
    )


def root_months(b: Batch) -> dict[str, str]:
    """Month of each discovered thread root (its own date if present, else the earliest reply seen)."""
    own: dict[str, str] = {}
    earliest: dict[str, str] = {}
    for p in b.posts:
        m = month_key(p.created_at)
        if not m:
            continue
        root = p.root_post_uid or p.post_uid
        if p.post_uid == root:
            own[root] = m
        elif root not in earliest or m < earliest[root]:
            earliest[root] = m
    return {**earliest, **own}
