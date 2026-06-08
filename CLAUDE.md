# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PodScribe is a self-hosted podcast transcription and reader app. It downloads podcast audio, transcribes it via Google Gemini 2.5 Flash (cloud) or faster-whisper (local), and presents a synced audio/transcript reader as a PWA.

## Development Commands

**Run locally:**
```bash
pip install -r requirements.txt
export GEMINI_API_KEY="AIza..."
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 7878
```

**Run with Docker:**
```bash
# Copy and edit .env with GEMINI_API_KEY
docker-compose up -d

# With local Whisper support:
docker-compose build --build-arg INSTALL_WHISPER=true
docker-compose up -d
```

**Health check:**
```bash
curl http://localhost:7878/health
```

There is no test suite — manual testing via the browser UI is the primary verification method.

## Architecture

### Backend (`app/`)

- **`main.py`** — FastAPI app (~1500 lines). All API routes (`/api/*`), lifespan hooks (DB init + scheduler start), and static SPA mount. This is the single backend file for routes.
- **`processor.py`** — Orchestrates the full episode pipeline: download audio → transcribe → enrich → auto-tag → update DB. Status transitions: `pending → downloading → transcribing → done` (or `error`).
- **`transcriber.py`** — Gemini API calls. Models are env-overridable (`GEMINI_FLASH_MODEL`, `GEMINI_PRO_MODEL`, `GEMINI_LITE_MODEL`), defaulting to `gemini-2.5-flash` / `gemini-2.5-pro` / `gemini-2.5-flash-lite`. Three paths:
  - Audio upload → `transcribe_audio()` → returns structured JSON (segments with timestamps, speakers, summary, takeaways, chapters)
  - Text-only → `enrich_text()` → used after Whisper transcription or when feed provides a transcript
  - Digest generation → `generate_digest()` → Gemini Pro writes 1200–2000 word articles
- **`whisper_backend.py`** — Optional faster-whisper local inference. Returns transcript only; `transcriber.enrich_text()` handles enrichment.
- **`downloader.py`** — yt-dlp wrapper. Downloads audio and re-encodes via ffmpeg to 32kbps mono 16kHz MP3 (keeps a 60-min episode ≈14 MB, under Gemini's 20 MB inline limit).
- **`transcript_fetch.py`** — Fetches and parses pre-existing feed transcripts (`<podcast:transcript>`): VTT / SRT / JSON / plain-text → normalized `{time, speaker, text}` segments. Used by the fast path to skip audio download + audio transcription.
- **`feed_parser.py`** — RSS/OPML parsing, Spotify redirect resolution, Podcast Index `<podcast:transcript>` tag detection (decides whether the fast path applies).
- **`tagging.py`** — Canonical topic tagging with alias-based de-dup: slugify → alias lookup → exact slug → fuzzy merge (`difflib`, threshold 0.82) → else create new tag. Writes `tags` / `tag_aliases` / `episode_tags`.
- **`database.py`** — All SQLite queries via aiosqlite. Schema: 12 tables + FTS5 virtual table (`settings`, `podcasts`, `episodes`, `transcripts`, `summaries`, `notes`, `digests`, `tags`, `tag_aliases`, `episode_tags`, `issue_recipes`, `scheduled_issues`). Key design: denormalized counts (`unread_count`, `done_count`) on `podcasts` for UI performance; `segments_json` in `transcripts` stores timestamped segments for player sync. New columns are added via additive `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` migrations near the bottom of the file.
- **`scheduler.py`** — APScheduler jobs: hourly feed checks, scheduled digest generation (cron-style via `scheduled_issues` table).
- **`exporter.py`** — TXT, Markdown, PDF, "AI Copy" (XML) export formatters.
- **`notifier.py`** — ntfy.sh push notifications (topic/URL from `settings`); fails silently when unconfigured.

### Frontend (`app/static/`)

Vanilla JS SPA — no build step, no framework. Pages share `app.js` utilities.

- **`episode.html` + `app.js`** — The most complex page. Audio player syncs with transcript: clicking a paragraph seeks audio; during playback, current paragraph highlights and auto-scrolls. Uses `segments_json` from the API for timestamp mapping.
- **`sw.js`** — Service Worker caches transcript pages for offline reading.
- **`style.css`** — CSS variables design system, dark mode, mobile-first (bottom nav) + desktop (top nav) responsive layout.

### Data Flow

1. User adds RSS feed → `feed_parser` checks for episodes → new episodes inserted as `pending`
2. Scheduler or manual trigger → `processor` picks up `queued`/`pending` episodes
3. Fast path: feed has transcript URL → `transcript_fetch.fetch_transcript()` parses it → `enrich_text()` → done
4. Standard path: `downloader` fetches audio (yt-dlp) → ffmpeg to 32kbps mono 16kHz MP3 → `transcribe_audio()` → structured JSON stored in `transcripts` + `summaries`
5. Auto-tagging (`tagging.py`): Gemini Flash reads summary+takeaways+chapters → generates tag slugs → stored in `tags` with alias dedup

### Key Design Decisions

- **Single-file backend**: All routes in `main.py`. Before adding new routes, check what already exists there.
- **Runtime config from DB**: API key, backend selection, and intervals are stored in the `settings` table and loaded at startup/on-change — not hardcoded env vars (though env var bootstrap is supported).
- **Segments JSON**: Transcript timing lives in `transcripts.segments_json` as a JSON array of `{time, speaker, text}` objects, where `time` is an `HH:MM:SS` string. The player frontend fetches this and builds a seek map client-side.
- **FTS5**: Full-text search uses SQLite's FTS5 extension. The `transcripts_fts` virtual table is kept in sync via triggers. Search queries go through ranked FTS5 SQL, not Python-level filtering.
- **No ORM**: Raw SQL via aiosqlite throughout `database.py`.
