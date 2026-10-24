"""YouTube Data API v3 with a FREE key (10,000 units/day). Videos = roots, comments = replies.

search.list costs 100 units (≈100 searches/day), commentThreads.list costs 1 unit.
"""
from __future__ import annotations

import logging

from ..http import PoliteClient
from ..models import Batch, EdgeRec, PostRec, UserRec, parse_ts
from .base import Connector

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/youtube/v3"


class YouTubeConnector(Connector):
    platform = "youtube"
    query_kinds = ("query",)

    def __init__(self, settings, pseudo):
        super().__init__(settings, pseudo)
        self.http = PoliteClient(settings.user_agent, rps=2)

    def enabled(self):
        return (bool(self.s.youtube_api_key), "set YOUTUBE_API_KEY (free)")

    def _get(self, path: str, params: dict):
        r = self.http.get(f"{API}/{path}", params={**params, "key": self.s.youtube_api_key})
        if r.status_code == 403:
            log.warning("YouTube quota/permission error: %s", r.text[:200])
            return None
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def search(self, query: str, kind: str, limit: int = 50) -> Batch:
        b = Batch()
        data = self._get("search", {
            "part": "snippet", "q": query, "type": "video", "maxResults": min(50, limit), "order": "relevance",
            "publishedAfter": self.s.date_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "publishedBefore": self.s.date_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "relevanceLanguage": (self.s.langs or ["en"])[0],
        })
        for it in (data or {}).get("items", []):
            vid = it["id"].get("videoId")
            sn = it["snippet"]
            if not vid:
                continue
            ch_uid = self.uid(sn["channelId"])
            p_uid = self.uid(f"video:{vid}")
            b.users.append(UserRec(ch_uid, self.platform))
            b.posts.append(PostRec(
                post_uid=p_uid, platform=self.platform, author_uid=ch_uid,
                text=f"{sn.get('title', '')}\n\n{sn.get('description', '')}".strip(),
                created_at=parse_ts(sn.get("publishedAt")), media_type="video", root_post_uid=p_uid,
                community=sn.get("channelTitle"), source_method="search",
            ))
            b.discovered_roots.append((vid, p_uid))
        return b

    def thread(self, raw_root: str, max_comments: int = 1000) -> Batch:
        b = Batch()
        root_uid = self.uid(f"video:{raw_root}")
        token, n = None, 0
        while n < max_comments:
            params = {"part": "snippet,replies", "videoId": raw_root, "maxResults": 100, "textFormat": "plainText"}
            if token:
                params["pageToken"] = token
            data = self._get("commentThreads", params)
            if not data:
                break
            for th in data.get("items", []):
                top = th["snippet"]["topLevelComment"]
                tp = self._comment(top, root_uid, root_uid)
                if tp:
                    b.extend(tp)
                for rep in (th.get("replies") or {}).get("comments", []):
                    rp = self._comment(rep, self.uid(f"comment:{top['id']}"), root_uid)
                    if rp:
                        b.extend(rp)
                n += 1
            token = data.get("nextPageToken")
            if not token:
                break
        return b

    def _comment(self, c: dict, parent_uid: str, root_uid: str) -> Batch | None:
        sn = c["snippet"]
        ch = (sn.get("authorChannelId") or {}).get("value")
        if not ch:
            return None
        b = Batch()
        a_uid = self.uid(ch)
        p_uid = self.uid(f"comment:{c['id']}")
        created = parse_ts(sn.get("publishedAt"))
        if not self.in_range(created):
            return None
        b.users.append(UserRec(a_uid, self.platform))
        b.posts.append(PostRec(
            post_uid=p_uid, platform=self.platform, author_uid=a_uid,
            text=self.p.rewrite_mentions(self.platform, sn.get("textOriginal") or sn.get("textDisplay", "")),
            created_at=created, parent_post_uid=parent_uid, root_post_uid=root_uid,
            like_count=sn.get("likeCount"), reply_count=None, source_method="thread",
        ))
        return b  # reply edges are derived later from parent_post_uid -> author_uid
