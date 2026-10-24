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


def export_all(db: DB, out_dir: Path, chunk: int = 200_000) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, sql in TABLES.items():
        target = out_dir / name
        n = 0
        with db.conn.transaction(), db.conn.cursor(name=f"exp_{name}") as cur:
            cur.itersize = chunk
            cur.execute(sql)
            part = 0
            while True:
                rows = cur.fetchmany(chunk)
                if not rows:
                    break
                table = pa.Table.from_pylist(rows)
                if name == "posts":
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
