"""CLI:  python -m collector <command>

  init-db                     create tables
  propose  [-p bluesky ...]   ask the local LLM (Qwen) for queries/hashtags/subreddits per domain
  seeds                       list seeds (manual + LLM)
  discover [-p ...]           search platforms with the seeds -> candidate accounts
  expand   [-p ...]           LLM relevance gate + backfill 2023->today + snowball (follows/mentions)
  threads  [-p ...]           full conversation trees
  label                       LLM annotation of posts (domain, topic, intent, stance, entities)
  run --all [--loop H]        discover -> expand -> threads -> label (repeat every H hours)
  reddit-archives             ingest .zst dumps from REDDIT_ARCHIVE_DIR
  export                      Parquet export to data/export
  stats                       counts per platform
  dashboard                   web progress page on :8080
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import DATA_DIR, settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collector")
    ap.add_argument("command")
    ap.add_argument("-p", "--platform", action="append", help="limit to platform(s)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--loop", type=float, default=0, help="repeat every N hours")
    ap.add_argument("--per-month", type=int, default=0, help="export: also write a time-balanced sample (N posts per platform-month)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cmd = a.command
    if cmd == "dashboard":
        from .dashboard import serve
        serve()
        return 0

    from .pipeline import Pipeline

    if cmd in {"init-db"}:
        settings.validate()
        from .db import DB
        DB(settings.database_url).init_schema()
        print("schema ready")
        return 0

    pl = Pipeline(settings)
    plats = a.platform
    if cmd == "propose":
        pl.propose(plats)
    elif cmd == "seeds":
        for s in pl.db.seeds():
            print(f"{s['platform']:9} {s['domain']:20} {s['kind']:10} {s['origin']:6} {s['value']}")
    elif cmd == "discover":
        pl.discover(plats)
    elif cmd == "expand":
        pl.expand(plats)
    elif cmd == "threads":
        pl.threads(plats)
    elif cmd == "label":
        pl.label()
    elif cmd == "run":
        while True:
            pl.run_all(plats)
            if not a.loop:
                break
            logging.info("sleeping %.1f h before next cycle", a.loop)
            time.sleep(a.loop * 3600)
    elif cmd == "reddit-archives":
        from .connectors.reddit import RedditConnector
        c = RedditConnector(settings, pl.p)
        total = 0
        for b in c.archive_batches(settings.reddit_archive_dir):
            pl.db.save(b)
            total += len(b.posts)
            logging.info("reddit archives: %d items", total)
    elif cmd == "export":
        from .export import export_all
        print(export_all(pl.db, DATA_DIR / "export", per_month=a.per_month, date_from=settings.date_from))
    elif cmd == "stats":
        for row in pl.db.stats():
            print(row)
        print(pl.db.conn.execute("SELECT platform, status, count(*) FROM crawl_queue GROUP BY 1,2 ORDER BY 1,2").fetchall())
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
