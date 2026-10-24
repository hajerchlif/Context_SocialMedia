"""PostgreSQL access: schema creation, idempotent upserts, queues."""
from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .models import Batch

SCHEMA = Path(__file__).with_name("schema.sql")


class DB:
    def __init__(self, url: str):
        self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)

    def init_schema(self) -> None:
        self.conn.execute(SCHEMA.read_text())

    # ---------- writes ----------
    def save(self, b: Batch, domain: str | None = None, hop: int | None = None) -> dict:
        with self.conn.cursor() as cur:
            if b.users:
                cur.executemany(
                    """INSERT INTO users (user_uid, platform, bio, account_created_at, followers_count,
                                          following_count, posts_count, is_bot_flag, domain_hint, hop)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (user_uid) DO UPDATE SET
                         bio = COALESCE(EXCLUDED.bio, users.bio),
                         followers_count = COALESCE(EXCLUDED.followers_count, users.followers_count),
                         following_count = COALESCE(EXCLUDED.following_count, users.following_count),
                         posts_count = COALESCE(EXCLUDED.posts_count, users.posts_count),
                         domain_hint = COALESCE(users.domain_hint, EXCLUDED.domain_hint),
                         hop = LEAST(COALESCE(users.hop, 9), COALESCE(EXCLUDED.hop, 9))""",
                    [(u.user_uid, u.platform, u.bio, u.account_created_at, u.followers_count, u.following_count,
                      u.posts_count, u.is_bot_flag, domain, hop) for u in b.users],
                )
            if b.posts:
                cur.executemany(
                    """INSERT INTO posts (post_uid, platform, author_uid, text, created_at, lang, hashtags, mentions_uid,
                                          urls, media_type, media_alt_text, parent_post_uid, root_post_uid, quote_of_uid,
                                          like_count, repost_count, reply_count, community, source_method)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (post_uid) DO UPDATE SET
                         like_count = COALESCE(EXCLUDED.like_count, posts.like_count),
                         repost_count = COALESCE(EXCLUDED.repost_count, posts.repost_count),
                         reply_count = COALESCE(EXCLUDED.reply_count, posts.reply_count),
                         parent_post_uid = COALESCE(posts.parent_post_uid, EXCLUDED.parent_post_uid),
                         root_post_uid = COALESCE(posts.root_post_uid, EXCLUDED.root_post_uid)""",
                    [(p.post_uid, p.platform, p.author_uid, p.text, p.created_at, p.lang, p.hashtags, p.mentions_uid,
                      p.urls, p.media_type, p.media_alt_text, p.parent_post_uid, p.root_post_uid, p.quote_of_uid,
                      p.like_count, p.repost_count, p.reply_count, p.community, p.source_method) for p in b.posts],
                )
            if b.edges:
                cur.executemany(
                    """INSERT INTO edges (src_uid, dst_uid, platform, type, post_uid, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    [(e.src_uid, e.dst_uid, e.platform, e.type, e.post_uid or "", e.created_at) for e in b.edges],
                )
        return {"users": len(b.users), "posts": len(b.posts), "edges": len(b.edges)}

    def enqueue_users(self, platform: str, items: list[tuple[str, str]], domain: str | None, hop: int, priority: float = 0) -> int:
        if not items:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO crawl_queue (platform, raw_id, user_uid, domain, hop, priority)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (platform, raw_id) DO UPDATE SET priority = crawl_queue.priority + 1""",
                [(platform, raw, uid, domain, hop, priority) for raw, uid in items],
            )
        return len(items)

    def enqueue_roots(self, platform: str, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO thread_queue (platform, raw_root, root_uid) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                [(platform, raw, uid) for raw, uid in items],
            )

    def next_users(self, platform: str, limit: int, max_hop: int = 2) -> list[dict]:
        return self.conn.execute(
            """SELECT * FROM crawl_queue WHERE platform=%s AND status='todo' AND hop<=%s
               ORDER BY hop ASC, priority DESC LIMIT %s""",
            (platform, max_hop, limit),
        ).fetchall()

    def next_roots(self, platform: str, limit: int) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM thread_queue WHERE platform=%s AND status='todo' LIMIT %s", (platform, limit)
        ).fetchall()

    def mark_user(self, platform: str, raw_id: str, status: str) -> None:
        self.conn.execute("UPDATE crawl_queue SET status=%s, updated_at=now() WHERE platform=%s AND raw_id=%s",
                          (status, platform, raw_id))

    def mark_root(self, platform: str, raw_root: str, status: str) -> None:
        self.conn.execute("UPDATE thread_queue SET status=%s WHERE platform=%s AND raw_root=%s",
                          (status, platform, raw_root))

    def count_queue(self, platform: str, domain: str | None, hop: int) -> int:
        return self.conn.execute(
            "SELECT count(*) AS n FROM crawl_queue WHERE platform=%s AND domain IS NOT DISTINCT FROM %s AND hop=%s",
            (platform, domain, hop),
        ).fetchone()["n"]

    def add_seed(self, platform: str, domain: str, kind: str, value: str, origin: str = "manual", accepted: bool = True) -> None:
        self.conn.execute(
            """INSERT INTO seeds (platform, domain, kind, value, origin, accepted) VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (platform, kind, value) DO NOTHING""",
            (platform, domain, kind, value, origin, accepted),
        )

    def seeds(self, platform: str | None = None) -> list[dict]:
        if platform:
            return self.conn.execute("SELECT * FROM seeds WHERE accepted AND platform=%s", (platform,)).fetchall()
        return self.conn.execute("SELECT * FROM seeds WHERE accepted").fetchall()

    def reid(self, uid: str, platform: str, raw_id: str) -> None:
        self.conn.execute("INSERT INTO reid_map VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (uid, platform, raw_id))

    def unlabeled_posts(self, limit: int) -> list[dict]:
        return self.conn.execute(
            """SELECT p.post_uid, p.text, u.domain_hint FROM posts p
               LEFT JOIN llm_labels l USING (post_uid) LEFT JOIN users u ON u.user_uid = p.author_uid
               WHERE l.post_uid IS NULL AND p.text IS NOT NULL AND length(p.text) > 15
               ORDER BY p.collected_at DESC LIMIT %s""",
            (limit,),
        ).fetchall()

    def save_labels(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO llm_labels (post_uid, domain, relevance, context_json, model)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (post_uid) DO NOTHING""",
                rows,
            )

    def run_start(self, platform: str, job: str) -> int:
        return self.conn.execute("INSERT INTO collection_runs (platform, job) VALUES (%s,%s) RETURNING run_id",
                                 (platform, job)).fetchone()["run_id"]

    def run_end(self, run_id: int, items: int, errors: int, note: str = "") -> None:
        self.conn.execute("UPDATE collection_runs SET ended_at=now(), items=%s, errors=%s, note=%s WHERE run_id=%s",
                          (items, errors, note, run_id))

    def stats(self) -> list[dict]:
        return self.conn.execute(
            """SELECT p.platform, count(*) AS posts, count(DISTINCT p.author_uid) AS authors,
                      count(DISTINCT p.root_post_uid) AS threads, min(p.created_at) AS first, max(p.created_at) AS last
               FROM posts p GROUP BY p.platform ORDER BY posts DESC"""
        ).fetchall()
