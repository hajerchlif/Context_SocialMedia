"""Telegram through the official API (MTProto, Telethon) — free, read-only, public chats only.

Why: the web preview t.me/s/<channel> has no keyword search, shows only broadcast channels and no
comments. With a (free) API id the collector can:
  * search public channels AND public discussion groups by keyword (contacts.Search) — real
    discovery, no more guessing channel names;
  * backfill the full history of each public chat from DATE_FROM (2023-01-01);
  * collect comments under channel posts and messages of public groups, i.e. real users.

Setup (once):
  1. https://my.telegram.org -> "API development tools" -> create an app -> api_id + api_hash
  2. put TELEGRAM_API_ID / TELEGRAM_API_HASH in .env
  3. docker compose run --rm -it collector python -m collector telegram-login
     (phone number + code sent by Telegram; the session is saved in data/telegram.session)

Rules kept here: read-only (never posts, joins, reacts or messages anyone), public chats only,
senders pseudonymised like every other platform, Telegram flood limits respected (FloodWait).
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from ..config import DATA_DIR
from ..http import QuotaExhausted
from ..models import Batch, EdgeRec, PostRec, UserRec
from .base import Connector
from .telegram_web import linked_channels

log = logging.getLogger(__name__)

_CLIENT = None  # one MTProto connection per process


def session_path(settings) -> str:
    return settings.telegram_session or str(DATA_DIR / "telegram")


def get_client(settings):
    """Connected, authorised Telethon client, or None (no credentials / not logged in yet)."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        return None
    from telethon.sync import TelegramClient

    client = TelegramClient(session_path(settings), int(settings.telegram_api_id), settings.telegram_api_hash,
                            flood_sleep_threshold=300, device_model="ContextSocialMediaResearch",
                            timeout=15, connection_retries=2, retry_delay=2)
    client.connect()
    if not client.is_user_authorized():
        client.disconnect()
        return None
    _CLIENT = client
    return client


