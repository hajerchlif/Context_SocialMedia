"""Telegram public channels via the public web preview t.me/s/<channel> (no account, no login).

Only public broadcast channels expose this page. Comments/private groups are NOT collected.
robots.txt is respected; if the page is unavailable we skip the channel.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..http import Blocked, PoliteClient
from ..models import Batch, PostRec, UserRec, parse_ts
from .base import Connector

log = logging.getLogger(__name__)


def _count(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip().upper().replace(",", ".")
    mult = 1000 if s.endswith("K") else 1_000_000 if s.endswith("M") else 1
    try:
        return int(float(s.rstrip("KM")) * mult)
    except ValueError:
        return None


class TelegramWebConnector(Connector):
    platform = "telegram"
    query_kinds = ("channel",)

    def __init__(self, settings, pseudo):
        super().__init__(settings, pseudo)
        self.http = PoliteClient(settings.user_agent, rps=0.5, respect_robots=True)

    def enabled(self):
        return (self.s.enable_telegram_web, "ENABLE_TELEGRAM_WEB=false")

    def parse_page(self, html: str, channel: str) -> tuple[Batch, int | None]:
        b = Batch()
        soup = BeautifulSoup(html, "html.parser")
        ch_uid = self.uid(channel)
        desc = soup.select_one(".tgme_channel_info_description")
        subs = None
        for c in soup.select(".tgme_channel_info_counter"):
            if "subscriber" in c.get_text():
                subs = _count(c.select_one(".counter_value").get_text() if c.select_one(".counter_value") else None)
        b.users.append(UserRec(user_uid=ch_uid, platform=self.platform,
                               bio=self.p.rewrite_mentions(self.platform, desc.get_text("\n") if desc else ""),
                               followers_count=subs))
        oldest = None
        for msg in soup.select("div.tgme_widget_message[data-post]"):
            post_ref = msg["data-post"]  # channel/123
            try:
                mid = int(post_ref.rsplit("/", 1)[1])
            except ValueError:
                continue
            oldest = mid if oldest is None else min(oldest, mid)
            t = msg.select_one("time[datetime]")
            ts = parse_ts(t["datetime"]) if t else None
            if not self.in_range(ts):
                continue
            txt_el = msg.select_one(".tgme_widget_message_text")
            text = txt_el.get_text("\n") if txt_el else ""
            urls = [a["href"] for a in (txt_el.select("a[href]") if txt_el else []) if a["href"].startswith("http")]
            hashtags = sorted({h.lower() for h in re.findall(r"#(\w+)", text)})
            media = "image" if msg.select_one(".tgme_widget_message_photo_wrap") else \
                    "video" if msg.select_one(".tgme_widget_message_video_player") else None
            reply = msg.select_one("a.tgme_widget_message_reply[href]")
            parent = None
            if reply:
                m = re.search(r"t\.me/([^/]+)/(\d+)", reply["href"])
                parent = self.uid(f"{m.group(1)}/{m.group(2)}") if m else None
            p_uid = self.uid(post_ref)
            b.posts.append(PostRec(
                post_uid=p_uid, platform=self.platform, author_uid=ch_uid,
                text=self.p.rewrite_mentions(self.platform, text), created_at=ts, hashtags=hashtags, urls=urls,
                media_type=media, parent_post_uid=parent, root_post_uid=parent or p_uid,
                like_count=None, repost_count=None, reply_count=None, community=channel,
                source_method="web_public",
            ))
        return b, oldest

    def timeline(self, raw_id: str, max_posts: int = 3000) -> Batch:
        b = Batch()
        before, n = None, 0
        while n < max_posts:
            url = f"https://t.me/s/{raw_id}" + (f"?before={before}" if before else "")
            try:
                r = self.http.get(url)
            except Blocked as e:
                log.info("telegram %s skipped: %s", raw_id, e)
                break
            if r.status_code != 200 or "tgme_widget_message" not in r.text:
                break
            page, oldest = self.parse_page(r.text, raw_id)
            if not b.users:
                b.users = page.users
            b.posts += page.posts
            n += len(page.posts)
            if oldest is None or oldest <= 1 or (page.posts == [] and before):
                break
            # stop when the page is entirely older than DATE_FROM
            times = [p.created_at for p in page.posts if p.created_at]
            if not page.posts and before:
                break
            if times and min(times) <= self.s.date_from:
                break
            before = oldest
        return b

    def search(self, query: str, kind: str, limit: int = 200) -> Batch:
        b = Batch()
        ch = query.strip().lstrip("@").replace("https://t.me/", "").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", ch):
            return b
        # the LLM can invent channel names: keep only channels whose public page really has messages
        try:
            r = self.http.get(f"https://t.me/s/{ch}")
        except Blocked:
            return b
        if r.status_code == 200 and "data-post=\"" in r.text:
            page, _ = self.parse_page(r.text, ch)
            b.users, b.posts = page.users, page.posts
            b.discovered_users.append((ch, self.uid(ch)))
        else:
            log.info("telegram channel %r does not exist or has no public page — ignored", ch)
        return b

    def resolve(self, kind: str, value: str) -> str | None:
        return value.strip().lstrip("@").replace("https://t.me/", "").strip("/") or None

    def sample(self, raw_id: str) -> dict:
        try:
            r = self.http.get(f"https://t.me/s/{raw_id}")
        except Blocked:
            return {"bio": "", "sample": []}
        if r.status_code != 200:
            return {"bio": "", "sample": []}
        page, _ = self.parse_page(r.text, raw_id)
        return {"bio": page.users[0].bio if page.users else "", "posts_count": len(page.posts),
                "sample": [p.text for p in page.posts if p.text][:8]}
