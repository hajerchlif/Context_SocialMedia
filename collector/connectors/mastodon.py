"""Mastodon / fediverse via public REST API (free). Respects `indexable`, `discoverable`, `noindex`.

Raw ids used in the queue:  user  -> "instance|local_account_id"
                            root  -> "instance|local_status_id"
Pseudonyms use stable global identifiers: full acct (user@host) and the status URI.
"""
from __future__ import annotations

import html
import logging
import re

from ..http import Blocked, PoliteClient
from ..models import Batch, EdgeRec, PostRec, UserRec, parse_ts
from .base import Connector

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>|</p>\s*<p>", re.I)


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)
    return html.unescape(_TAG_RE.sub("", s)).strip()


class MastodonConnector(Connector):
    platform = "mastodon"
    query_kinds = ("hashtag", "account")

    def __init__(self, settings, pseudo):
        super().__init__(settings, pseudo)
        self.http = PoliteClient(settings.user_agent, rps=1.5)
        self.instances = [i.strip() for i in settings.mastodon_instances if i.strip()]

    def enabled(self):
        return (bool(self.instances), "set MASTODON_INSTANCES")

    def _get(self, instance: str, path: str, params: dict | None = None):
        r = self.http.get(f"https://{instance}{path}", params=params)
        if r.status_code in (401, 403, 404, 410, 422):
            return None
        r.raise_for_status()
        return r.json()

    @staticmethod
    def full_acct(acct: dict, instance: str) -> str:
        a = acct.get("acct", "")
        return a if "@" in a else f"{a}@{instance}"

    @staticmethod
    def opted_out(acct: dict) -> bool:
        return (acct.get("indexable") is False or acct.get("discoverable") is False
                or acct.get("noindex") is True or "#nobot" in (acct.get("note") or "").lower())

    def _user(self, acct: dict, instance: str) -> UserRec:
        return UserRec(
            user_uid=self.uid(self.full_acct(acct, instance)), platform=self.platform,
            bio=self.p.rewrite_mentions(self.platform, strip_html(acct.get("note", ""))),
            account_created_at=parse_ts(acct.get("created_at")),
            followers_count=acct.get("followers_count"), following_count=acct.get("following_count"),
            posts_count=acct.get("statuses_count"), is_bot_flag=bool(acct.get("bot")),
        )

    def status_to_batch(self, st: dict, instance: str, method: str, id_to_uri: dict | None = None) -> Batch:
        b = Batch()
        id_to_uri = id_to_uri or {}
        acct = st.get("account") or {}
        if self.opted_out(acct):
            return b
        if st.get("reblog"):
            inner = self.status_to_batch(st["reblog"], instance, method, id_to_uri)
            if inner.posts:
                src = self.uid(self.full_acct(acct, instance))
                orig = inner.posts[0]
                inner.edges.append(EdgeRec(src, orig.author_uid, self.platform, "repost", orig.post_uid,
                                           parse_ts(st.get("created_at"))))
            return inner
        a_full = self.full_acct(acct, instance)
        a_uid = self.uid(a_full)
        uri = st.get("uri") or st.get("url")
        p_uid = self.uid(uri)
        created = parse_ts(st.get("created_at"))
        known = {}
        mention_uids = []
        for m in st.get("mentions", []) or []:
            full = m["acct"] if "@" in m["acct"] else f"{m['acct']}@{instance}"
            known[m.get("username", "")] = full
            known[m["acct"]] = full
            mention_uids.append(self.uid(full))
            b.discovered_users.append((f"{instance}|{m['id']}", self.uid(full)))
        text = self.p.rewrite_mentions(self.platform, strip_html(st.get("content", "")), known)
        if st.get("spoiler_text"):
            text = f"[CW: {st['spoiler_text']}] {text}"
        media = st.get("media_attachments") or []
        parent_id = st.get("in_reply_to_id")
        parent_uri = id_to_uri.get(parent_id) if parent_id else None
        card = st.get("card") or {}
        b.users.append(self._user(acct, instance))
        b.posts.append(PostRec(
            post_uid=p_uid, platform=self.platform, author_uid=a_uid, text=text, created_at=created,
            lang=st.get("language"), hashtags=sorted({t["name"].lower() for t in st.get("tags", []) or []}),
            mentions_uid=mention_uids, urls=[card["url"]] if card.get("url") else [],
            media_type=media[0]["type"] if media else ("link" if card else None),
            media_alt_text=" | ".join(m["description"] for m in media if m.get("description")) or None,
            parent_post_uid=self.uid(parent_uri) if parent_uri else None,
            root_post_uid=None if parent_id else p_uid,
            like_count=st.get("favourites_count"), repost_count=st.get("reblogs_count"),
            reply_count=st.get("replies_count"), source_method=method,
        ))
        for m in mention_uids:
            b.edges.append(EdgeRec(a_uid, m, self.platform, "mention", p_uid, created))
        if parent_id or (st.get("replies_count") or 0) > 0:
            b.discovered_roots.append((f"{instance}|{st['id']}", p_uid))
        return b

    # ------------------------------------------------------------------- API
    @staticmethod
    def snowflake(ts) -> str:
        """Mastodon status ids are time-based: (unix_ms << 16). Lets us jump to any date."""
        return str(int(ts.timestamp() * 1000) << 16)

    def supports_window(self, kind: str) -> bool:
        return kind == "hashtag"

    def search_window(self, query: str, kind: str, since, until, limit: int) -> Batch:
        """Tag timeline restricted to [since, until), spread over 3 sub-windows so posts are not all
        from the last days of the month. Only posts we keep (indexable accounts) count towards limit."""
        b = Batch()
        tag = query.lstrip("#").replace(" ", "")
        step = (until - since) / 3
        subs = [(since + step * i, since + step * (i + 1)) for i in range(3)]
        per_sub = max(1, limit // (3 * max(1, len(self.instances))))
        for inst in self.instances:
            for s_from, s_to in subs:
                max_id, n, pages = self.snowflake(s_to), 0, 0
                while n < per_sub and pages < 8:
                    data = self._get(inst, f"/api/v1/timelines/tag/{tag}", {"limit": 40, "max_id": max_id})
                    pages += 1
                    if not data:
                        break
                    stop = False
                    for st in data:
                        ts = parse_ts(st.get("created_at"))
                        if ts and ts < s_from:
                            stop = True
                            break
                        sb = self.status_to_batch(st, inst, "search")
                        if sb.posts:
                            acct = (st.get("reblog") or st)["account"]
                            sb.discovered_users.append((f"{inst}|{acct['id']}", sb.posts[0].author_uid))
                            b.extend(sb)
                            n += 1
                            if n >= per_sub:
                                break
                    max_id = data[-1]["id"]
                    if stop:
                        break
        return b

    def search(self, query: str, kind: str, limit: int = 200) -> Batch:
        b = Batch()
        if kind == "account":
            raw = self.resolve(kind, query)
            if raw:
                acct = query.strip().lstrip("@")
                full = acct if "@" in acct else f"{acct}@{self.instances[0]}"
                b.discovered_users.append((raw, self.uid(full)))
            return b
        tag = query.lstrip("#").replace(" ", "")
        for inst in self.instances:
            max_id, n = None, 0
            while n < limit:
                params = {"limit": 40}
                if max_id:
                    params["max_id"] = max_id
                data = self._get(inst, f"/api/v1/timelines/tag/{tag}", params)
                if not data:
                    break
                stop = False
                for st in data:
                    ts = parse_ts(st.get("created_at"))
                    if ts and ts < self.s.date_from:
                        stop = True
                        break
                    sb = self.status_to_batch(st, inst, "search")
                    if sb.posts:
                        acct = (st.get("reblog") or st)["account"]
                        sb.discovered_users.append((f"{inst}|{acct['id']}", sb.posts[0].author_uid))
                    b.extend(sb)
                    n += 1
                max_id = data[-1]["id"]
                if stop:
                    break
        return b

    def resolve(self, kind: str, value: str) -> str | None:
        acct = value.strip().lstrip("@")
        host = acct.split("@", 1)[1] if "@" in acct else self.instances[0]
        data = self._get(host, "/api/v1/accounts/lookup", {"acct": acct.split("@")[0]})
        if not data or self.opted_out(data):
            return None
        return f"{host}|{data['id']}"

    def _account(self, raw_id: str) -> tuple[str, dict | None]:
        inst, aid = raw_id.split("|", 1)
        return inst, self._get(inst, f"/api/v1/accounts/{aid}")

    def sample(self, raw_id: str) -> dict:
        inst, acct = self._account(raw_id)
        if not acct or self.opted_out(acct):
            return {"bio": "", "sample": [], "opted_out": True}
        sts = self._get(inst, f"/api/v1/accounts/{acct['id']}/statuses",
                        {"limit": 15, "exclude_reblogs": "true"}) or []
        return {"bio": strip_html(acct.get("note", "")), "posts_count": acct.get("statuses_count", 0),
                "sample": [strip_html(s.get("content", "")) for s in sts if s.get("content")][:8]}

    def timeline(self, raw_id: str, max_posts: int = 3000) -> Batch:
        b = Batch()
        inst, acct = self._account(raw_id)
        if not acct or self.opted_out(acct):
            return b
        b.users.append(self._user(acct, inst))
        max_id, n = None, 0
        while n < max_posts:
            params = {"limit": 40}
            if max_id:
                params["max_id"] = max_id
            data = self._get(inst, f"/api/v1/accounts/{acct['id']}/statuses", params)
            if not data:
                break
            stop = False
            for st in data:
                ts = parse_ts(st.get("created_at"))
                if ts and ts < self.s.date_from:
                    stop = True
                    break
                if ts and ts > self.s.date_to:
                    continue
                b.extend(self.status_to_batch(st, inst, "timeline"))
                n += 1
            max_id = data[-1]["id"]
            if stop:
                break
        return b

    def follows(self, raw_id: str, limit: int = 400) -> Batch:
        b = Batch()
        inst, aid = raw_id.split("|", 1)
        src_acct = self._get(inst, f"/api/v1/accounts/{aid}")
        if not src_acct:
            return b
        src = self.uid(self.full_acct(src_acct, inst))
        max_id, n = None, 0
        while n < limit:
            params = {"limit": 80}
            if max_id:
                params["max_id"] = max_id
            r = self.http.get(f"https://{inst}/api/v1/accounts/{aid}/following", params=params)
            if r.status_code != 200:
                break
            data = r.json()
            for acct in data:
                if self.opted_out(acct):
                    continue
                u = self.uid(self.full_acct(acct, inst))
                b.discovered_users.append((f"{inst}|{acct['id']}", u))
                b.edges.append(EdgeRec(src, u, self.platform, "follow"))
                n += 1
            m = re.search(r"max_id=(\d+)[^>]*>;\s*rel=\"next\"", r.headers.get("link", ""))
            if not data or not m:
                break
            max_id = m.group(1)
        return b

    def thread(self, raw_root: str) -> Batch:
        b = Batch()
        inst, sid = raw_root.split("|", 1)
        st = self._get(inst, f"/api/v1/statuses/{sid}")
        ctx = self._get(inst, f"/api/v1/statuses/{sid}/context")
        if not st or ctx is None:
            return b
        allst = (ctx.get("ancestors") or []) + [st] + (ctx.get("descendants") or [])
        id_to_uri = {s["id"]: s.get("uri") or s.get("url") for s in allst}
        root_uri = id_to_uri[allst[0]["id"]]
        for s in allst:
            sb = self.status_to_batch(s, inst, "thread", id_to_uri)
            for p in sb.posts:
                p.root_post_uid = self.uid(root_uri)
            sb.discovered_roots = []
            parent = s.get("in_reply_to_id")
            if parent and sb.posts:
                pa = next((x for x in allst if x["id"] == parent), None)
                if pa:
                    sb.edges.append(EdgeRec(sb.posts[0].author_uid, self.uid(self.full_acct(pa["account"], inst)),
                                            self.platform, "reply", sb.posts[0].post_uid, sb.posts[0].created_at))
            b.extend(sb)
        return b