def login(settings) -> None:
    """Interactive one-time login (phone + code, and 2FA password if the account has one)."""
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first (https://my.telegram.org).")
    from telethon.sync import TelegramClient

    client = TelegramClient(session_path(settings), int(settings.telegram_api_id), settings.telegram_api_hash,
                            device_model="ContextSocialMediaResearch")
    client.start()  # prompts for phone, code and password in the terminal
    me = client.get_me()
    print(f"Telegram session ready ({session_path(settings)}.session) for account id {me.id}.")
    client.disconnect()


def _ts(d: dt.datetime | None) -> dt.datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


class TelegramApiConnector(Connector):
    platform = "telegram"
    query_kinds = ("query", "channel")
    entity = "channel"           # relevance gate judges chats on topic, not on being an individual
    llm_kind = "query"           # Qwen proposes keywords, Telegram's own search finds the chats
    llm_hint = "telegram_api"

    def __init__(self, settings, pseudo):
        super().__init__(settings, pseudo)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = get_client(self.s)
            if self._client is None:
                raise QuotaExhausted("Telegram API session missing — run: python -m collector telegram-login")
        return self._client

    def enabled(self):
        if not (self.s.telegram_api_id and self.s.telegram_api_hash):
            return False, "TELEGRAM_API_ID / TELEGRAM_API_HASH empty"
        try:
            ok = get_client(self.s) is not None
        except Exception as e:  # noqa: BLE001
            return False, f"Telegram API unavailable: {e}"
        return (True, "") if ok else (False, "not logged in — run: python -m collector telegram-login")

    # ------------------------------------------------------------ helpers
    def _call(self, fn, *args, **kw):
        from telethon import errors

        try:
            return fn(*args, **kw)
        except errors.FloodWaitError as e:  # longer than flood_sleep_threshold
            raise QuotaExhausted(f"Telegram flood wait {e.seconds}s") from e

    def _entity(self, username: str):
        from telethon import errors

        try:
            return self._call(self.client.get_entity, username)
        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError,
                errors.ChannelPrivateError):
            return None

    @staticmethod
    def _is_public_chat(ent) -> bool:
        return getattr(ent, "username", None) is not None and (
            getattr(ent, "broadcast", False) or getattr(ent, "megagroup", False))

    def _chat_user(self, ent) -> UserRec:
        return UserRec(user_uid=self.uid(ent.username), platform=self.platform,
                       bio=getattr(ent, "title", None),
                       followers_count=getattr(ent, "participants_count", None))

    def _post(self, msg, chat, b: Batch, root_uid: str | None = None, parent_uid: str | None = None,
              raw_prefix: str | None = None) -> PostRec | None:
        """Telethon Message -> PostRec. Authors: the channel itself (broadcast) or the pseudonymised
        sender (groups, comments)."""
        text = msg.message or ""
        ts = _ts(msg.date)
        if not self.in_range(ts):
            return None
        chat_name = chat.username or f"c{chat.id}"
        prefix = raw_prefix or chat_name
        sender = getattr(msg, "sender_id", None)
        if getattr(chat, "broadcast", False) or not sender or sender < 0:  # <0: posted as a channel
            author = self.uid(chat_name)
        else:
            author = self.uid(f"user:{sender}")
            if not any(u.user_uid == author for u in b.users):
                snd = getattr(msg, "sender", None)
                b.users.append(UserRec(user_uid=author, platform=self.platform,
                                       is_bot_flag=bool(getattr(snd, "bot", False)) if snd else None))
        p_uid = self.uid(f"{prefix}/{msg.id}")
        if parent_uid is None and msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
            parent_uid = self.uid(f"{prefix}/{msg.reply_to.reply_to_msg_id}")
        for other in linked_channels(text, chat_name):
            b.discovered_users.append((other, self.uid(other)))
        media = None
        if msg.photo:
            media = "image"
        elif msg.video:
            media = "video"
        replies = getattr(msg, "replies", None)
        p = PostRec(
            post_uid=p_uid, platform=self.platform, author_uid=author,
            text=self.p.rewrite_mentions(self.platform, text), created_at=ts,
            hashtags=sorted({h.lower() for h in re.findall(r"#(\w+)", text)}),
            urls=re.findall(r"https?://\S+", text)[:20], media_type=media,
            parent_post_uid=parent_uid, root_post_uid=root_uid or parent_uid or p_uid,
            like_count=None, repost_count=getattr(msg, "forwards", None),
            reply_count=getattr(replies, "replies", None) if replies else None,
            community=chat.username, source_method="telegram_api",
        )
        if parent_uid and author != parent_uid:
            b.edges.append(EdgeRec(src_uid=author, dst_uid=parent_uid, platform=self.platform,
                                   type="reply", post_uid=p_uid, created_at=ts))
        return p

    # ------------------------------------------------------------ interface
    def search(self, query: str, kind: str, limit: int = 200) -> Batch:
        b = Batch()
        if kind == "channel":
            ent = self._entity(query.strip().lstrip("@").replace("https://t.me/", "").strip("/"))
            chats = [ent] if ent is not None else []
        else:
            from telethon.tl.functions.contacts import SearchRequest

            res = self._call(self.client, SearchRequest(q=query, limit=50))
            chats = list(res.chats)
        for ch in chats:
            if not self._is_public_chat(ch):
                continue
            b.users.append(self._chat_user(ch))
            b.discovered_users.append((ch.username, self.uid(ch.username)))
        if not chats:
            log.info("telegram: nothing public found for %r", query)
        return b

    def resolve(self, kind: str, value: str) -> str | None:
        return value.strip().lstrip("@").replace("https://t.me/", "").strip("/") or None

    def sample(self, raw_id: str) -> dict:
        ent = self._entity(raw_id)
        if ent is None or not self._is_public_chat(ent):
            return {"bio": "", "sample": []}
        texts = [m.message for m in self._call(self.client.get_messages, ent, limit=30) if m.message][:8]
        kind = "group" if getattr(ent, "megagroup", False) else "channel"
        return {"bio": f"{ent.title} ({kind})", "posts_count": None, "kind": kind, "sample": texts}

    def timeline(self, raw_id: str, max_posts: int = 3000) -> Batch:
        """Whole history of a public channel/group, newest first, down to DATE_FROM."""
        b = Batch()
        ent = self._entity(raw_id)
        if ent is None or not self._is_public_chat(ent):
            return b
        b.users.append(self._chat_user(ent))
        offset = self.s.date_to if self.s.date_to < dt.datetime.now(dt.timezone.utc) else None
        n = 0
        for msg in self.client.iter_messages(ent, offset_date=offset, limit=max_posts, wait_time=1):
            ts = _ts(msg.date)
            if ts and ts < self.s.date_from:
                break
            if msg.action is not None:  # service messages (joins, pins...)
                continue
            p = self._post(msg, ent, b)
            if p is None:
                continue
            b.posts.append(p)
            n += 1
            if getattr(ent, "broadcast", False) and msg.replies and msg.replies.comments and msg.replies.replies:
                b.discovered_roots.append((f"{ent.username}/{msg.id}", p.post_uid))
        log.debug("telegram %s: %d messages", raw_id, n)
        return b

    def thread(self, raw_root: str) -> Batch:
        """Comments under a channel post (they live in the channel's linked discussion group)."""
        from telethon import errors

        b = Batch()
        username, _, mid = raw_root.rpartition("/")
        ent = self._entity(username)
        if ent is None or not mid.isdigit():
            return b
        root_uid = self.uid(raw_root)
        disc_prefix = None
        try:
            for msg in self.client.iter_messages(ent, reply_to=int(mid), limit=2000, wait_time=1):
                if msg.action is not None:
                    continue
                chat = msg.chat or ent
                disc_prefix = disc_prefix or f"c{getattr(chat, 'id', 'x')}"
                top = getattr(msg.reply_to, "reply_to_top_id", None) if msg.reply_to else None
                parent = (self.uid(f"{disc_prefix}/{msg.reply_to.reply_to_msg_id}")
                          if top and msg.reply_to.reply_to_msg_id != top else root_uid)
                p = self._post(msg, chat, b, root_uid=root_uid, parent_uid=parent, raw_prefix=disc_prefix)
                if p:
                    b.posts.append(p)
        except (errors.MsgIdInvalidError, errors.ChannelPrivateError) as e:
            log.debug("telegram comments %s unavailable: %s", raw_root, e)
        return b

    def follows(self, raw_id: str, limit: int = 200) -> Batch:
        """Snowball: public chats linked, @mentioned or forwarded from in recent messages."""
        b = Batch()
        ent = self._entity(raw_id)
        if ent is None:
            return b
        seen: dict[str, tuple[str, str]] = {}
        for msg in self._call(self.client.get_messages, ent, limit=300):
            for other in linked_channels(msg.message or "", raw_id):
                seen.setdefault(other.lower(), (other, self.uid(other)))
            fwd = getattr(msg, "forward", None)
            chat = getattr(fwd, "chat", None) if fwd else None
            if chat is not None and getattr(chat, "username", None) and chat.username.lower() != raw_id.lower():
                seen.setdefault(chat.username.lower(), (chat.username, self.uid(chat.username)))
        b.discovered_users = list(seen.values())[:limit]
        return b
