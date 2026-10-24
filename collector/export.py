"""Export pseudonymised tables to Parquet (posts partitioned by platform/year/month).

Never exported: crawl_queue, thread_queue, seeds, reid_map (they contain raw identifiers).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .db import DB

log = logging.getLogger(__name__)

TABLES = {
    "users": "SELECT * FROM users",
    "edges": "SELECT * FROM edges",
    "llm_labels": "SELECT post_uid, domain, relevance, context_json::text AS context_json, model, labeled_at FROM llm_labels",
    "posts": """SELECT *, EXTRACT(YEAR FROM created_at)::int AS year, EXTRACT(MONTH FROM created_at)::int AS month
                FROM posts WHERE created_at IS NOT NULL""",
}


BALANCED = """SELECT * FROM (
                 SELECT *, EXTRACT(YEAR FROM created_at)::int AS year, EXTRACT(MONTH FROM created_at)::int AS month,
                        row_number() OVER (PARTITION BY platform, date_trunc('month', created_at)
                                           ORDER BY md5(post_uid)) AS _rn
                 FROM posts WHERE created_at >= %(date_from)s AND created_at <= now()) t
               WHERE _rn <= %(per_month)s"""


def export_all(db: DB, out_dir: Path, chunk: int = 200_000, per_month: int = 0, date_from=None) -> dict[str, int]:
    """per_month > 0 also writes posts_balanced/: at most N posts per platform and month (time-balanced sample)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    tables = dict(TABLES)
    if per_month:
        tables["posts_balanced"] = BALANCED
    for name, sql in tables.items():
        target = out_dir / name
        n = 0
        with db.conn.transaction(), db.conn.cursor(name=f"exp_{name}") as cur:
            cur.itersize = chunk
            cur.execute(sql, {"per_month": per_month, "date_from": date_from} if name == "posts_balanced" else None)
            part = 0
            while True:
                rows = cur.fetchmany(chunk)
                if not rows:
                    break
                if name == "posts_balanced":
                    rows = [{k: v for k, v in r.items() if k != "_rn"} for r in rows]
                table = pa.Table.from_pylist(rows)
                if name in ("posts", "posts_balanced"):
                    pq.write_to_dataset(table, root_path=str(target), partition_cols=["platform", "year", "month"],
                                        basename_template=f"part-{part}-{{i}}.parquet",
                                        existing_data_behavior="overwrite_or_ignore")
                else:
                    target.mkdir(parents=True, exist_ok=True)
                    pq.write_table(table, target / f"part-{part}.parquet")
                n += len(rows)
                part += 1
        counts[name] = n
        log.info("exported %s: %d rows -> %s", name, n, target)
    return counts
