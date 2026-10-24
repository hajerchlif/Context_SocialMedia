"""Lemmy (fediverse, Reddit-like) via its free public API v3 — no key, no account.

Raw ids:  user -> "name@host"   (queried through LEMMY_INSTANCE, which federates most of the network)
          root -> "instance|post_id"
Pseudonyms use global ActivityPub ids (actor_id, ap_id).
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..http import PoliteClient
from ..models import Batch, EdgeRec, PostRec, UserRec, parse_ts
from .base import Connector

log = logging.getLogger(__name__)


def acct_of(person: dict) -> str:
    host = urlparse(person.get("actor_id", "")).netloc
    return f"{person['name']}@{host}" if host else person["name"]


class LemmyConnector(Connector):
    platform = "lemmy"
    query_kinds = ("query", "community", "account")

    def __init__(self, settings, pseudo):
        super().__init__(settings, pseudo)
        self.instance = getattr(settings, "lemmy_instance", "lemmy.world")
        self.http = PoliteClient(settings.user_agent, rps=1.0)

    def enabled(self):
        return (bool(self.instance) and getattr(self.s, "enable_lemmy", True), "ENABLE_LEMMY=false")

    def _get(self, path: str, params: dict, instance: str | None = None):
        try:
            r = self.http.get(f"https://{instance or self.instance}/api/v3/{path}", params=params)
        except Exception:  # noqa: BLE001  (home instance down / not Lemmy)
            if instance:
                return None
            raise
        if r.status_code >= 400 or "json" not in r.headers.get("content-type", ""):
            return None
        return r.json()

    def _user_page(self, raw_id: str, **params):
        """User history: ask the user's home instance first (full history), then our instance."""
        name, _, host = raw_id.partition("@")
        if host and host != self.instance:
            data = self._get("user", {"username": name, **params}, instance=host)
            if data and data.get("person_view"):
                return data
        return self._get("user", {"username": raw_id, **params})

    # ------------------------------------------------------------ normalise
    def _user(self, person: dict, counts: dict | None = None) -> UserRec:
        counts = counts or {}
        return UserRec(
            user_uid=self.uid(person["actor_id"]), platform=self.platform,
            bio=self.p.rewrite_mentions(self.platform, person.get("bio") or ""),
            account_created_at=parse_ts(person.get("published")),
            posts_count=(counts.get("post_count") or 0) + (counts.get("comment_count") or 0) or None,
            is_bot_flag=bool(person.get("bot_account")),
        )

    def post_to_batch(self, pv: dict, method: str) -> Batch:
        b = Batch()
        post, creator, comm = pv["post"], pv["creator"], pv.get("community") or {}
        if post.get("deleted") or post.get("removed") or creator.get("bot_account"):
            return b
        a_uid = self.uid(creator["actor_id"])
        p_uid = self.uid(post["ap_id"])
        text = "\n\n".join(x for x in (post.get("name"), post.get("body")) if x)
        counts = pv.get("counts") or {}
        b.users.append(self._user(creator))
        b.posts.append(PostRec(
            post_uid=p_uid, platform=self.platform, author_uid=a_uid,
            text=self.p.rewrite_mentions(self.platform, text), created_at=parse_ts(post.get("published")),
            urls=[post["url"]] if post.get("url") else [], media_type="link" if post.get("url") else None,
            root_post_uid=p_uid, like_count=counts.get("score"), reply_count=counts.get("comments"),
            community=f"{comm.get('name')}@{urlparse(comm.get('actor_id', '')).netloc}" if comm else None,
            source_method=method,
        ))
        b.discovered_users.append((acct_of(creator), a_uid))
        if (counts.get("comments") or 0) > 0:
            b.discovered_roots.append((f"{self.instance}|{post['id']}", p_uid))
        return b

    def comment_to_batch(self, cv: dict, method: str, id_to: dict) -> Batch:
        """id_to maps local comment id -> (ap_id, author_uid); filled by thread()."""
        b = Batch()
        c, creator, post = cv["comment"], cv["creator"], cv["post"]
        if c.get("deleted") or c.get("removed") or creator.get("bot_account"):
            return b
        a_uid = self.uid(creator["actor_id"])
        c_uid = self.uid(c["ap_id"])
        root_uid = self.uid(post["ap_id"])
        path = c.get("path", "0").split(".")
        parent_local = int(path[-2]) if len(path) > 2 else None
        if parent_local and parent_local in id_to:
            parent_uid, parent_author = self.uid(id_to[parent_local][0]), id_to[parent_local][1]
        else:
            parent_uid, parent_author = root_uid, None
        created = parse_ts(c.get("published"))
        counts = cv.get("counts") or {}
        b.users.append(self._user(creator))
        b.posts.append(PostRec(
            post_uid=c_uid, platform=self.platform, author_uid=a_uid,
            text=self.p.rewrite_mentions(self.platform, c.get("content", "")), created_at=created,
            parent_post_uid=parent_uid, root_post_uid=root_uid, like_count=counts.get("score"),
            reply_count=counts.get("child_count"), source_method=method,
        ))
        if parent_author and parent_author != a_uid:
            b.edges.append(EdgeRec(a_uid, parent_author, self.platform, "reply", c_uid, created))
        b.discovered_users.append((acct_of(creator), a_uid))
        return b

    # ----------------------------------------------------------------- API
    def search(self, query: str, kind: str, limit: int = 200) -> Batch:
        b = Batch()
        if kind == "account":
            raw = self.resolve(kind, query)
            if raw:
                b.discovered_users.append((raw, self.uid(f"https://{raw.split('@')[1]}/u/{raw.split('@')[0]}")))
            return b
        page, n = 1, 0
        while n < limit:
            if kind == "community":
                data = self._get("post/list", {"community_name": query.lstrip("!"), "sort": "TopAll",
                                               "limit": 50, "page": page})
            else:
                data = self._get("search", {"q": query, "type_": "Posts", "sort": "TopAll",
                                            "limit": 50, "page": page})
            posts = (data or {}).get("posts", [])
            if not posts:
                break
            for pv in posts:
                if self.in_range(parse_ts(pv["post"].get("published"))):
                    b.extend(self.post_to_batch(pv, "search"))
            n += len(posts)
            page += 1
        return b

    def resolve(self, kind: str, value: str) -> str | None:
        v = value.strip().lstrip("@")
        if "@" not in v:
            v = f"{v}@{self.instance}"
        data = self._get("user", {"username": v, "limit": 1})
        return acct_of(data["person_view"]["person"]) if data else None

    def sample(self, raw_id: str) -> dict:
        data = self._user_page(raw_id, sort="New", limit=15)
        if not data:
            return {"bio": "", "sample": []}
        pv = data["person_view"]
        if pv["person"].get("bot_account"):
            return {"bio": "", "sample": [], "opted_out": True}
        texts = [p["post"]["name"] for p in data.get("posts", [])] + \
                [c["comment"]["content"] for c in data.get("comments", [])]
        counts = pv.get("counts", {})
        return {"bio": pv["person"].get("bio") or "", "sample": [t for t in texts if t][:8],
                "posts_count": (counts.get("post_count") or 0) + (counts.get("comment_count") or 0)}

    def timeline(self, raw_id: str, max_posts: int = 3000) -> Batch:
        b = Batch()
        page, n = 1, 0
        while n < max_posts:
            data = self._user_page(raw_id, sort="New", limit=50, page=page)
            if not data:
                break
            if page == 1:
                pv = data["person_view"]
                b.users.append(self._user(pv["person"], pv.get("counts")))
            posts, comments = data.get("posts", []), data.get("comments", [])
            if not posts and not comments:
                break
            oldest = None
            for pv in posts:
                ts = parse_ts(pv["post"].get("published"))
                oldest = min(oldest or ts, ts) if ts else oldest
                if self.in_range(ts):
                    b.extend(self.post_to_batch(pv, "timeline"))
            for cv in comments:
                ts = parse_ts(cv["comment"].get("published"))
                oldest = min(oldest or ts, ts) if ts else oldest
                if self.in_range(ts):
                    cb = self.comment_to_batch(cv, "timeline", {})
                    if cb.posts:
                        cb.posts[0].parent_post_uid = None  # exact parent is resolved by thread()
                    b.extend(cb)
                    b.discovered_roots.append((f"{self.instance}|{cv['post']['id']}", self.uid(cv["post"]["ap_id"])))
            n += len(posts) + len(comments)
            page += 1
            if oldest and oldest < self.s.date_from:
                break
        return b

    def thread(self, raw_root: str) -> Batch:
        b = Batch()
        inst, pid = raw_root.split("|", 1)
        post = self._get("post", {"id": pid})
        if not post:
            return b
        b.extend(self.post_to_batch(post["post_view"], "thread"))
        b.discovered_roots = []
        id_to: dict[int, tuple[str, str]] = {}
        page, rows, seen = 1, [], set()
        while page <= 20:
            data = self._get("comment/list", {"post_id": pid, "max_depth": 50, "limit": 50, "page": page,
                                              "sort": "Old", "type_": "All"})
            new = [cv for cv in (data or {}).get("comments", []) if cv["comment"]["id"] not in seen]
            if not new:
                break  # with max_depth some servers return the whole tree on every page
            seen.update(cv["comment"]["id"] for cv in new)
            rows += new
            page += 1
        rows.sort(key=lambda cv: len(cv["comment"].get("path", "").split(".")))  # parents first
        for cv in rows:
            cb = self.comment_to_batch(cv, "thread", id_to)
            if cb.posts:
                id_to[cv["comment"]["id"]] = (cv["comment"]["ap_id"], cb.posts[0].author_uid)
            b.extend(cb)
        return b
