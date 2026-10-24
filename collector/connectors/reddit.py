"""Reddit — two free routes:

1. Official API, app-only OAuth (free, non-commercial): REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET
   (create a "script" app at https://www.reddit.com/prefs/apps). Gives recent posts + full comment trees.
   Listings only go back ~1000 items, so for 2023-2024 use route 2.
2. Archive dumps (.zst NDJSON, e.g. per-subreddit dumps from Academic Torrents / Arctic Shift)
   dropped in REDDIT_ARCHIVE_DIR -> `python -m collector reddit-archives`.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import logging
import re
import time
from pathlib import Path

import httpx

from ..http import PoliteClient
from ..models import Batch, EdgeRec, PostRec, UserRec
from .base import Connector

log = logging.getLogger(__name__)
API = "https://oauth.reddit.com"
_U_RE = re.compile(r"(?<![\w/])/?u/([A-Za-z0-9_-]{3,20})")


def _ts(v) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(float(v), tz=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


class RedditConnector(Connector):
    platform = "reddit"
    query_kinds = ("subreddit", "query")

    def __init__(self, settings, pseudo):
        super().__init__(settings, pseudo)
        self.http = PoliteClient(settings.user_agent, rps=1.0)  # free tier: 100 req/min
        self._token, self._exp = None, 0.0

    def enabled(self):
        return (bool(self.s.reddit_client_id and self.s.reddit_client_secret),
                "set REDDIT_CLIENT_ID/SECRET (free) or use reddit-archives")

    def _auth(self) -> dict:
        if not self._token or time.time() > self._exp - 60:
            basic = base64.b64encode(f"{self.s.reddit_client_id}:{self.s.reddit_client_secret}".encode()).decode()
            r = httpx.post("https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"},
                           headers={"Authorization": f"Basic {basic}", "User-Agent": self.s.user_agent}, timeout=30)
            r.raise_for_status()
            j = r.json()
            self._token, self._exp = j["access_token"], time.time() + j.get("expires_in", 3600)
        return {"Authorization": f"bearer {self._token}"}

    def _get(self, path: str, params: dict | None = None):
        r = self.http.get(f"{API}{path}", params={**(params or {}), "raw_json": 1}, headers=self._auth())
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        return r.json()

    # ---------------------------------------------------------- normalise
    def item_to_post(self, d: dict, method: str) -> tuple[PostRec | None, list[EdgeRec], str | None]:
        author = d.get("author")
        if not author or author in ("[deleted]", "AutoModerator"):
            return None, [], None
        a_uid = self.uid(author)
        is_sub = d.get("name", "").startswith("t3_") or "title" in d
        name = d.get("name") or (("t3_" if is_sub else "t1_") + d["id"])
        p_uid = self.uid(name)
        created = _ts(d.get("created_utc"))
        if is_sub:
            text = f"{d.get('title', '')}\n\n{d.get('selftext', '')}".strip()
            parent, root = None, p_uid
        else:
            text = d.get("body", "")
            parent = self.uid(d["parent_id"]) if d.get("parent_id") else None
            root = self.uid(d["link_id"]) if d.get("link_id") else None
        url = d.get("url") if is_sub and not d.get("is_self") else None
        post = PostRec(
            post_uid=p_uid, platform=self.platform, author_uid=a_uid,
            text=self.p.rewrite_mentions(self.platform, _U_RE.sub(r"@\1", text)),
            created_at=created, urls=[url] if url else [],
            media_type=d.get("post_hint") if is_sub else None, parent_post_uid=parent, root_post_uid=root,
            like_count=d.get("score"), reply_count=d.get("num_comments") if is_sub else None,
            community=d.get("subreddit"), source_method=method,
        )
        return post, [], author

    # ---------------------------------------------------------------- API
    def search(self, query: str, kind: str, limit: int = 300) -> Batch:
        b = Batch()
        path = f"/r/{query}/new" if kind == "subreddit" else "/search"
        after, n = None, 0
        while n < limit:
            params = {"limit": 100}
            if kind != "subreddit":
                params.update(q=query, sort="new", t="all")
            if after:
                params["after"] = after
            data = self._get(path, params)
            if not data:
                break
            children = data["data"]["children"]
            for c in children:
                post, _, author = self.item_to_post(c["data"], "search")
                if post and self.in_range(post.created_at):
                    b.posts.append(post)
                    b.users.append(UserRec(post.author_uid, self.platform))
                    b.discovered_users.append((author, post.author_uid))
                    if (c["data"].get("num_comments") or 0) > 0:
                        b.discovered_roots.append((c["data"]["id"], post.post_uid))
            n += len(children)
            after = data["data"].get("after")
            if not after or not children:
                break
        return b

    def resolve(self, kind: str, value: str) -> str | None:
        return value.strip().removeprefix("u/").removeprefix("/u/") or None

    def sample(self, raw_id: str) -> dict:
        data = self._get(f"/user/{raw_id}/overview", {"limit": 15}) or {"data": {"children": []}}
        about = self._get(f"/user/{raw_id}/about") or {}
        texts = [(c["data"].get("body") or c["data"].get("title") or "") for c in data["data"]["children"]]
        sub = (about.get("data") or {}).get("subreddit") or {}
        return {"bio": sub.get("public_description", ""), "sample": [t for t in texts if t][:8]}

    def timeline(self, raw_id: str, max_posts: int = 1000) -> Batch:
        b = Batch()
        about = (self._get(f"/user/{raw_id}/about") or {}).get("data") or {}
        if about.get("is_suspended"):
            return b
        b.users.append(UserRec(self.uid(raw_id), self.platform,
                               bio=((about.get("subreddit") or {}).get("public_description")),
                               account_created_at=_ts(about.get("created_utc"))))
        after, n = None, 0
        while n < max_posts:
            params = {"limit": 100, "sort": "new"}
            if after:
                params["after"] = after
            data = self._get(f"/user/{raw_id}/overview", params)
            if not data:
                break
            children = data["data"]["children"]
            stop = False
            for c in children:
                post, _, _ = self.item_to_post(c["data"], "timeline")
                if not post:
                    continue
                if post.created_at and post.created_at < self.s.date_from:
                    stop = True
                    break
                b.posts.append(post)
                root_id = (c["data"].get("link_id") or "t3_" + c["data"]["id"]).removeprefix("t3_")
                b.discovered_roots.append((root_id, self.uid("t3_" + root_id)))
            n += len(children)
            after = data["data"].get("after")
            if stop or not after:
                break
        return b

    def thread(self, raw_root: str) -> Batch:
        b = Batch()
        data = self._get(f"/comments/{raw_root}", {"limit": 500, "depth": 20})
        if not data:
            return b
        authors: dict[str, str] = {}
        sub = data[0]["data"]["children"][0]["data"]
        stack = [sub] + [c["data"] for c in data[1]["data"]["children"] if c["kind"] == "t1"]
        while stack:
            d = stack.pop()
            post, _, author = self.item_to_post(d, "thread")
            if post:
                b.posts.append(post)
                b.users.append(UserRec(post.author_uid, self.platform))
                authors[d.get("name", "")] = post.author_uid
                parent_author = authors.get(d.get("parent_id", ""))
                if parent_author and parent_author != post.author_uid:
                    b.edges.append(EdgeRec(post.author_uid, parent_author, self.platform, "reply",
                                           post.post_uid, post.created_at))
            replies = d.get("replies")
            if isinstance(replies, dict):
                stack.extend(c["data"] for c in replies["data"]["children"] if c["kind"] == "t1")
        return b

    # ------------------------------------------------------------ archives
    def iter_archives(self, directory: str):
        import zstandard

        for f in sorted(Path(directory).glob("*.zst")):
            log.info("reading archive %s", f.name)
            with f.open("rb") as fh:
                reader = zstandard.ZstdDecompressor(max_window_size=2**31).stream_reader(fh)
                for line in io.TextIOWrapper(reader, encoding="utf-8", errors="ignore"):
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    created = _ts(d.get("created_utc"))
                    if created and self.in_range(created):
                        yield d

    def archive_batches(self, directory: str, batch_size: int = 5000):
        b = Batch()
        for d in self.iter_archives(directory):
            post, _, _ = self.item_to_post(d, "archive")
            if not post:
                continue
            b.posts.append(post)
            b.users.append(UserRec(post.author_uid, self.platform))
            if len(b.posts) >= batch_size:
                yield b
                b = Batch()
        if b.posts:
            yield b
