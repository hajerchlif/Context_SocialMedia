"""Settings from environment variables (.env) and YAML/CSV files in config/."""
from __future__ import annotations

import csv
import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:  # .env is optional (Docker passes env vars directly)
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "config"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _date(value: str) -> dt.datetime:
    if not value or value.lower() == "today":
        return dt.datetime.now(dt.timezone.utc)
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)


@dataclass
class Domain:
    name: str
    description: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class Seed:
    platform: str
    domain: str
    kind: str  # account | feed | subreddit | hashtag | channel | query | video
    value: str


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://collector:collector@localhost:5432/socialmedia"))
    hmac_secret: str = field(default_factory=lambda: _env("HMAC_SECRET"))
    keep_reid_table: bool = field(default_factory=lambda: _bool("KEEP_REID_TABLE"))
    date_from: dt.datetime = field(default_factory=lambda: _date(_env("DATE_FROM", "2023-01-01")))
    date_to: dt.datetime = field(default_factory=lambda: _date(_env("DATE_TO", "today")))
    langs: list[str] = field(default_factory=lambda: [x for x in _env("LANGS", "en,fr").split(",") if x])
    pilot_mode: bool = field(default_factory=lambda: _bool("PILOT_MODE", True))
    contact_email: str = field(default_factory=lambda: _env("CONTACT_EMAIL", "research@example.org"))

    # LLM (local, Ollama)
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "qwen3:14b"))
    use_llm: bool = field(default_factory=lambda: _bool("USE_LLM", True))

    # Bluesky (free app password: Settings > Privacy and security > App passwords)
    bsky_handle: str = field(default_factory=lambda: _env("BSKY_HANDLE"))
    bsky_app_password: str = field(default_factory=lambda: _env("BSKY_APP_PASSWORD"))

    # Mastodon
    mastodon_instances: list[str] = field(default_factory=lambda: [x.strip() for x in _env("MASTODON_INSTANCES", "mastodon.social").split(",") if x.strip()])

    # Reddit (free non-commercial OAuth app) + archives
    reddit_client_id: str = field(default_factory=lambda: _env("REDDIT_CLIENT_ID"))
    reddit_client_secret: str = field(default_factory=lambda: _env("REDDIT_CLIENT_SECRET"))
    reddit_archive_dir: str = field(default_factory=lambda: _env("REDDIT_ARCHIVE_DIR", "data/reddit_dumps"))

    # Lemmy (fediverse, Reddit-like; public API, no key)
    lemmy_instance: str = field(default_factory=lambda: _env("LEMMY_INSTANCE", "lemmy.world"))
    enable_lemmy: bool = field(default_factory=lambda: _bool("ENABLE_LEMMY", True))

    # Stack Exchange (free; 300 req/day without key, 10k/day with a free key)
    stackexchange_key: str = field(default_factory=lambda: _env("STACKEXCHANGE_KEY"))
    enable_stackexchange: bool = field(default_factory=lambda: _bool("ENABLE_STACKEXCHANGE", True))

    # YouTube (free API key, daily quota)
    youtube_api_key: str = field(default_factory=lambda: _env("YOUTUBE_API_KEY"))

    # Telegram public channels (web preview t.me/s/<channel>, no account)
    enable_telegram_web: bool = field(default_factory=lambda: _bool("ENABLE_TELEGRAM_WEB", True))
    # Telegram official API (free, https://my.telegram.org): keyword search, public groups, comments.
    # When set and logged in (python -m collector telegram-login) it replaces the web preview.
    telegram_api_id: str = field(default_factory=lambda: _env("TELEGRAM_API_ID"))
    telegram_api_hash: str = field(default_factory=lambda: _env("TELEGRAM_API_HASH"))
    telegram_session: str = field(default_factory=lambda: _env("TELEGRAM_SESSION"))

    # Public research datasets (Hugging Face Hub, Zenodo) for platforms we cannot collect from (X, Weibo)
    datasets_auto: bool = field(default_factory=lambda: _bool("DATASETS_AUTO", True))  # in every `run` cycle
    datasets_platforms: list[str] = field(default_factory=lambda: [x.strip() for x in _env("DATASETS_PLATFORMS", "x,weibo").split(",") if x.strip()])
    datasets_max_rows: int = field(default_factory=lambda: int(_env("DATASETS_MAX_ROWS", "200000")))
    datasets_max_mb: int = field(default_factory=lambda: int(_env("DATASETS_MAX_MB", "2000")))
    datasets_respect_dates: bool = field(default_factory=lambda: _bool("DATASETS_RESPECT_DATES", True))
    datasets_allow_unknown_license: bool = field(default_factory=lambda: _bool("DATASETS_ALLOW_UNKNOWN_LICENSE", False))
    datasets_allow_scraped: bool = field(default_factory=lambda: _bool("DATASETS_ALLOW_SCRAPED", False))
    hf_token: str = field(default_factory=lambda: _env("HF_TOKEN"))

    # Time balance (same budget for every month DATE_FROM..DATE_TO)
    discovery_posts_per_month: int = field(default_factory=lambda: int(_env("DISCOVERY_POSTS_PER_MONTH", "0")))
    posts_per_user_month: int = field(default_factory=lambda: int(_env("POSTS_PER_USER_MONTH", "40")))
    max_scan_per_user: int = field(default_factory=lambda: int(_env("MAX_SCAN_PER_USER", "0")))

    # Limits
    max_users_per_domain_hop1: int = field(default_factory=lambda: int(_env("MAX_USERS_HOP1", "2000")))
    max_users_per_domain_hop2: int = field(default_factory=lambda: int(_env("MAX_USERS_HOP2", "5000")))
    min_posts_per_user: int = field(default_factory=lambda: int(_env("MIN_POSTS_PER_USER", "20")))
    pilot_max_threads: int = field(default_factory=lambda: int(_env("PILOT_MAX_THREADS", "2000")))

    @property
    def user_agent(self) -> str:
        return f"ContextSocialMediaResearch/0.1 (+mailto:{self.contact_email})"

    def validate(self) -> None:
        if not self.hmac_secret or self.hmac_secret.startswith("change_me"):
            raise SystemExit("HMAC_SECRET is missing in .env (run scripts/deploy_ubuntu.sh or set a random 64-hex value).")


def load_domains(path: Path | None = None) -> list[Domain]:
    path = path or CONFIG_DIR / "domains.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Domain(name=d["name"], description=d.get("description", ""), keywords=d.get("keywords", []) or []) for d in data.get("domains", [])]


def load_seeds(seed_dir: Path | None = None) -> list[Seed]:
    seed_dir = seed_dir or CONFIG_DIR / "seeds"
    seeds: list[Seed] = []
    if not seed_dir.exists():
        return seeds
    for f in sorted(seed_dir.glob("*.csv")):
        platform = f.stem
        with f.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                value = (row.get("value") or "").strip()
                if not value or "XXXX" in value or value.startswith("exemple"):
                    continue  # skip template placeholders
                seeds.append(Seed(platform=platform, domain=row["domain"].strip(), kind=row["kind"].strip(), value=value))
    return seeds


settings = Settings()
