"""Normalised records shared by all connectors."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class UserRec:
    user_uid: str
    platform: str
    bio: str | None = None
    account_created_at: dt.datetime | None = None
    followers_count: int | None = None
    following_count: int | None = None
    posts_count: int | None = None
    is_bot_flag: bool | None = None


@dataclass
class PostRec:
    post_uid: str
    platform: str
    author_uid: str
    text: str | None
    created_at: dt.datetime | None
    lang: str | None = None
    hashtags: list[str] = field(default_factory=list)
    mentions_uid: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    media_type: str | None = None
    media_alt_text: str | None = None
    parent_post_uid: str | None = None
    root_post_uid: str | None = None
    quote_of_uid: str | None = None
    like_count: int | None = None
    repost_count: int | None = None
    reply_count: int | None = None
    community: str | None = None
    source_method: str = "api"


@dataclass
class EdgeRec:
    src_uid: str
    dst_uid: str
    platform: str
    type: str
    post_uid: str = ""
    created_at: dt.datetime | None = None


@dataclass
class Batch:
    users: list[UserRec] = field(default_factory=list)
    posts: list[PostRec] = field(default_factory=list)
    edges: list[EdgeRec] = field(default_factory=list)
    # raw ids discovered (for the crawl queue): (raw_id, user_uid)
    discovered_users: list[tuple[str, str]] = field(default_factory=list)
    # raw thread roots discovered: (raw_root, root_uid)
    discovered_roots: list[tuple[str, str]] = field(default_factory=list)

    def extend(self, other: "Batch") -> None:
        self.users += other.users
        self.posts += other.posts
        self.edges += other.edges
        self.discovered_users += other.discovered_users
        self.discovered_roots += other.discovered_roots


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
