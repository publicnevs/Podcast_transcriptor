# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

PodScribe is a self-hosted podcast transcription hub: subscribe to RSS/YouTube/audio
feeds, auto-download new episodes, transcribe them with Gemini (or local Whisper),
and serve a searchable library with AI summaries, chapters, a synced audio player,
and a journalistic "Zeitung" (digest) generator. Deployed via Docker on a Synology NAS.
The README and most user-facing strings are in German — keep new UI text German.

## Commands

There is **no test suite, linter, or frontend build step** — the frontend is vanilla
JS/CSS served statically, no bundler.

```bash
# Run locally (needs ffmpeg installed; GEMINI_API_KEY in env or .env)
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 7878 --reload

# Run via Docker (production parity)
docker-compose up -d --build

# With the optional local Whisper backend (much larger image)
docker-compose build --build-arg INSTALL_WHISPER=true && docker-compose up -d

# Health check
curl http://localhost:7878/health
```

`requirements-whisper.txt` (faster-whisper) is only installed when `INSTALL_WHISPER=true`;
keep `app/whisper_backend.py` imports lazy so the default image works without it.

## Architecture

FastAPI backend (`app/main.py`, ~1200 lines — all HTTP routes + audio proxy) over an
async SQLite DB (`aiosqlite`, WAL mode). Background work runs via APScheduler in-process.
**Single uvicorn worker only** — SQLite WAL plus the in-process scheduler assume one process.

### Transcription is two decoupled stages (`app/transcriber.py`)
1. **Transcription** (audio → timestamped segments) via the selected `backend`: `gemini` or `whisper`.
2. **Enrichment** (text → summary / takeaways / chapters) — *always* a cheap Gemini text call.

This split lets local Whisper do the heavy audio work while Gemini adds summaries from
plain text for almost no cost. `app/transcript_fetch.py` adds a fast path: if a feed already
ships a transcript (`transcript_url`), the pipeline skips download + audio transcription
entirely and only runs text enrichment.

### Episode pipeline (`app/processor.py`)
`process_episode()` orchestrates: feed-transcript fast path → else download (`downloader.py`,
yt-dlp) → transcribe → write `transcripts` + `summaries` rows → manual `transcripts_fts`
insert → auto-tag (`tagging.py`) → ntfy push (`notifier.py`). Downloaded audio is deleted in
`finally`. Status flows through `episodes.status`: `pending → queued → downloading →
transcribing → done | error`. The scheduler enqueues (`status='queued'`); `process_queued()`
drains 3 at a time.

### Scheduler (`app/scheduler.py`)
`check_all_feeds` runs hourly (only podcasts with `auto_transcribe=1`); `run_due_issues`
runs every 30 min to generate scheduled newsletters whose `cron_dow`/`cron_hour` match now
(de-duped by `last_run_at`). Started from the FastAPI `lifespan` handler.

### Runtime config precedence
Settings live in the DB `settings` table (editable in the app UI, no restart). At startup
and after any settings change, `_apply_runtime_config()` in `main.py` pushes DB values into
`transcriber.configure()`. **DB setting wins; env var is only the fallback.** So changing
`GEMINI_API_KEY`/`TRANSCRIPTION_BACKEND` in `.env` has no effect once a value is stored in
the DB — change it in the app's Settings page instead.

### Gemini models
Default to `gemini-2.5-flash` (transcription/enrichment/tagging) and `gemini-2.5-pro`
(digests), overridable via `GEMINI_FLASH_MODEL` / `GEMINI_PRO_MODEL`. (The README still says
1.5 — that's outdated; 1.5 was retired.) Audio is sent as `inline_data`, not the Files API.

### Database (`app/database.py`)
Schema is created idempotently in `init_db()` via `executescript(SCHEMA)`. Because
`CREATE TABLE IF NOT EXISTS` won't add columns to an existing table, **new columns must be
added as `ALTER TABLE` statements in the `_MIGRATIONS` list** (each wrapped in try/except so
re-running is safe) — not just edited into `SCHEMA`. `transcripts_fts` is a manually-synced
FTS5 table (inserted in `processor.py`), not a trigger-backed shadow table.

Key tables: `podcasts`, `episodes`, `transcripts` (1:1, `segments_json` drives player sync),
`summaries`, `notes`, `digests`, `tags`/`tag_aliases`/`episode_tags`, and
`issue_recipes`/`scheduled_issues` (the "Zeitung 2.0" recurring-newsletter feature).

### Frontend (`app/static/`)
Per-page HTML (`index`, `podcast`, `episode`, `digest`, `settings`, `about`) + shared
`app.js` (nav, AudioPlayer, service-worker registration) and `style.css` (CSS-variable design
system, dark mode). PWA via `sw.js` + `manifest.json`. The audio player seeks by clicking
transcript paragraphs, backed by the Range-supporting `/api/episodes/{id}/audio` proxy.
Bump the cache version in `sw.js` when changing cached static assets.
