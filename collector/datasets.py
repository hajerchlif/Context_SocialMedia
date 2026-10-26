"""Public research datasets for platforms we cannot collect from directly (X/Twitter, Weibo).

Qwen drives the whole loop, like for the live platforms:
  1. propose : Qwen proposes search queries per platform x domain (+ fixed base queries);
  2. search  : Hugging Face Hub and Zenodo are searched through their public APIs;
  3. screen  : hard rules first (licence, gated, size, scraped provenance), then Qwen reads each
               dataset card and decides whether it really contains posts of that platform, with
               text, relevant to the thesis domains;
  4. import  : rows are mapped to the common schema (heuristics, Qwen fallback for the columns),
               user ids pseudonymised, kept only inside DATE_FROM..DATE_TO, saved in `posts`;
  5. trace   : every dataset seen is recorded in `dataset_imports` (licence, provenance, Qwen's
               reasons, rows imported) so the thesis can cite exactly what was used.

Nothing here logs into X or Weibo or scrapes them: only datasets that researchers published.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import re
import zipfile
from pathlib import Path

import pyarrow.parquet as pq

from .config import DATA_DIR
from .http import PoliteClient
from .models import Batch, EdgeRec, PostRec, UserRec

log = logging.getLogger(__name__)

HF_API = "https://huggingface.co/api/datasets"
HF_PARQUET = "https://datasets-server.huggingface.co/parquet"
ZENODO_API = "https://zenodo.org/api/records"

BASE_QUERIES = {
    "x": ["twitter", "tweets", "twitter dataset", "x.com posts"],
    "weibo": ["weibo", "sina weibo", "微博"],
}
PLATFORM_WORDS = {
    "x": ("twitter", "tweet", "x.com", "x (formerly", "formerly twitter"),
    "weibo": ("weibo", "微博"),
}
# licences that allow non-commercial academic reuse
LICENCE_OK = re.compile(r"^(cc0|cc-by|cc-by-sa|cc-by-nc|cc-by-nc-sa|cc-by-4\.0|cc-by-sa-4\.0|cc-by-nc-4\.0|"
                        r"cc-by-nc-sa-4\.0|cc-by-3\.0|cc-by-nc-3\.0|cc-by-nc-nd|cc-by-nd|mit|apache-2\.0|"
                        r"bsd.*|odc-by|odbl|pddl|openrail|unlicense|gpl.*|lgpl.*|afl-3\.0|ecl-2\.0|"
                        r"artistic-2\.0|wtfpl|cdla.*|c-uda|etalab.*)", re.I)
# collected by mining networks / scraping services: provenance not defensible in a thesis
SCRAPED_HINTS = re.compile(r"bittensor|subnet\s*\d+|\bminers?\b|apify|scraped with|snscrape|twscrape|"
                           r"nitter|selenium|without (the )?api", re.I)
TABULAR_EXT = (".parquet", ".csv", ".tsv", ".jsonl", ".json", ".csv.gz", ".jsonl.gz", ".tsv.gz", ".zip")

COLS = {
    "text": ["text", "full_text", "rawcontent", "raw_content", "tweet", "tweet_text", "content", "post", "body",
             "message", "weibo_text", "text_raw", "微博正文", "正文", "内容", "sentence", "clean_text"],
    "id": ["id", "tweet_id", "id_str", "status_id", "post_id", "mid", "weibo_id", "微博id", "博文id", "idstr"],
    "date": ["created_at", "date", "datetime", "timestamp", "time", "created_time", "epoch", "publish_time",
             "发布时间", "posted_at", "tweet_created_at", "createdat"],
    "user": ["user_id", "author_id", "userid", "uid", "user", "username", "screen_name", "author",
             "username_encoded", "user_id_str", "用户id", "用户", "user_name", "account"],
    "parent": ["in_reply_to_status_id", "in_reply_to_status_id_str", "reply_to", "parent_id", "replied_to",
               "in_reply_to_id", "repost_from", "retweeted_status_id", "retweetedtweetid", "转发id"],
    "root": ["conversation_id", "conversationid", "conversationidstr", "thread_id", "root_id", "root_mid"],
    "lang": ["lang", "language"],
    "hashtags": ["hashtags", "tweet_hashtags", "topics", "话题"],
    "likes": ["like_count", "likecount", "favorite_count", "likes", "attitudes_count", "点赞数"],
    "reposts": ["retweet_count", "retweetcount", "reposts_count", "reposts", "转发数"],
    "replies": ["reply_count", "replycount", "comments_count", "评论数"],
}


def _norm(c: str) -> str:
    return re.sub(r"[\s\-.]", "_", c.strip().lower())


def guess_columns(columns: list[str]) -> dict[str, str]:
    """Map our fields to the dataset's column names by name (exact, then 'contains')."""
    by_norm = {_norm(c): c for c in columns}
    out: dict[str, str] = {}
    for field, names in COLS.items():
        for n in names:
            if _norm(n) in by_norm:
                out[field] = by_norm[_norm(n)]
                break
        else:
            if field in ("text", "date"):
                for nc, orig in by_norm.items():
                    if any(_norm(n) in nc for n in names[:3]) and orig not in out.values():
                        out[field] = orig
                        break
    if out.get("id") and out.get("user") == out.get("id"):
        out.pop("user")
    return out


