"""Local LLM "brain" (Ollama, e.g. qwen3:14b on the GPU). No paid API, no cloud.

Roles:
  - propose_queries(domain)       -> search terms / hashtags / subreddits / channels to explore
  - score_accounts(domain, list)  -> keep only accounts really active in the domain
  - label_posts(list)             -> domain + relevance + context (topic, intent, stance, entities)
"""
from __future__ import annotations

import json
import logging
import re

import httpx

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def parse_json(text: str):
    """Robust JSON extraction from an LLM answer (strips <think>, code fences, trailing prose)."""
    if text is None:
        return None
    t = _THINK_RE.sub("", text).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = t.find(open_c), t.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


class LocalLLM:
    def __init__(self, url: str, model: str, enabled: bool = True, timeout: float = 300):
        self.url = url.rstrip("/")
        self.model = model
        self.enabled = enabled
        self.http = httpx.Client(timeout=timeout)

    def available(self) -> bool:
        if not self.enabled:
            return False
        try:
            r = self.http.get(f"{self.url}/api/tags", timeout=5)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            ok = any(n == self.model or n.startswith(self.model.split(":")[0]) for n in names)
            if not ok:
                log.warning("Ollama up but model %s not pulled (have: %s)", self.model, names)
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("Ollama not reachable at %s: %s", self.url, e)
            return False

    def ask_json(self, system: str, user: str, temperature: float = 0.2):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": "json",
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_ctx": 8192},
        }
        try:
            r = self.http.post(f"{self.url}/api/chat", json=payload)
            r.raise_for_status()
            return parse_json(r.json().get("message", {}).get("content", ""))
        except Exception as e:  # noqa: BLE001
            log.warning("LLM call failed: %s", e)
            return None

    # ------------------------------------------------------------------ roles
    def propose_queries(self, domain: str, description: str, keywords: list[str], platform: str, n: int = 15) -> list[str]:
        kinds = {
            "bluesky": "search phrases and hashtags (without #) used on Bluesky",
            "mastodon": "real hashtags (without #) exactly as people type them on Mastodon (e.g. ClimateChange, LLM), no underscores, no invented tags",
            "reddit": "subreddit names (without r/)",
            "telegram": "usernames (without @) of REAL, existing, well-known public Telegram channels (news outlets, organisations). Only names you are certain exist; return fewer rather than inventing",
            "dataset_x": ("short search queries (2-5 words, English) to find PUBLIC research datasets of tweets "
                          "(Twitter/X posts) about this domain on Hugging Face or Zenodo, e.g. 'climate tweets 2023'"),
            "dataset_weibo": ("short search queries (English or Chinese) to find PUBLIC research datasets of Sina Weibo "
                              "posts about this domain on Hugging Face or Zenodo, e.g. 'weibo health', '微博 数据集'"),
            "dataset_reddit": ("short search queries (2-5 words, English) to find PUBLIC research datasets of Reddit "
                               "posts or comments about this domain on Hugging Face or Zenodo, e.g. 'reddit climate'"),
            "dataset_facebook": ("short search queries (2-5 words, English or French) to find PUBLIC research datasets "
                                 "of Facebook posts or comments about this domain, e.g. 'facebook posts health'"),
            "dataset_instagram": ("short search queries (2-5 words, English or French) to find PUBLIC research datasets "
                                  "of Instagram posts/captions about this domain, e.g. 'instagram captions sport'"),
            "telegram_api": "short keyword searches (1-3 words, English or French) to find public Telegram channels and discussion groups by name or title",
            "youtube": "YouTube search queries",
            "lemmy": "short search queries (2-4 words) that would match post titles on Lemmy, the fediverse Reddit alternative",
            "stackexchange": ("Stack Exchange 'site:tag' pairs, using ONLY these sites: stackoverflow, ai, datascience, "
                              "sustainability, earthscience, health, fitness, medicalsciences, politics, law, sports, "
                              "outdoors, movies, music, scifi, gaming, literature, cooking, travel, economics, history. "
                              "Tags are lowercase with hyphens (e.g. politics:elections, ai:large-language-models)"),
        }
        sys = ("You help a PhD researcher build a social-media corpus (2023-2026) for context-aware user "
               "interest modelling. Answer ONLY with JSON: {\"items\": [\"...\"]}. No explanations.")
        usr = (f"Domain: {domain}\nDescription: {description}\nKnown keywords: {', '.join(keywords)}\n"
               f"Give {n} {kinds.get(platform, 'search queries')} that would surface ordinary users "
               f"discussing this domain in English or French. Mix generic and niche terms.")
        out = self.ask_json(sys, usr, temperature=0.5)
        items = out.get("items", []) if isinstance(out, dict) else (out if isinstance(out, list) else [])
        clean = []
        for it in items:
            if isinstance(it, str):
                s = it.strip().lstrip("#@").removeprefix("r/").strip()
                if 1 < len(s) < 80 and s.lower() not in {c.lower() for c in clean}:
                    clean.append(s)
        return clean[:n]

    def score_accounts(self, domain: str, description: str, accounts: list[dict],
                       entity: str = "account") -> dict[str, float]:
        """accounts: [{"id": key, "bio": str, "sample": [post texts]}] -> {id: score 0..1}

        entity="channel" (Telegram): channels are broadcast by media, organisations or creators, so
        they are judged on topical fit instead of being rejected for not being individuals."""
        if not accounts:
            return {}
        if entity == "channel":
            sys = ("You are a strict annotator. Each item is a public Telegram channel or group. Score how "
                   "much it regularly posts real content about the domain. News outlets, organisations, "
                   "experts, creators and discussion groups are fine. Give 0 to spam, ads, casinos, "
                   "betting, crypto pumps, giveaways, adult content or unrelated topics. "
                   "Answer ONLY JSON: {\"scores\": {\"<id>\": <0..1>}}")
        else:
            sys = ("You are a strict annotator. For each account decide how much it is a genuine individual "
                   "regularly posting about the domain (not a brand, not a bot, not spam). "
                   "Answer ONLY JSON: {\"scores\": {\"<id>\": <0..1>}}")
        lines = []
        for a in accounts:
            sample = " | ".join((a.get("sample") or [])[:4])[:600]
            lines.append(f"- id={a['id']} bio=\"{(a.get('bio') or '')[:200]}\" posts=\"{sample}\"")
        usr = f"Domain: {domain} — {description}\nAccounts:\n" + "\n".join(lines)
        out = self.ask_json(sys, usr)
        scores = out.get("scores", {}) if isinstance(out, dict) else {}
        res = {}
        for k, v in scores.items():
            try:
                res[str(k)] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue
        return res

    def judge_dataset(self, platform: str, domains: list[tuple[str, str]], card: str) -> dict:
        """Read a dataset card. -> {platform_match, has_post_text, relevant, domains, period, reason}"""
        names = {"x": "X / Twitter (tweets)", "weibo": "Sina Weibo (微博 posts)", "reddit": "Reddit (posts or comments)",
                 "facebook": "Facebook (posts or comments)", "instagram": "Instagram (posts, captions or comments)"}
        sys = ("You screen public research datasets for a PhD corpus of social-media posts (2023-2026) on "
               "context-aware user interest modelling and community detection. Read the dataset card and answer "
               "ONLY JSON: {\"platform_match\": bool (rows are posts/comments FROM the named platform), "
               "\"has_post_text\": bool (the post text itself is included, not only IDs to re-hydrate), "
               "\"relevant\": bool (useful for at least one domain, or general-topic posts), "
               "\"domains\": [domain names], \"period\": \"years covered or unknown\", "
               "\"reason\": \"one short sentence\"}. Be strict: benchmarks of tiny size (<500 posts), "
               "pure image datasets, ID-only releases and synthetic/LLM-generated posts are not acceptable.")
        dl = "\n".join(f"- {n}: {d}" for n, d in domains)
        out = self.ask_json(sys, f"Platform wanted: {names.get(platform, platform)}\nDomains:\n{dl}\n\nCARD:\n{card[:7000]}")
        return out if isinstance(out, dict) else {"reason": "LLM gave no answer"}

    def map_columns(self, platform: str, sample_row: dict) -> dict[str, str]:
        """Ask which column holds the post text / id / date / user... -> {field: column}"""
        sys = ("Map dataset columns to fields. Answer ONLY JSON with any of these keys whose column exists: "
               "text (the post text), id (post id), date (creation date), user (author id or name), "
               "parent (id of the post it replies to / reposts), root (conversation id), lang, hashtags. "
               "Values are exact column names from the row. Omit keys you cannot find.")
        out = self.ask_json(sys, f"Platform: {platform}\nRow: {json.dumps(sample_row, ensure_ascii=False)[:3000]}")
        return {k: v for k, v in out.items() if isinstance(v, str)} if isinstance(out, dict) else {}

    def label_posts(self, domains: list[str], posts: list[dict]) -> dict[str, dict]:
        """posts: [{"id", "text"}] -> {id: {domain, relevance, topic, intent, stance, entities}}"""
        if not posts:
            return {}
        sys = ("Annotate social-media posts for context-aware interest modelling. For each post return: "
               "domain (one of the given list, or 'other'), relevance 0..1 to that domain, topic (2-5 words), "
               "intent (inform|opinion|question|share|promote|social|other), stance (positive|negative|neutral), "
               "entities (list, max 5). Domains are open: if 'other', propose a short new domain name in 'new_domain'. "
               "Answer ONLY JSON: {\"labels\": {\"<id>\": {...}}}")
        body = "\n".join(f"- id={p['id']}: {p['text'][:500]!r}" for p in posts)
        out = self.ask_json(sys, f"Domains: {', '.join(domains)}\nPosts:\n{body}")
        labels = out.get("labels", {}) if isinstance(out, dict) else {}
        return {str(k): v for k, v in labels.items() if isinstance(v, dict)}
