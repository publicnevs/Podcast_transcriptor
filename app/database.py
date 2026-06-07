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

CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
    episode_id,
    content,
    tokenize='unicode61'
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('ntfy_topic', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('ntfy_url', 'https://ntfy.sh');
INSERT OR IGNORE INTO settings (key, value) VALUES ('check_interval_hours', '24');
"""

async def init_db():
    import os
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
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
