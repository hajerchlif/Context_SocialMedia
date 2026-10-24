"""LLM-driven collection pipeline.

  1. discover : Qwen proposes queries/hashtags/subreddits/channels per domain (+ your manual seeds)
                -> platform search -> candidate accounts (hop 0)
  2. expand   : Qwen scores each candidate (bio + recent posts); relevant ones are backfilled
                (all posts DATE_FROM..DATE_TO) and their follows/mentions are queued (hop 1, 2)
  3. threads  : full conversation trees for collected roots
  4. label    : Qwen annotates posts (domain, relevance, topic, intent, stance, entities; open domains)
"""
from __future__ import annotations

import json
import logging
import time

from .config import Settings, load_domains, load_seeds
from .connectors import ALL
from .connectors.base import Connector
from .db import DB
from .http import Blocked
from .llm import LocalLLM
from .pseudo import Pseudonymizer

log = logging.getLogger(__name__)

PLATFORM_KIND_FOR_LLM = {"bluesky": "query", "mastodon": "hashtag", "reddit": "subreddit",
                         "telegram": "channel", "youtube": "query"}


class Pipeline:
    def __init__(self, settings: Settings):
        settings.validate()
        self.s = settings
        self.db = DB(settings.database_url)
        self.db.init_schema()
        self.p = Pseudonymizer(settings.hmac_secret)
        self.llm = LocalLLM(settings.ollama_url, settings.ollama_model, enabled=settings.use_llm)
        self.llm_ok = self.llm.available()
        self.domains = load_domains()
        self.dom_by_name = {d.name: d for d in self.domains}
        log.info("LLM %s (%s) | domains: %s", "ON" if self.llm_ok else "OFF", settings.ollama_model,
                 [d.name for d in self.domains])
        for sd in load_seeds():
            self.db.add_seed(sd.platform, sd.domain, sd.kind, sd.value, "manual")

    # ----------------------------------------------------------- helpers
    def connectors(self, only: list[str] | None = None) -> list[Connector]:
        out = []
        for name, cls in ALL.items():
            if only and name not in only:
                continue
            c = cls(self.s, self.p)
            ok, why = c.enabled()
            if ok:
                out.append(c)
            else:
                log.info("[%s] skipped: %s", name, why)
        return out

    def _save(self, c: Connector, batch, domain=None, hop=0):
        stats = self.db.save(batch, domain, hop)
        self.db.enqueue_roots(c.platform, batch.discovered_roots)
        if self.s.keep_reid_table:
            for raw, uid in batch.discovered_users:
                self.db.reid(uid, c.platform, raw)
        return stats

    @property
    def users_per_run(self) -> int:
        return 60 if self.s.pilot_mode else 2000

    @property
    def posts_per_user(self) -> int:
        return 400 if self.s.pilot_mode else 5000

    # ---------------------------------------------------------- 1. discover
    def propose(self, platforms: list[str] | None = None, n: int = 15) -> None:
        """Ask the local LLM for queries per domain/platform; stored as seeds (origin=llm)."""
        if not self.llm_ok:
            log.warning("LLM unavailable: only manual seeds will be used")
            return
        for c in self.connectors(platforms):
            kind = PLATFORM_KIND_FOR_LLM.get(c.platform)
            if not kind:
                continue
            for d in self.domains:
                items = self.llm.propose_queries(d.name, d.description, d.keywords, c.platform, n)
                for it in items:
                    self.db.add_seed(c.platform, d.name, kind, it, origin="llm")
                log.info("[%s] %s: LLM proposed %s", c.platform, d.name, items)

    def discover(self, platforms: list[str] | None = None) -> None:
        if self.llm_ok and not any(s["origin"] == "llm" for s in self.db.seeds()):
            self.propose(platforms)
        for c in self.connectors(platforms):
            run = self.db.run_start(c.platform, "discover")
            items = errors = 0
            for seed in self.db.seeds(c.platform):
                if seed["kind"] not in c.query_kinds:
                    continue
                try:
                    batch = c.search(seed["value"], seed["kind"], limit=100 if self.s.pilot_mode else 1000)
                except Blocked as e:
                    log.warning("[%s] blocked on %r: %s", c.platform, seed["value"], e)
                    errors += 1
                    continue
                except Exception as e:  # noqa: BLE001
                    log.exception("[%s] search %r failed: %s", c.platform, seed["value"], e)
                    errors += 1
                    continue
                self._save(c, batch, seed["domain"], 0)
                users = [(r, u) for r, u in dict(batch.discovered_users).items()]
                self.db.enqueue_users(c.platform, users, seed["domain"], 0,
                                      priority=2.0 if seed["kind"] == "account" else 1.0)
                items += len(batch.posts) + len(users)
                log.info("[%s] %s %r -> %d posts, %d accounts", c.platform, seed["kind"], seed["value"],
                         len(batch.posts), len(users))
            self.db.run_end(run, items, errors)

    # ------------------------------------------------------------ 2. expand
    def _relevant(self, c: Connector, rows: list[dict]) -> dict[str, float]:
        """LLM relevance gate. Without LLM: keep users with enough posts."""
        samples, scores = {}, {}
        for r in rows:
            try:
                samples[r["raw_id"]] = c.sample(r["raw_id"])
            except Exception as e:  # noqa: BLE001
                log.debug("sample failed %s: %s", r["raw_id"], e)
                samples[r["raw_id"]] = {"bio": "", "sample": []}
        for r in rows:
            sm = samples[r["raw_id"]]
            if sm.get("opted_out") or len(sm.get("sample", [])) < 3:
                scores[r["raw_id"]] = 0.0
            elif (sm.get("posts_count") or 99) < self.s.min_posts_per_user:
                scores[r["raw_id"]] = 0.0
            else:
                scores[r["raw_id"]] = 0.6
        if not self.llm_ok:
            return scores
        by_domain: dict[str, list[dict]] = {}
        for i, r in enumerate(rows):
            if scores[r["raw_id"]] > 0:
                by_domain.setdefault(r["domain"] or "", []).append(
                    {"id": str(i), "raw": r["raw_id"], "bio": samples[r["raw_id"]].get("bio", ""),
                     "sample": samples[r["raw_id"]].get("sample", [])})
        for dom, accs in by_domain.items():
            d = self.dom_by_name.get(dom)
            desc = d.description if d else "any topic (snowball neighbour)"
            for k in range(0, len(accs), 8):
                chunk = accs[k:k + 8]
                res = self.llm.score_accounts(dom or "general", desc, chunk)
                for a in chunk:
                    if a["id"] in res:
                        scores[a["raw"]] = res[a["id"]]
        return scores

    def expand(self, platforms: list[str] | None = None, max_hop: int = 2, threshold: float = 0.4) -> None:
        for c in self.connectors(platforms):
            if c.platform == "youtube":
                continue  # YouTube is collected through video threads only
            run = self.db.run_start(c.platform, "expand")
            done = errors = 0
            while done < self.users_per_run:
                rows = self.db.next_users(c.platform, 8, max_hop)
                if not rows:
                    break
                scores = self._relevant(c, rows)
                for r in rows:
                    sc = scores.get(r["raw_id"], 0)
                    if sc < threshold:
                        self.db.mark_user(c.platform, r["raw_id"], "skipped")
                        continue
                    try:
                        tl = c.timeline(r["raw_id"], self.posts_per_user)
                        self._save(c, tl, r["domain"], r["hop"])
                        if r["hop"] < max_hop:
                            cap = self.s.max_users_per_domain_hop1 if r["hop"] == 0 else self.s.max_users_per_domain_hop2
                            if self.db.count_queue(c.platform, r["domain"], r["hop"] + 1) < cap:
                                fb = c.follows(r["raw_id"], 200 if self.s.pilot_mode else 1000)
                                self._save(c, fb, None, r["hop"] + 1)
                                neigh = dict(fb.discovered_users)
                                neigh.update(dict(tl.discovered_users))
                                neigh.pop(r["raw_id"], None)
                                self.db.enqueue_users(c.platform, list(neigh.items()), r["domain"], r["hop"] + 1)
                        self.db.mark_user(c.platform, r["raw_id"], "done")
                        done += 1
                        log.info("[%s] hop%d user %s… score=%.2f -> %d posts", c.platform, r["hop"],
                                 r["user_uid"][:8], sc, len(tl.posts))
                    except Blocked as e:
                        log.warning("[%s] blocked: %s", c.platform, e)
                        self.db.mark_user(c.platform, r["raw_id"], "error")
                        errors += 1
                    except Exception as e:  # noqa: BLE001
                        log.exception("[%s] timeline failed: %s", c.platform, e)
                        self.db.mark_user(c.platform, r["raw_id"], "error")
                        errors += 1
            self.db.run_end(run, done, errors)

    # ----------------------------------------------------------- 3. threads
    def threads(self, platforms: list[str] | None = None) -> None:
        limit = self.s.pilot_max_threads if self.s.pilot_mode else 50_000
        for c in self.connectors(platforms):
            if type(c).thread is Connector.thread:
                continue
            run = self.db.run_start(c.platform, "threads")
            n = errors = 0
            while n < limit:
                roots = self.db.next_roots(c.platform, 50)
                if not roots:
                    break
                for r in roots:
                    try:
                        tb = c.thread(r["raw_root"])
                        self._save(c, tb)
                        self.db.mark_root(c.platform, r["raw_root"], "done")
                        n += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("[%s] thread %s failed: %s", c.platform, r["root_uid"][:8], e)
                        self.db.mark_root(c.platform, r["raw_root"], "error")
                        errors += 1
            log.info("[%s] threads collected: %d", c.platform, n)
            self.db.run_end(run, n, errors)

    # ------------------------------------------------------------- 4. label
    def label(self, max_posts: int | None = None) -> None:
        if not self.llm_ok:
            log.warning("LLM unavailable: skipping labelling")
            return
        max_posts = max_posts or (2000 if self.s.pilot_mode else 100_000)
        names = [d.name for d in self.domains]
        done = 0
        while done < max_posts:
            rows = self.db.unlabeled_posts(10)
            if not rows:
                break
            batch = [{"id": str(i), "text": r["text"]} for i, r in enumerate(rows)]
            labels = self.llm.label_posts(names, batch)
            out = []
            for i, r in enumerate(rows):
                lab = labels.get(str(i), {})
                try:
                    rel = float(lab.get("relevance", 0) or 0)
                except (TypeError, ValueError):
                    rel = 0.0
                out.append((r["post_uid"], lab.get("domain") or "unlabeled", rel,
                            json.dumps(lab, ensure_ascii=False), self.s.ollama_model))
            self.db.save_labels(out)
            done += len(rows)
            if done % 200 == 0:
                log.info("labelled %d posts", done)

    # --------------------------------------------------------------- all
    def run_all(self, platforms: list[str] | None = None) -> None:
        t0 = time.time()
        self.discover(platforms)
        self.expand(platforms)
        self.threads(platforms)
        self.label()
        for row in self.db.stats():
            log.info("STATS %s", row)
        log.info("cycle done in %.0f min", (time.time() - t0) / 60)
