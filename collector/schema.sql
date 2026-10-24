-- Context_SocialMedia schema (PostgreSQL 16). All *_uid columns are HMAC pseudonyms.

CREATE TABLE IF NOT EXISTS users (
    user_uid            TEXT PRIMARY KEY,
    platform            TEXT NOT NULL,
    bio                 TEXT,
    account_created_at  TIMESTAMPTZ,
    followers_count     INTEGER,
    following_count     INTEGER,
    posts_count         INTEGER,
    is_bot_flag         BOOLEAN,
    bot_score           REAL,
    domain_hint         TEXT,          -- domain through which the user entered the sample
    hop                 SMALLINT,      -- 0 = seed, 1, 2
    backfilled_until    TIMESTAMPTZ,   -- oldest post date already collected
    first_seen_at       TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS interest_tags TEXT[];

CREATE TABLE IF NOT EXISTS posts (
    post_uid         TEXT PRIMARY KEY,
    platform         TEXT NOT NULL,
    author_uid       TEXT NOT NULL,
    text             TEXT,
    created_at       TIMESTAMPTZ,
    lang             TEXT,
    hashtags         TEXT[],
    mentions_uid     TEXT[],
    urls             TEXT[],
    media_type       TEXT,
    media_alt_text   TEXT,
    parent_post_uid  TEXT,
    root_post_uid    TEXT,
    quote_of_uid     TEXT,
    like_count       INTEGER,
    repost_count     INTEGER,
    reply_count      INTEGER,
    community        TEXT,
    source_method    TEXT,
    collected_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS posts_author_idx ON posts (author_uid);
CREATE INDEX IF NOT EXISTS posts_root_idx ON posts (root_post_uid);
CREATE INDEX IF NOT EXISTS posts_created_idx ON posts (created_at);

CREATE TABLE IF NOT EXISTS edges (
    src_uid     TEXT NOT NULL,
    dst_uid     TEXT NOT NULL,
    platform    TEXT NOT NULL,
    type        TEXT NOT NULL,     -- reply | mention | repost | quote | follow
    post_uid    TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ,
    PRIMARY KEY (src_uid, dst_uid, type, post_uid)
);

CREATE TABLE IF NOT EXISTS seeds (
    seed_id   BIGSERIAL PRIMARY KEY,
    platform  TEXT NOT NULL,
    domain    TEXT NOT NULL,
    kind      TEXT NOT NULL,
    value     TEXT NOT NULL,       -- raw handle/tag/subreddit; kept only to re-run discovery
    origin    TEXT NOT NULL DEFAULT 'manual',  -- manual | llm
    accepted  BOOLEAN NOT NULL DEFAULT TRUE,
    added_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (platform, kind, value)
);

-- Work queue: raw platform identifiers are needed to call the APIs. Kept in a separate table,
-- never exported, deletable after collection (scripts: python -m collector purge-queue).
CREATE TABLE IF NOT EXISTS crawl_queue (
    platform    TEXT NOT NULL,
    raw_id      TEXT NOT NULL,
    user_uid    TEXT NOT NULL,
    domain      TEXT,
    hop         SMALLINT NOT NULL DEFAULT 0,
    priority    REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'todo',   -- todo | done | skipped | error
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (platform, raw_id)
);

CREATE TABLE IF NOT EXISTS thread_queue (
    platform    TEXT NOT NULL,
    raw_root    TEXT NOT NULL,
    root_uid    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'todo',
    PRIMARY KEY (platform, raw_root)
);

CREATE TABLE IF NOT EXISTS llm_labels (
    post_uid     TEXT PRIMARY KEY,
    domain       TEXT,
    relevance    REAL,
    context_json JSONB,
    model        TEXT,
    labeled_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reid_map (      -- only filled if KEEP_REID_TABLE=true
    uid      TEXT PRIMARY KEY,
    platform TEXT,
    raw_id   TEXT
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id      BIGSERIAL PRIMARY KEY,
    platform    TEXT,
    job         TEXT,
    started_at  TIMESTAMPTZ DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    items       INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    note        TEXT
);
