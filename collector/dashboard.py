"""Web dashboard (stdlib only): live progress of the collection.

    python -m collector dashboard      -> http://<server>:8080

Shows only aggregates (counts, dates, queues, LLM-proposed hashtags, label topics):
no post text and no identifiers. Optional HTTP basic auth with DASHBOARD_PASSWORD.
"""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)
HTML = Path(__file__).with_name("dashboard.html")

QUERIES = {
    "totals": """SELECT (SELECT count(*) FROM posts) AS posts, (SELECT count(*) FROM users) AS users,
                        (SELECT count(*) FROM edges) AS edges, (SELECT count(*) FROM llm_labels) AS labels,
                        (SELECT count(DISTINCT root_post_uid) FROM posts) AS threads,
                        (SELECT count(*) FROM posts WHERE collected_at > now() - interval '10 minutes') AS last10m,
                        (SELECT count(*) FROM posts WHERE collected_at > now() - interval '1 hour') AS last1h,
                        (SELECT count(*) FROM posts WHERE collected_at > now() - interval '24 hours') AS last24h,
                        (SELECT count(*) FROM llm_labels WHERE labeled_at > now() - interval '1 hour') AS labels1h,
                        (SELECT max(collected_at) FROM posts) AS last_post_at""",
    "platforms": """SELECT platform, count(*) AS posts, count(DISTINCT author_uid) AS authors,
                           count(DISTINCT root_post_uid) AS threads, min(created_at) AS first, max(created_at) AS last
                    FROM posts GROUP BY platform ORDER BY posts DESC""",
    "monthly": """SELECT platform, to_char(date_trunc('month', created_at), 'YYYY-MM') AS month, count(*) AS n
                  FROM posts WHERE created_at >= '2023-01-01' AND created_at <= now()
                  GROUP BY 1, 2 ORDER BY 2""",
    "queue": """SELECT platform, hop, status, count(*) AS n FROM crawl_queue GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
    "threads_queue": """SELECT platform, status, count(*) AS n FROM thread_queue GROUP BY 1, 2 ORDER BY 1, 2""",
    "runs": """SELECT run_id, platform, job, started_at, ended_at, items, errors FROM collection_runs
               ORDER BY run_id DESC LIMIT 20""",
    "users_by_domain": """SELECT coalesce(domain_hint, '(snowball)') AS domain, count(*) AS n FROM users
                          GROUP BY 1 ORDER BY 2 DESC""",
    "labels_by_domain": """SELECT domain, count(*) AS n, round(avg(relevance)::numeric, 2) AS relevance
                           FROM llm_labels GROUP BY 1 ORDER BY 2 DESC LIMIT 15""",
    "new_domains": """SELECT context_json->>'new_domain' AS domain, count(*) AS n FROM llm_labels
                      WHERE coalesce(context_json->>'new_domain', '') NOT IN ('', 'null', 'None')
                      GROUP BY 1 ORDER BY 2 DESC LIMIT 15""",
    "topics": """SELECT lower(context_json->>'topic') AS topic, count(*) AS n FROM llm_labels
                 WHERE coalesce(context_json->>'topic', '') <> '' GROUP BY 1 ORDER BY 2 DESC LIMIT 20""",
    "intents": """SELECT context_json->>'intent' AS intent, count(*) AS n FROM llm_labels
                  WHERE context_json ? 'intent' GROUP BY 1 ORDER BY 2 DESC LIMIT 8""",
    "seeds": """SELECT platform, origin, count(*) AS n FROM seeds GROUP BY 1, 2 ORDER BY 1, 2""",
    "llm_seeds": """SELECT platform, domain, kind, value FROM seeds WHERE origin = 'llm'
                    ORDER BY seed_id DESC LIMIT 60""",
    "edges": """SELECT type, count(*) AS n FROM edges GROUP BY 1 ORDER BY 2 DESC""",
}


def _json_default(o):
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return float(o)
    return str(o)


class Stats:
    def __init__(self, db_url: str, ollama_url: str, model: str):
        self.db_url, self.ollama_url, self.model = db_url, ollama_url.rstrip("/"), model
        self.conn = None

    def _conn(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(self.db_url, autocommit=True, row_factory=dict_row)
        return self.conn

    def collect(self) -> dict:
        out: dict = {"now": dt.datetime.now(dt.timezone.utc)}
        try:
            conn = self._conn()
            for k, q in QUERIES.items():
                try:
                    out[k] = conn.execute(q).fetchall()
                except psycopg.errors.UndefinedTable:
                    out[k] = []
        except Exception as e:  # noqa: BLE001
            self.conn = None
            out["db_error"] = str(e)
        out["ollama"] = self.ollama()
        out["date_from"] = os.getenv("DATE_FROM", "2023-01-01")
        out["pilot"] = os.getenv("PILOT_MODE", "true")
        return out

    def ollama(self) -> dict:
        info = {"model": self.model, "up": False, "loaded": []}
        try:
            tags = httpx.get(f"{self.ollama_url}/api/tags", timeout=3).json()
            info["up"] = True
            info["pulled"] = [m["name"] for m in tags.get("models", [])]
            ps = httpx.get(f"{self.ollama_url}/api/ps", timeout=3).json()
            info["loaded"] = [{"name": m.get("name"), "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1)}
                              for m in ps.get("models", [])]
        except Exception as e:  # noqa: BLE001
            info["error"] = str(e)
        return info


def make_handler(stats: Stats, password: str):
    expected = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode() if password else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _auth_ok(self) -> bool:
            if not expected or self.headers.get("Authorization") == expected:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="collector"')
            self.end_headers()
            return False

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._auth_ok():
                return
            if self.path.startswith("/api/stats"):
                body = json.dumps(stats.collect(), default=_json_default).encode()
                self._send(200, body, "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/health":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    stats = Stats(os.getenv("DATABASE_URL", ""), os.getenv("OLLAMA_URL", "http://localhost:11434"),
                  os.getenv("OLLAMA_MODEL", "qwen3:14b"))
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    srv = ThreadingHTTPServer((host, port), make_handler(stats, password))
    log.info("dashboard on http://%s:%d %s", host, port, "(password protected)" if password else "")
    srv.serve_forever()