_DATE_FORMATS = ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                 "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M",
                 "%Y年%m月%d日 %H:%M", "%Y年%m月%d日")


def parse_any_date(v) -> dt.datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day, tzinfo=dt.timezone.utc)
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", v.strip())):
        x = float(v)
        if x > 1e11:  # milliseconds
            x /= 1000
        try:
            return dt.datetime.fromtimestamp(x, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        pass
    for f in _DATE_FORMATS:
        try:
            d = dt.datetime.strptime(s, f)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _num(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _str_id(v) -> str | None:
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s if s and s.lower() not in {"none", "nan", "null"} else None


class DatasetImporter:
    def __init__(self, settings, db, llm, llm_ok: bool, pseudo, domains):
        self.s = settings
        self.db = db
        self.llm = llm
        self.llm_ok = llm_ok
        self.p = pseudo
        self.domains = domains
        self.http = PoliteClient(settings.user_agent, rps=2.0, timeout=120)
        self.headers = {"Authorization": f"Bearer {settings.hf_token}"} if settings.hf_token else None
        self.root = DATA_DIR / "datasets"

    # ------------------------------------------------------------ 1. queries
    def queries(self, platform: str) -> list[str]:
        qs = list(BASE_QUERIES.get(platform, [platform]))
        for s in self.db.seeds(f"dataset_{platform}"):
            qs.append(s["value"])
        if self.llm_ok and not any(s["origin"] == "llm" for s in self.db.seeds(f"dataset_{platform}")):
            for d in self.domains:
                items = self.llm.propose_queries(d.name, d.description, d.keywords, f"dataset_{platform}", 6)
                for it in items:
                    self.db.add_seed(f"dataset_{platform}", d.name, "dataset_query", it, origin="llm")
                    qs.append(it)
                log.info("[datasets/%s] %s: Qwen proposed %s", platform, d.name, items)
        seen, out = set(), []
        for q in qs:
            if q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
        return out

    # ------------------------------------------------------------ 2. search
    def search_hf(self, q: str) -> list[dict]:
        r = self.http.get(HF_API, params={"search": q, "limit": 40, "full": "true"}, headers=self.headers)
        if r.status_code != 200:
            return []
        out = []
        for d in r.json():
            card = d.get("cardData") or {}
            tags = d.get("tags") or []
            lic = card.get("license") or next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), None)
            if isinstance(lic, list):
                lic = lic[0] if lic else None
            out.append({"source": "hf", "dataset_id": d["id"], "title": card.get("pretty_name") or d["id"],
                        "url": f"https://huggingface.co/datasets/{d['id']}", "license": lic,
                        "gated": bool(d.get("gated")) or bool(d.get("private")) or bool(d.get("disabled")),
                        "updated": d.get("lastModified"), "tags": tags,
                        "description": (d.get("description") or "")[:3000]})
        return out

    def search_zenodo(self, q: str) -> list[dict]:
        r = self.http.get(ZENODO_API, params={"q": q, "size": 25, "type": "dataset", "sort": "bestmatch"})
        if r.status_code != 200:
            return []
        out = []
        for h in r.json().get("hits", {}).get("hits", []):
            m = h.get("metadata", {})
            lic = (m.get("license") or {}).get("id")
            files = [{"name": f["key"], "size": f.get("size", 0), "url": f["links"]["self"]}
                     for f in h.get("files", []) if f["key"].lower().endswith(TABULAR_EXT)]
            out.append({"source": "zenodo", "dataset_id": str(h["id"]), "title": m.get("title", ""),
                        "url": h.get("links", {}).get("self_html") or f"https://zenodo.org/records/{h['id']}",
                        "license": lic, "gated": m.get("access_right") != "open", "updated": m.get("publication_date"),
                        "tags": m.get("keywords") or [], "files": files,
                        "description": re.sub(r"<[^>]+>", " ", m.get("description") or "")[:3000]})
        return out

    def _card(self, c: dict) -> str:
        if c["source"] == "hf" and not c.get("card_text"):
            r = self.http.get(f"https://huggingface.co/datasets/{c['dataset_id']}/raw/main/README.md",
                              headers=self.headers)
            c["card_text"] = r.text[:6000] if r.status_code == 200 else ""
        return f"{c['title']}\n{c.get('description', '')}\n{c.get('card_text', '')}\ntags: {', '.join(map(str, c['tags']))[:600]}"

    # ------------------------------------------------------------ 3. screen
    def screen(self, platform: str, c: dict) -> tuple[bool, str, dict]:
        """Hard rules, then Qwen. Returns (accepted, provenance, judgement)."""
        card = self._card(c)
        low = card.lower()
        if c["gated"]:
            return False, "gated", {"reason": "gated/restricted access (needs a manual request)"}
        lic = (c.get("license") or "").lower()
        if not LICENCE_OK.match(lic) and not self.s.datasets_allow_unknown_license:
            return False, "licence", {"reason": f"licence not open or unknown: {lic or 'none'}"}
        provenance = "scraped" if SCRAPED_HINTS.search(card) else "research"
        if provenance == "scraped" and not self.s.datasets_allow_scraped:
            return False, provenance, {"reason": "collected by scraping/mining network (provenance not defensible)"}
        if not any(w in low for w in PLATFORM_WORDS.get(platform, (platform,))):
            return False, provenance, {"reason": f"card does not mention {platform}"}
        if not self.llm_ok:
            return True, provenance, {"reason": "no LLM: accepted on platform keyword"}
        j = self.llm.judge_dataset(platform, [(d.name, d.description) for d in self.domains], card)
        ok = bool(j.get("platform_match")) and bool(j.get("has_post_text")) and bool(j.get("relevant"))
        return ok, provenance, j

    # ------------------------------------------------------------ 4. import
    def _files(self, c: dict) -> list[tuple[str, str, int]]:
        """(name, url, size) of the tabular files to read."""
        if c["source"] == "zenodo":
            return [(f["name"], f["url"], f["size"]) for f in c.get("files", [])]
        r = self.http.get(HF_PARQUET, params={"dataset": c["dataset_id"]}, headers=self.headers)
        if r.status_code == 200 and r.json().get("parquet_files"):
            return [(f"{f['config']}-{f['split']}-{Path(f['url']).name}", f["url"], f.get("size", 0))
                    for f in r.json()["parquet_files"]]
        # not converted by the HF dataset viewer: read the raw files of the repo
        r = self.http.get(f"{HF_API}/{c['dataset_id']}", params={"blobs": "true"}, headers=self.headers)
        if r.status_code != 200:
            return []
        out = []
        for f in r.json().get("siblings", []):
            name = f.get("rfilename", "")
            if name.lower().endswith(TABULAR_EXT) and not name.lower().endswith(("dataset_infos.json", "config.json")):
                url = f"https://huggingface.co/datasets/{c['dataset_id']}/resolve/main/{name}"
                out.append((name.replace("/", "_"), url, f.get("size") or 0))
        return out

    def _download(self, c: dict, name: str, url: str) -> Path:
        d = self.root / c["source"] / re.sub(r"[^\w.-]", "_", c["dataset_id"])
        d.mkdir(parents=True, exist_ok=True)
        path = d / re.sub(r"[^\w.-]", "_", name)
        if not path.exists():
            with self.http.client.stream("GET", url, headers=self.headers, timeout=600) as r:
                r.raise_for_status()
                tmp = path.with_suffix(path.suffix + ".part")
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                tmp.rename(path)
        return path

    @staticmethod
    def _rows(path: Path):
        """Yield dict rows from parquet/csv/tsv/jsonl/json (optionally .gz or inside a .zip)."""
        name = path.name.lower()
        if name.endswith(".parquet"):
            pf = pq.ParquetFile(path)
            for rb in pf.iter_batches(batch_size=5000):
                yield from rb.to_pylist()
            return
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                for inner in z.namelist():
                    if inner.lower().endswith((".csv", ".tsv", ".jsonl", ".json")):
                        data = z.read(inner)
                        yield from DatasetImporter._text_rows(inner.lower(), io.BytesIO(data))
            return
        opener = gzip.open if name.endswith(".gz") else open
        with opener(path, "rb") as fh:
            yield from DatasetImporter._text_rows(name.removesuffix(".gz"), fh)

    @staticmethod
    def _text_rows(name: str, fh):
        txt = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        if name.endswith(".jsonl"):
            for line in txt:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        elif name.endswith(".json"):
            data = json.load(txt)
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [data])
            yield from (r for r in data if isinstance(r, dict))
        else:
            csv.field_size_limit(10_000_000)
            yield from csv.DictReader(txt, delimiter="\t" if name.endswith(".tsv") else ",")

    def _mapping(self, platform: str, row: dict) -> dict[str, str]:
        cols = [k for k in row.keys() if isinstance(k, str)]
        m = guess_columns(cols)
        if "text" not in m and self.llm_ok:
            sample = {k: (str(v)[:120] if v is not None else None) for k, v in list(row.items())[:40]}
            m.update({k: v for k, v in self.llm.map_columns(platform, sample).items() if v in cols})
        return m

    def import_dataset(self, platform: str, c: dict) -> tuple[int, int, str]:
        files = self._files(c)
        total_mb = sum(f[2] or 0 for f in files) / 1e6
        if not files:
            return 0, 0, "no readable tabular file"
        if total_mb > self.s.datasets_max_mb:
            return 0, 0, f"too large ({total_mb:.0f} MB > DATASETS_MAX_MB)"
        src = f"dataset:{c['source']}:{c['dataset_id']}"
        anon_author = self.p.uid(platform, f"unknown:{src}")
        seen = kept = 0
        mapping = None
        b = Batch()
        users_seen: set[str] = set()
        for name, url, _ in files:
            path = self._download(c, name, url)
            for row in self._rows(path):
                if seen >= self.s.datasets_max_rows:
                    break
                seen += 1
                if mapping is None:
                    mapping = self._mapping(platform, row)
                    log.info("[datasets/%s] %s columns -> %s", platform, c["dataset_id"], mapping)
                    if "text" not in mapping:
                        return 0, 0, f"no text column among {list(row)[:20]}"
                p = self._post(platform, src, row, mapping, anon_author, b, users_seen)
                if p is None:
                    continue
                b.posts.append(p)
                kept += 1
                if len(b.posts) >= 2000:
                    self.db.save(b)
                    b = Batch()
            if seen >= self.s.datasets_max_rows:
                break
        if b.posts or b.users:
            self.db.save(b)
        if seen and not kept:
            return seen, 0, ("no dated row inside DATE_FROM..DATE_TO" if self.s.datasets_respect_dates
                             else "no usable text")
        return seen, kept, "ok"

    def _post(self, platform, src, row, m, anon_author, b: Batch, users_seen: set) -> PostRec | None:
        text = row.get(m["text"])
        if not isinstance(text, str) or len(text.strip()) < 3:
            return None
        ts = parse_any_date(row.get(m["date"])) if "date" in m else None
        if self.s.datasets_respect_dates and (ts is None or not (self.s.date_from <= ts <= self.s.date_to)):
            return None
        raw_id = _str_id(row.get(m["id"])) if "id" in m else None
        post_uid = self.p.uid(platform, f"post:{raw_id}") if raw_id else self.p.uid(platform, f"{src}:{hashlib.md5(text.encode()).hexdigest()}:{ts}")
        user = _str_id(row.get(m["user"])) if "user" in m else None
        if isinstance(row.get(m.get("user", "")), dict):  # nested user objects (Twitter v1 JSON)
            u = row[m["user"]]
            user = _str_id(u.get("id_str") or u.get("id") or u.get("username"))
        author = self.p.uid(platform, f"user:{user}") if user else anon_author
        if author not in users_seen:
            users_seen.add(author)
            b.users.append(UserRec(user_uid=author, platform=platform))
        parent_raw = _str_id(row.get(m["parent"])) if "parent" in m else None
        parent = self.p.uid(platform, f"post:{parent_raw}") if parent_raw else None
        root_raw = _str_id(row.get(m["root"])) if "root" in m else None
        root = self.p.uid(platform, f"post:{root_raw}") if root_raw else (parent or post_uid)
        tags = row.get(m["hashtags"]) if "hashtags" in m else None
        if isinstance(tags, str):
            tags = re.findall(r"[\w一-鿿]+", tags)
        if not isinstance(tags, list) or not tags:
            # Weibo topics are #话题# (closed); Twitter hashtags are #word
            tags = (re.findall(r"#([^#\n]{1,40})#", text.replace("##", "# #")) if platform == "weibo"
                    else re.findall(r"#(\w+)", text))
        if parent and parent != post_uid:
            b.edges.append(EdgeRec(src_uid=author, dst_uid=parent, platform=platform, type="reply",
                                   post_uid=post_uid, created_at=ts))
        lang = row.get(m["lang"]) if "lang" in m else None
        return PostRec(
            post_uid=post_uid, platform=platform, author_uid=author,
            text=self.p.rewrite_mentions(platform, text), created_at=ts,
            lang=lang if isinstance(lang, str) else None,
            hashtags=sorted({str(t).lower().lstrip("#") for t in tags if t})[:30],
            urls=re.findall(r"https?://\S+", text)[:20],
            parent_post_uid=parent, root_post_uid=root,
            like_count=_num(row.get(m["likes"])) if "likes" in m else None,
            repost_count=_num(row.get(m["reposts"])) if "reposts" in m else None,
            reply_count=_num(row.get(m["replies"])) if "replies" in m else None,
            community=src[:200], source_method=src[:200],
        )

    # ------------------------------------------------------------ 5. loop
    def run(self, platforms: list[str] | None = None, max_imports: int = 10) -> None:
        platforms = platforms or self.s.datasets_platforms
        for platform in platforms:
            run = self.db.run_start(f"dataset_{platform}", "datasets")
            imported = errors = 0
            known = self.db.dataset_statuses()
            cands: dict[tuple[str, str], dict] = {}
            for q in self.queries(platform):
                try:
                    for c in self.search_hf(q) + self.search_zenodo(q):
                        cands.setdefault((c["source"], c["dataset_id"]), c)
                except Exception as e:  # noqa: BLE001
                    log.warning("[datasets/%s] search %r failed: %s", platform, q, e)
            log.info("[datasets/%s] %d candidate datasets", platform, len(cands))
            for key, c in cands.items():
                if imported >= max_imports:
                    break
                if known.get(key) in {"imported", "rejected", "empty"}:
                    continue
                try:
                    ok, prov, j = self.screen(platform, c)
                except Exception as e:  # noqa: BLE001
                    log.warning("[datasets/%s] screening %s failed: %s", platform, c["dataset_id"], e)
                    continue
                if not ok:
                    self.db.record_dataset(c, platform, prov, j, 0, 0, "rejected")
                    log.info("[datasets/%s] rejected %s: %s", platform, c["dataset_id"], j.get("reason"))
                    continue
                log.info("[datasets/%s] importing %s (%s, licence %s)", platform, c["dataset_id"], c["source"], c["license"])
                try:
                    seen, kept, note = self.import_dataset(platform, c)
                    status = "imported" if kept else "empty"
                    j = {**j, "import_note": note}
                    self.db.record_dataset(c, platform, prov, j, seen, kept, status)
                    log.info("[datasets/%s] %s: %d rows read, %d posts kept (%s)", platform, c["dataset_id"], seen, kept, note)
                    imported += 1 if kept else 0
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    log.exception("[datasets/%s] import of %s failed: %s", platform, c["dataset_id"], e)
                    self.db.record_dataset(c, platform, prov, {**j, "error": str(e)[:300]}, 0, 0, "error")
            self.db.run_end(run, imported, errors)
