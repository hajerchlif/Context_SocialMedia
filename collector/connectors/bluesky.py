"""Bluesky via the free public AppView API (no paid access).

- Public, no account: getProfile, getAuthorFeed, getPostThread, getFollows, searchActors.
- searchPosts needs a (free) session: set BSKY_HANDLE + BSKY_APP_PASSWORD (Settings > App passwords).
"""
from __future__ import annotations

import logging

from ..http import Blocked, PoliteClient
from ..models import Batch, EdgeRec, PostRec, UserRec, parse_ts
from .base import Connector

log = logging.getLogger(__name__)

PUBLIC = "https://public.api.bsky.app/xrpc"
PDS = "https://bsky.social/xrpc"


class BlueskyConnector(Connector):
    platform = "bluesky"
    query_kinds = ("query", "hashtag", "account")

    def __init__(self, settings, pseudo):
        super().__init__(settings, pseudo)
        self.http = PoliteClient(settings.user_agent, rps=5)
        self._jwt: str | None = None

    # ------------------------------------------------------------------ infra
    def _session(self) -> str | None:
        if self._jwt or not (self.s.bsky_handle and self.s.bsky_app_password):
            return self._jwt
        r = self.http.post(f"{PDS}/com.atproto.server.createSession",
                           json={"identifier": self.s.bsky_handle, "password": self.s.bsky_app_password})
        if r.status_code == 200:
            self._jwt = r.json()["accessJwt"]
        else:
            log.warning("Bluesky login failed (%s) — searchPosts disabled, using searchActors", r.status_code)
        return self._jwt

    def _get(self, method: str, params: dict, auth: bool = False) -> dict:
        if auth and self._session():
            r = self.http.get(f"{PDS}/{method}", params=params, headers={"Authorization": f"Bearer {self._jwt}"})
        else:
            r = self.http.get(f"{PUBLIC}/{method}", params=params)
        if r.status_code in (400, 404):
            return {}
        if r.status_code in (401, 403):
            raise Blocked(f"{method}: HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------- normalise
    def _user(self, prof: dict) -> UserRec:
        return UserRec(
            user_uid=self.uid(prof["did"]), platform=self.platform,
            bio=self.p.rewrite_mentions(self.platform, prof.get("description") or ""),
            account_created_at=parse_ts(prof.get("createdAt")),
            followers_count=prof.get("followersCount"), following_count=prof.get("followsCount"),
            posts_count=prof.get("postsCount"),
            is_bot_flag=any(lab.get("val") == "bot" for lab in prof.get("labels", []) or []),
        )

    def post_to_batch(self, post: dict, method: str, reason: dict | None = None) -> Batch:
        """Normalise one app.bsky.feed.defs#postView (+ optional repost reason)."""
        b = Batch()
        rec = post.get("record", {}) or {}
        author = post.get("author", {}) or {}
        did = author.get("did")
        if not did or not post.get("uri"):
            return b
        a_uid = self.uid(did)
        p_uid = self.uid(post["uri"])
        created = parse_ts(rec.get("createdAt") or post.get("indexedAt"))
        text = rec.get("text") or ""

        hashtags, mentions, urls, known = [], [], [], {}
        raw_bytes = text.encode("utf-8")
        for facet in rec.get("facets", []) or []:
            idx = facet.get("index", {})
            for feat in facet.get("features", []) or []:
                t = feat.get("$type", "")
                if t.endswith("#mention") and feat.get("did"):
                    mentions.append(feat["did"])
                    handle = raw_bytes[idx.get("byteStart", 0):idx.get("byteEnd", 0)].decode("utf-8", "ignore")
                    if handle:
                        known[handle.lstrip("@")] = feat["did"]
                elif t.endswith("#link") and feat.get("uri"):
                    urls.append(feat["uri"])
                elif t.endswith("#tag") and feat.get("tag"):
                    hashtags.append(feat["tag"].lower())

        media_type, alt, quote_uri = None, None, None
        emb = rec.get("embed") or {}
        et = emb.get("$type", "")
        if "recordWithMedia" in et:
            quote_uri = ((emb.get("record") or {}).get("record") or {}).get("uri")
            emb, et = emb.get("media") or {}, (emb.get("media") or {}).get("$type", "")
        if et.endswith("embed.images"):
            media_type = "image"
            alt = " | ".join(i.get("alt", "") for i in emb.get("images", []) if i.get("alt")) or None
        elif et.endswith("embed.video"):
            media_type, alt = "video", emb.get("alt")
        elif et.endswith("embed.external"):
            media_type = "link"
            ext = emb.get("external") or {}
            if ext.get("uri"):
                urls.append(ext["uri"])
        elif et.endswith("embed.record"):
            quote_uri = (emb.get("record") or {}).get("uri")

        reply = rec.get("reply") or {}
        parent_uri = (reply.get("parent") or {}).get("uri")
        root_uri = (reply.get("root") or {}).get("uri") or post["uri"]
        mention_uids = [self.uid(d) for d in mentions]

        b.users.append(self._user(author))
        b.posts.append(PostRec(
            post_uid=p_uid, platform=self.platform, author_uid=a_uid,
            text=self.p.rewrite_mentions(self.platform, text, known), created_at=created,
            lang=(rec.get("langs") or [None])[0], hashtags=sorted(set(hashtags)), mentions_uid=mention_uids,
            urls=urls, media_type=media_type, media_alt_text=alt,
            parent_post_uid=self.uid(parent_uri) if parent_uri else None, root_post_uid=self.uid(root_uri),
            quote_of_uid=self.uid(quote_uri) if quote_uri else None,
            like_count=post.get("likeCount"), repost_count=post.get("repostCount"),
            reply_count=post.get("replyCount"), source_method=method,
        ))
        for m in mention_uids:
            b.edges.append(EdgeRec(a_uid, m, self.platform, "mention", p_uid, created))
        b.discovered_users += [(d, self.uid(d)) for d in mentions]
        if quote_uri:
            qdid = quote_uri.split("/")[2] if quote_uri.startswith("at://") else None
            if qdid:
                b.edges.append(EdgeRec(a_uid, self.uid(qdid), self.platform, "quote", p_uid, created))
        if parent_uri and parent_uri.startswith("at://"):
            pdid = parent_uri.split("/")[2]
            b.edges.append(EdgeRec(a_uid, self.uid(pdid), self.platform, "reply", p_uid, created))
        if (post.get("replyCount") or 0) > 0 or parent_uri:
            b.discovered_roots.append((root_uri, self.uid(root_uri)))
        if reason and "reasonRepost" in reason.get("$type", ""):
            by = (reason.get("by") or {}).get("did")
            if by:
                b.edges.append(EdgeRec(self.uid(by), a_uid, self.platform, "repost", p_uid,
                                       parse_ts(reason.get("indexedAt"))))
                b.discovered_users.append((did, a_uid))
        return b

    # ------------------------------------------------------------------- API
    def search(self, query: str, kind: str, limit: int = 200) -> Batch:
        b = Batch()
        if kind == "account":
            raw = self.resolve(kind, query)
            if raw:
                b.discovered_users.append((raw, self.uid(raw)))
            return b
        if self._session():
            return self.search_window(query, kind, self.s.date_from, self.s.date_to, limit)
        else:
            # Without a session, discover accounts whose profile matches the query (public endpoint).
            data = self._get("app.bsky.actor.searchActors", {"q": query, "limit": min(100, limit)})
            for actor in data.get("actors", []):
                b.discovered_users.append((actor["did"], self.uid(actor["did"])))
        return b

    def supports_window(self, kind: str) -> bool:
        return kind in ("query", "hashtag") and bool(self._session())

    def search_window(self, query: str, kind: str, since, until, limit: int) -> Batch:
        """Full-text post search restricted to [since, until) — needs a (free) app-password session."""
        b = Batch()
        q = f"#{query}" if kind == "hashtag" else query
        cursor, n = None, 0
        while n < limit:
            params = {"q": q, "limit": min(100, limit - n), "sort": "top",
                      "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "until": until.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if cursor:
                params["cursor"] = cursor
            data = self._get("app.bsky.feed.searchPosts", params, auth=True)
            posts = data.get("posts", [])
            for post in posts:
                pb = self.post_to_batch(post, "search")
                pb.discovered_users.append((post["author"]["did"], self.uid(post["author"]["did"])))
                b.extend(pb)
            n += len(posts)
            cursor = data.get("cursor")
            if not cursor or not posts:
                break
        return b

    def resolve(self, kind: str, value: str) -> str | None:
        value = value.strip().lstrip("@")
        if value.startswith("did:"):
            return value
        data = self._get("app.bsky.actor.getProfile", {"actor": value})
        return data.get("did")

    def sample(self, raw_id: str) -> dict:
        prof = self._get("app.bsky.actor.getProfile", {"actor": raw_id})
        feed = self._get("app.bsky.feed.getAuthorFeed", {"actor": raw_id, "limit": 15, "filter": "posts_no_replies"})
        texts = [i["post"]["record"].get("text", "") for i in feed.get("feed", [])
                 if i.get("post", {}).get("author", {}).get("did") == raw_id]
        return {"bio": prof.get("description") or "", "sample": [t for t in texts if t][:8],
                "posts_count": prof.get("postsCount", 0)}

    def timeline(self, raw_id: str, max_posts: int = 3000) -> Batch:
        b = Batch()
        prof = self._get("app.bsky.actor.getProfile", {"actor": raw_id})
        if prof:
            b.users.append(self._user(prof))
        cursor, n = None, 0
        while n < max_posts:
            params = {"actor": raw_id, "limit": 100, "filter": "posts_with_replies"}
            if cursor:
                params["cursor"] = cursor
            data = self._get("app.bsky.feed.getAuthorFeed", params)
            items = data.get("feed", [])
            if not items:
                break
            too_old = False
            for it in items:
                post = it.get("post", {})
                ts = parse_ts((it.get("reason") or {}).get("indexedAt") or post.get("record", {}).get("createdAt"))
                if ts and ts < self.s.date_from:
                    too_old = True
                    continue
                if ts and ts > self.s.date_to:
                    continue
                b.extend(self.post_to_batch(post, "timeline", it.get("reason")))
                n += 1
            cursor = data.get("cursor")
            if too_old or not cursor:
                break
        return b

    def follows(self, raw_id: str, limit: int = 500) -> Batch:
        b = Batch()
        src = self.uid(raw_id)
        cursor, n = None, 0
        while n < limit:
            params = {"actor": raw_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = self._get("app.bsky.graph.getFollows", params)
            for prof in data.get("follows", []):
                u = self.uid(prof["did"])
                b.discovered_users.append((prof["did"], u))
                b.edges.append(EdgeRec(src, u, self.platform, "follow"))
                n += 1
            cursor = data.get("cursor")
            if not cursor or not data.get("follows"):
                break
        return b

    def thread(self, raw_root: str) -> Batch:
        b = Batch()
        data = self._get("app.bsky.feed.getPostThread", {"uri": raw_root, "depth": 50, "parentHeight": 0})
        stack = [data.get("thread")]
        while stack:
            node = stack.pop()
            if not node or "threadViewPost" not in node.get("$type", ""):
                continue
            pb = self.post_to_batch(node["post"], "thread")
            pb.discovered_roots = []  # we are already walking this tree
            b.extend(pb)
            stack.extend(node.get("replies", []) or [])
        return b
