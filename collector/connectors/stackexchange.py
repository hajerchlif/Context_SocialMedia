"""Stack Exchange network (Stack Overflow + ~170 topic sites) via the official API 2.3.

Free. Without a key: 300 requests/day per IP. With a free key (STACKEXCHANGE_KEY, register an app at
https://stackapps.com/apps/oauth/register): 10,000 requests/day. Content is CC BY-SA.

Each user's top tags are stored in users.interest_tags — a ground truth for interest profiles.
Raw ids:  user -> "site|user_id"     root -> "site|question_id"
Seeds:    kind "tag" with value "site:tag" (e.g. "politics:elections") or "site" (e.g. "sustainability")
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import re
import time

from ..http import Blocked, PoliteClient, QuotaExhausted
from ..models import Batch, EdgeRec, PostRec, UserRec
from .base import Connector

log = logging.getLogger(__name__)
API = "https://api.stackexchange.com/2.3"
_TAG = re.compile(r"<[^>]+>")

# domain -> default sites (used when Qwen proposes only tags)
SITES = {
    "climate_environment": ["sustainability", "earthscience"],
    "ai_tech": ["ai", "datascience", "stackoverflow"],
    "health_wellbeing": ["health", "fitness", "medicalsciences"],
    "politics_society": ["politics", "law"],
    "sports": ["sports", "outdoors"],
    "culture_entertainment": ["movies", "music", "scifi", "gaming", "literature"],
}


def _ts(v) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(v), tz=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def strip(s: str | None) -> str:
    return html.unescape(_TAG.sub(" ", s or "")).strip()


class StackExchangeConnector(Connector):
    platform = "stackexchange"
    query_kinds = ("tag", "site", "query")

    def __init__(self, settings, pseudo):
        super().__init__(settings, pseudo)
        self.key = getattr(settings, "stackexchange_key", "")
        self.http = PoliteClient(settings.user_agent, rps=2)
        self.quota_left = None

    def enabled(self):
        return (getattr(self.s, "enable_stackexchange", True), "ENABLE_STACKEXCHANGE=false")

    def _get(self, path: str, params: dict):
        if self.quota_left is not None and self.quota_left < 5:
            raise QuotaExhausted("Stack Exchange daily quota exhausted (add a free STACKEXCHANGE_KEY for 10k/day)")
        p = {**params, "filter": params.get("filter", "withbody")}
        if self.key:
            p["key"] = self.key
        r = self.http.get(f"{API}/{path}", params=p)
        if r.status_code == 400 and ("throttle" in r.text or "quota" in r.text):
            raise QuotaExhausted("Stack Exchange quota/throttle reached")
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        j = r.json()
        self.quota_left = j.get("quota_remaining", self.quota_left)
        if j.get("backoff"):
            time.sleep(int(j["backoff"]))
        return j

    # ----------------------------------------------------------- normalise
    def _owner_uid(self, owner: dict) -> str | None:
        if not owner or owner.get("user_type") == "does_not_exist":
            return None
        # account_id is shared across all Stack Exchange sites -> one person = one pseudonym
        return self.uid(f"acct:{owner.get('account_id') or owner.get('user_id')}")

    def _question(self, q: dict, site: str, method: str) -> Batch:
        b = Batch()
        a_uid = self._owner_uid(q.get("owner"))
        if not a_uid:
            return b
        p_uid = self.uid(f"{site}:q:{q['question_id']}")
        b.users.append(UserRec(a_uid, self.platform))
        b.posts.append(PostRec(
            post_uid=p_uid, platform=self.platform, author_uid=a_uid,
            text=self.p.rewrite_mentions(self.platform,
                                         f"{html.unescape(q.get('title', ''))}\n\n{strip(q.get('body'))}".strip()),
            created_at=_ts(q.get("creation_date")), hashtags=[t.lower() for t in q.get("tags", [])],
            root_post_uid=p_uid, like_count=q.get("score"), reply_count=q.get("answer_count"),
            community=site, source_method=method,
        ))
        b.discovered_users.append((f"{site}|{q['owner']['user_id']}", a_uid))
        if (q.get("answer_count") or 0) > 0 or (q.get("comment_count") or 0) > 0:
            b.discovered_roots.append((f"{site}|{q['question_id']}", p_uid))
        return b

    def _answer(self, a: dict, site: str, q_uid: str, q_author: str | None, method: str) -> Batch:
        b = Batch()
        a_uid = self._owner_uid(a.get("owner"))
        if not a_uid:
            return b
        p_uid = self.uid(f"{site}:a:{a['answer_id']}")
        created = _ts(a.get("creation_date"))
        b.users.append(UserRec(a_uid, self.platform))
        b.posts.append(PostRec(
            post_uid=p_uid, platform=self.platform, author_uid=a_uid,
            text=self.p.rewrite_mentions(self.platform, strip(a.get("body"))), created_at=created,
            parent_post_uid=q_uid, root_post_uid=q_uid, like_count=a.get("score"), community=site,
            source_method=method,
        ))
        if q_author and q_author != a_uid:
            b.edges.append(EdgeRec(a_uid, q_author, self.platform, "reply", p_uid, created))
        b.discovered_users.append((f"{site}|{a['owner']['user_id']}", a_uid))
        return b

    # ----------------------------------------------------------------- API
    def _site_tag(self, query: str, kind: str) -> list[tuple[str, str | None]]:
        q = query.strip().lower()
        if ":" in q:
            site, tag = q.split(":", 1)
            return [(site.strip(), tag.strip().replace(" ", "-") or None)]
        if kind == "site":
            return [(q, None)]
        return [("stackoverflow", q.replace(" ", "-"))]

    def supports_window(self, kind: str) -> bool:
        return True

    def search_window(self, query: str, kind: str, since, until, limit: int) -> Batch:
        return self.search(query, kind, limit, since, until)

    def search(self, query: str, kind: str, limit: int = 200, since=None, until=None) -> Batch:
        since, until = since or self.s.date_from, until or self.s.date_to
        b = Batch()
        for site, tag in self._site_tag(query, kind):
            page, n = 1, 0
            while n < limit:
                params = {"site": site, "pagesize": min(100, limit), "page": page, "sort": "votes", "order": "desc",
                          "fromdate": int(since.timestamp()), "todate": int(until.timestamp())}
                if kind == "query":
                    params["q"] = query.split(":", 1)[-1]
                    data = self._get("search/advanced", params)
                else:
                    if tag:
                        params["tagged"] = tag
                    data = self._get("questions", params)
                items = (data or {}).get("items", [])
                for q in items:
                    b.extend(self._question(q, site, "search"))
                n += len(items)
                page += 1
                if not data or not data.get("has_more"):
                    break
        return b

    def resolve(self, kind: str, value: str) -> str | None:
        return value if "|" in value else None

    def sample(self, raw_id: str) -> dict:
        site, uid = raw_id.split("|", 1)
        u = self._get(f"users/{uid}", {"site": site, "filter": "default"})
        tags = self._get(f"users/{uid}/top-tags", {"site": site, "pagesize": 10, "filter": "default"})
        user = ((u or {}).get("items") or [{}])[0]
        tag_names = [t["tag_name"] for t in (tags or {}).get("items", [])]
        posts = self._get(f"users/{uid}/questions", {"site": site, "pagesize": 8, "sort": "creation",
                                                       "filter": "default"})
        titles = [p.get("title", "") for p in (posts or {}).get("items", [])]
        return {"bio": (f"top tags: {', '.join(tag_names)}" if tag_names else "")
                       + f" | reputation {user.get('reputation', 0)}",
                "sample": (titles + [f"answers about: {t}" for t in tag_names])[:8]}

    def timeline(self, raw_id: str, max_posts: int = 3000) -> Batch:
        b = Batch()
        site, uid = raw_id.split("|", 1)
        u = ((self._get(f"users/{uid}", {"site": site, "filter": "default"}) or {}).get("items") or [None])[0]
        if not u:
            return b
        a_uid = self._owner_uid(u)
        tags = self._get(f"users/{uid}/top-tags", {"site": site, "pagesize": 20, "filter": "default"})
        b.users.append(UserRec(a_uid, self.platform, account_created_at=_ts(u.get("creation_date")),
                               interest_tags=[f"{site}:{t['tag_name']}" for t in (tags or {}).get("items", [])]))
        since = int(self.s.date_from.timestamp())
        for kind in ("questions", "answers"):
            page, n = 1, 0
            while n < max_posts // 2:
                data = self._get(f"users/{uid}/{kind}", {"site": site, "pagesize": 100, "page": page,
                                                          "fromdate": since, "sort": "creation"})
                items = (data or {}).get("items", [])
                for it in items:
                    if kind == "questions":
                        b.extend(self._question(it, site, "timeline"))
                    else:
                        q_uid = self.uid(f"{site}:q:{it['question_id']}")
                        b.extend(self._answer(it, site, q_uid, None, "timeline"))
                        b.discovered_roots.append((f"{site}|{it['question_id']}", q_uid))
                n += len(items)
                page += 1
                if not data or not data.get("has_more"):
                    break
        return b

    def thread(self, raw_root: str) -> Batch:
        b = Batch()
        site, qid = raw_root.split("|", 1)
        q = ((self._get(f"questions/{qid}", {"site": site}) or {}).get("items") or [None])[0]
        if not q:
            return b
        qb = self._question(q, site, "thread")
        qb.discovered_roots = []
        b.extend(qb)
        q_uid = self.uid(f"{site}:q:{qid}")
        q_author = qb.posts[0].author_uid if qb.posts else None
        answers = self._get(f"questions/{qid}/answers", {"site": site, "pagesize": 100, "sort": "votes"})
        authors = {}
        for a in (answers or {}).get("items", []):
            ab = self._answer(a, site, q_uid, q_author, "thread")
            if ab.posts:
                authors[a["answer_id"]] = ab.posts[0].author_uid
            b.extend(ab)
        # comments on the question and on its answers (short replies, often the real discussion)
        ids = ";".join([str(qid)] + [str(a) for a in list(authors)[:99]])
        comments = self._get(f"posts/{ids}/comments", {"site": site, "pagesize": 100})
        for c in (comments or {}).get("items", []):
            c_author = self._owner_uid(c.get("owner"))
            if not c_author:
                continue
            parent = (self.uid(f"{site}:q:{qid}") if c["post_id"] == int(qid)
                      else self.uid(f"{site}:a:{c['post_id']}"))
            parent_author = q_author if c["post_id"] == int(qid) else authors.get(c["post_id"])
            c_uid = self.uid(f"{site}:c:{c['comment_id']}")
            created = _ts(c.get("creation_date"))
            b.users.append(UserRec(c_author, self.platform))
            b.posts.append(PostRec(
                post_uid=c_uid, platform=self.platform, author_uid=c_author,
                text=self.p.rewrite_mentions(self.platform, strip(c.get("body"))), created_at=created,
                parent_post_uid=parent, root_post_uid=q_uid, like_count=c.get("score"), community=site,
                source_method="thread",
            ))
            if parent_author and parent_author != c_author:
                b.edges.append(EdgeRec(c_author, parent_author, self.platform, "reply", c_uid, created))
        return b
