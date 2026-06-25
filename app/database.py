import os
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "/app/data/podscribe.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS podcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    rss_url TEXT UNIQUE NOT NULL,
    artwork_url TEXT,
    description TEXT,
    website_url TEXT,
    language TEXT,
    auto_transcribe INTEGER DEFAULT 0,
    check_interval_hours INTEGER DEFAULT 24,
    last_checked TIMESTAMP,
    max_episodes INTEGER DEFAULT 0,
    feed_type TEXT DEFAULT 'podcast',
    full_text_extraction INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    podcast_id INTEGER REFERENCES podcasts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    audio_url TEXT NOT NULL,
    episode_url TEXT,
    pub_date TIMESTAMP,
    duration_sec INTEGER,
    description TEXT,
    status TEXT DEFAULT 'pending',
    error_msg TEXT,
    read_at TIMESTAMP,
    watchlist INTEGER DEFAULT 0,
    scroll_pos INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER UNIQUE REFERENCES episodes(id) ON DELETE CASCADE,
    content TEXT,
    segments_json TEXT,
    language TEXT,
    word_count INTEGER,
    model_used TEXT DEFAULT 'gemini-1.5-flash',
    translation_de TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER UNIQUE REFERENCES episodes(id) ON DELETE CASCADE,
    summary TEXT,
    takeaways_json TEXT,
    chapters_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER UNIQUE REFERENCES episodes(id) ON DELETE CASCADE,
    content TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    mode TEXT,
    episode_ids_json TEXT,
    content_html TEXT DEFAULT '',
    content_md TEXT DEFAULT '',
    status TEXT DEFAULT 'generating',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    position INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    kind TEXT DEFAULT 'topic',
    episode_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tag_aliases (
    alias_slug TEXT PRIMARY KEY,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS episode_tags (
    episode_id INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    source TEXT DEFAULT 'auto',
    confidence REAL DEFAULT 1.0,
    PRIMARY KEY (episode_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_episode_tags_tag ON episode_tags(tag_id);

CREATE TABLE IF NOT EXISTS issue_recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    format TEXT DEFAULT 'newsletter',
    date_window_days INTEGER DEFAULT 7,
    date_from TIMESTAMP,
    date_to TIMESTAMP,
    podcast_ids_json TEXT DEFAULT '[]',
    tag_ids_json TEXT DEFAULT '[]',
    match_mode TEXT DEFAULT 'any',
    length INTEGER DEFAULT 3,
    style INTEGER DEFAULT 3,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduled_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER REFERENCES issue_recipes(id) ON DELETE CASCADE,
    cron_dow INTEGER DEFAULT 0,
    cron_hour INTEGER DEFAULT 7,
    enabled INTEGER DEFAULT 1,
    last_run_at TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
    episode_id,
    content,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS episode_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
    chunk_idx INTEGER DEFAULT 0,
    start_time TEXT DEFAULT '00:00:00',
    text TEXT,
    vector BLOB
);
CREATE INDEX IF NOT EXISTS idx_episode_chunks_ep ON episode_chunks(episode_id);

-- Per-feed check history: one row per poll (RSS / website / newsletter), so the
-- statistics page can show how many updates each feed produced and when.
CREATE TABLE IF NOT EXISTS feed_check_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    podcast_id INTEGER REFERENCES podcasts(id) ON DELETE CASCADE,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    new_episodes INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 1,
    error_msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_check_log ON feed_check_log(podcast_id, checked_at);

-- Per-episode processing history: what was loaded/transcribed and whether it
-- succeeded, so the statistics page can show a success/failure protocol.
CREATE TABLE IF NOT EXISTS processing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
    podcast_id INTEGER REFERENCES podcasts(id) ON DELETE SET NULL,
    action TEXT,
    ok INTEGER DEFAULT 1,
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_processing_log ON processing_log(created_at);

-- Named read-only "friend" logins (up to 10). Owner stays in the settings table.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'guest',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('ntfy_topic', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ntfy_url', 'https://ntfy.sh');
INSERT OR IGNORE INTO settings (key, value) VALUES ('check_interval_hours', '24');
INSERT OR IGNORE INTO settings (key, value) VALUES ('transcription_backend', 'gemini');
INSERT OR IGNORE INTO settings (key, value) VALUES ('whisper_model', 'base');
INSERT OR IGNORE INTO settings (key, value) VALUES ('gemini_api_key', '');
-- Newsletter inbox (IMAP)
INSERT OR IGNORE INTO settings (key, value) VALUES ('newsletter_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('newsletter_imap_host', 'imap.hosting.de');
INSERT OR IGNORE INTO settings (key, value) VALUES ('newsletter_imap_port', '993');
INSERT OR IGNORE INTO settings (key, value) VALUES ('newsletter_imap_user', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('newsletter_imap_password', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('newsletter_check_interval_hours', '24');
-- Digest email delivery (SMTP)
INSERT OR IGNORE INTO settings (key, value) VALUES ('digest_email_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('digest_email_to', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('smtp_host', 'smtp.hosting.de');
INSERT OR IGNORE INTO settings (key, value) VALUES ('smtp_port', '465');
INSERT OR IGNORE INTO settings (key, value) VALUES ('smtp_user', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('smtp_password', '');
-- Deep links in push notifications
INSERT OR IGNORE INTO settings (key, value) VALUES ('public_base_url', '');
-- Access mode: open (read-by-default once owner set) | guest_password | friends_only
INSERT OR IGNORE INTO settings (key, value) VALUES ('access_mode', 'open');
-- Tageszeitung (daily AI newspaper)
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_hour', '7');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_email_to', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_length', '4');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_style', '3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_focus', '');
-- Scraping: optional headless-browser rendering for JS-only/paywalled pages
INSERT OR IGNORE INTO settings (key, value) VALUES ('js_render_enabled', '0');
-- Audio: skip downloads longer than this (minutes) — keeps under Gemini's limit
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_audio_minutes', '180');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_category_ids_json', '[]');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_podcast_ids_json', '[]');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tageszeitung_last_run', '');
"""

# Additive migrations for existing databases (CREATE TABLE IF NOT EXISTS
# won't add new columns to an already-existing table).
_MIGRATIONS = [
    "ALTER TABLE episodes ADD COLUMN transcript_url TEXT",
    "ALTER TABLE episodes ADD COLUMN transcript_type TEXT",
    # Zeitung 2.0
    "ALTER TABLE digests ADD COLUMN format TEXT DEFAULT 'zeitung'",
    "ALTER TABLE digests ADD COLUMN length INTEGER DEFAULT 3",
    "ALTER TABLE digests ADD COLUMN style INTEGER DEFAULT 3",
    "ALTER TABLE digests ADD COLUMN sections_json TEXT DEFAULT '[]'",
    "ALTER TABLE digests ADD COLUMN tldr_md TEXT DEFAULT ''",
    "ALTER TABLE digests ADD COLUMN subtitle TEXT DEFAULT ''",
    "ALTER TABLE digests ADD COLUMN recipe_json TEXT DEFAULT ''",
    "ALTER TABLE digests ADD COLUMN share_token TEXT",
    # CR 10: RSS Newsfeed support
    "ALTER TABLE podcasts ADD COLUMN feed_type TEXT DEFAULT 'podcast'",
    "ALTER TABLE podcasts ADD COLUMN full_text_extraction INTEGER DEFAULT 0",
    # Retention: max number of transcripts to keep per feed (0 = unlimited)
    "ALTER TABLE podcasts ADD COLUMN max_transcripts INTEGER DEFAULT 0",
    # Dedup hard guard for newsfeed articles (partial: only non-empty links).
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_ep_url ON episodes(podcast_id, episode_url) WHERE episode_url <> ''",
    # UX: track when active processing started (for elapsed-time display)
    "ALTER TABLE episodes ADD COLUMN processing_started_at TIMESTAMP",
    # UX: feed health tracking
    "ALTER TABLE podcasts ADD COLUMN last_fetch_error TEXT",
    "ALTER TABLE podcasts ADD COLUMN consecutive_fetch_errors INTEGER DEFAULT 0",
    # German summaries: track language of generated summary
    "ALTER TABLE summaries ADD COLUMN summary_lang TEXT",
    # Extended recipe fields for new formats
    "ALTER TABLE issue_recipes ADD COLUMN model TEXT DEFAULT ''",
    "ALTER TABLE issue_recipes ADD COLUMN custom_style TEXT DEFAULT ''",
    "ALTER TABLE issue_recipes ADD COLUMN focus TEXT DEFAULT ''",
    # User-managed categories + manual ordering for the library homepage
    "ALTER TABLE podcasts ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL",
    "ALTER TABLE podcasts ADD COLUMN position INTEGER DEFAULT 0",
    # Surface why an issue/digest failed so the reader can show it + offer retry
    "ALTER TABLE digests ADD COLUMN error_msg TEXT",
    # Performance: composite index for the inbox and per-podcast episode listings
    "CREATE INDEX IF NOT EXISTS idx_episodes_podcast_status ON episodes(podcast_id, status, pub_date DESC)",
    # Performance: episode_tags was only indexed by tag_id; episode_id lookups were full scans
    "CREATE INDEX IF NOT EXISTS idx_episode_tags_episode ON episode_tags(episode_id)",
]


async def _backfill_pub_dates(db):
    """One-time conversion of legacy RFC-822 pub_date strings to sortable ISO.

    Older rows stored the raw feed string (e.g. 'Wed, 08 Jun 2026 …') which sorts
    wrong as text. RFC-822 always contains the weekday comma, so the LIKE '%,%'
    guard makes this idempotent — once converted (no comma) rows are skipped.
    """
    import email.utils
    async with db.execute(
        "SELECT id, pub_date FROM episodes WHERE pub_date LIKE '%,%' AND pub_date != ''"
    ) as cur:
        rows = await cur.fetchall()
    for ep_id, raw in rows:
        try:
            iso = email.utils.parsedate_to_datetime(raw).strftime("%Y-%m-%d %H:%M:%S")
            await db.execute("UPDATE episodes SET pub_date=? WHERE id=?", (iso, ep_id))
        except Exception:
            pass  # unparseable → leave as-is


async def init_db():
    import os
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                await db.execute(stmt)
            except Exception:
                pass  # column already exists
        await _backfill_pub_dates(db)
        # Bootstrap a stable session secret so signed cookies survive restarts.
        async with db.execute("SELECT value FROM settings WHERE key='session_secret'") as cur:
            row = await cur.fetchone()
        if not row or not row[0]:
            import secrets as _secrets
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('session_secret', ?)",
                (_secrets.token_hex(32),),
            )
        await db.commit()

async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else ""

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value)
        )
        await db.commit()
