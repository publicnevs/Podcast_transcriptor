# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PodScribe is a self-hosted podcast transcription and reader app. It downloads podcast audio, transcribes it via Google Gemini 2.5 Flash (cloud) or faster-whisper (local), and presents a synced audio/transcript reader as a PWA.

## Development Commands

**Run locally:** (requires Python 3.11 and a system `ffmpeg` binary on PATH — used by the downloader to re-encode audio)
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

## Synology Deployment

The Synology NAS does **not** use `git pull`. Deployment is done by downloading a
tarball of the branch with `wget`, extracting it with `tar`, and all commands run
with `sudo` (the deploy user is not in the docker group / lacks write perms on the
app dir). Substitute `<current-branch>` below with the active development branch
(see your task instructions) — the examples here are templates, not a fixed branch.

> See also `DEPLOY.md` and `SYNOLOGY_COMMANDS.md` (repo root) for the same procedure;
> this section is canonical — keep those in sync if the steps change.

```bash
# 1. Go to the app directory
cd /volume1/docker/Podcast_transcriptor

# 2. Download the branch tarball from GitHub
sudo wget -O podscribe.tar.gz \
  https://github.com/publicnevs/podcast_transcriptor/archive/refs/heads/<current-branch>.tar.gz

# 3. Extract, stripping the top-level folder so files land in the current dir
sudo tar -xzf podscribe.tar.gz --strip-components=1

# 4. Clean up the tarball
sudo rm podscribe.tar.gz

# 5. Rebuild and restart the container
sudo docker-compose down
sudo docker-compose build
sudo docker-compose up -d

# 6. Verify (wait ~5s for startup)
curl http://localhost:7878/health
```

Notes:
- DB schema changes apply automatically via `init_db()` on startup — no manual migration.
- The service-worker cache bumps with each shell change (currently `v14`); browsers auto-refresh, but a hard-refresh (`Ctrl+Shift+R`) speeds it up.
- Keep `.env` (with `GEMINI_API_KEY`) in the app dir — `--strip-components=1` only overwrites files present in the tarball, so `.env` is preserved.

**ALWAYS provide these Synology deploy commands at the end of every work block** (each
completed mode/phase/task), filled in with the current branch name, so the user can deploy
immediately without asking.

## Feature Documentation

`FEATURES.md` (repo root) is the canonical **end-user** feature list — what the app can do,
in plain language, grouped by area. Keep it in sync: whenever a user-facing feature is added,
changed, or removed, update `FEATURES.md` (and the in-app `app/static/about.html` page) in the
same work block.

## Architecture

### Backend (`app/`)

- **`main.py`** — FastAPI app (~2700 lines). All API routes (`/api/*`), lifespan hooks (DB init + scheduler start), the auth ASGI middleware, and static SPA mount. This is the single backend file for routes.
- **`processor.py`** — Orchestrates the full episode pipeline: download audio → transcribe → enrich → auto-tag → update DB. Status transitions: `pending → downloading → transcribing → done` (or `error`). For non-audio (`newsfeed`/`website`/`newsletter`) episodes it skips audio and enriches the article text; when an article feed only shipped a teaser (`feed_parser.is_truncated`), it fetches the full page. Shared best-effort web helpers live here: `_fetch_html`, `_extract_main_text` (trafilatura with an lxml fallback), `_fetch_article_text`, and `_fetch_site_image` (og:image/twitter:image/apple-touch-icon/favicon, optional settings-gated DuckDuckGo favicon service `favicon_service_enabled`).
- **`transcriber.py`** — Gemini API calls. Models are env-overridable (`GEMINI_FLASH_MODEL`, `GEMINI_PRO_MODEL`, `GEMINI_LITE_MODEL`, plus `GEMINI_DIGEST_MODEL` which defaults to the Pro model), defaulting to `gemini-2.5-flash` / `gemini-2.5-pro` / `gemini-2.5-flash-lite`. Three paths:
  - Audio upload → `transcribe_audio()` → returns structured JSON (segments with timestamps, speakers, summary, takeaways, chapters)
  - Text-only → `enrich_text()` → used after Whisper transcription or when feed provides a transcript
  - Digest generation → `generate_digest()` → Gemini Pro writes 1200–2000 word articles
- **`whisper_backend.py`** — Optional faster-whisper local inference. Returns transcript only; `transcriber.enrich_text()` handles enrichment.
- **`downloader.py`** — yt-dlp wrapper. Downloads audio and re-encodes via ffmpeg to 32kbps mono 16kHz MP3 (keeps a 60-min episode ≈14 MB, under Gemini's 20 MB inline limit).
- **`transcript_fetch.py`** — Fetches and parses pre-existing feed transcripts (`<podcast:transcript>`): VTT / SRT / JSON / plain-text → normalized `{time, speaker, text}` segments. Used by the fast path to skip audio download + audio transcription.
- **`feed_parser.py`** — RSS/OPML parsing, Spotify redirect resolution, Podcast Index `<podcast:transcript>` tag detection (decides whether the fast path applies). Article entries prefer `<content:encoded>` over `<summary>` (`_entry_content`); `is_truncated()` flags teaser-only feed text so `processor` knows to fetch the full page.
- **`tagging.py`** — Canonical topic tagging with alias-based de-dup: slugify → alias lookup → exact slug → fuzzy merge (`difflib`, threshold 0.82) → else create new tag. Writes `tags` / `tag_aliases` / `episode_tags`.
- **`database.py`** — All SQLite queries via aiosqlite. Schema: 17 tables + FTS5 virtual table (`settings`, `podcasts`, `episodes`, `transcripts`, `summaries`, `notes`, `digests`, `tags`, `tag_aliases`, `episode_tags`, `issue_recipes`, `scheduled_issues`, `episode_chunks`, `categories`, `users`, `feed_check_log`, `processing_log`). `feed_check_log` records each feed poll (when / how many new / ok) for the statistics page; `processing_log` records per-episode load/transcribe success/failure for the protocol view. The `users` table holds named read-only "friend" logins (username + PBKDF2 hash). `podcasts.category_id` (→`categories`) + `podcasts.position` drive the category-grouped, drag-orderable library homepage. Key design: denormalized counts (`unread_count`, `done_count`) on `podcasts` for UI performance; `segments_json` in `transcripts` stores timestamped segments for player sync. New columns are added via additive `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` migrations near the bottom of the file.
- **`scheduler.py`** — APScheduler jobs: hourly feed checks (`check_all_feeds` scans **all** podcasts, gated per-feed by `_feed_due`), scheduled digest generation (cron-style via `scheduled_issues` table), the weekly trending auto-digest (`run_auto_digest`), and the daily **Tageszeitung** (`run_daily_paper` → `main.build_daily_paper`, gated by `tageszeitung_enabled` + `tageszeitung_hour` + a `tageszeitung_last_run` once-per-day guard). New episodes are inserted with their natural status (`pending` when auto-transcribe is off); `process_queued()` only processes `queued` rows, so nothing is auto-transcribed that the user didn't opt into. A single feed can also be re-scanned on demand via `POST /api/podcasts/{id}/check` (ignores the auto-transcribe gate — this is what the "check for new episodes" button calls), and **all** feeds + the newsletter inbox via `POST /api/feeds/check-all` (the Neuzugänge "Aktualisieren" button; `check_all_feeds(force=True)`). `feed_type='website'` sources are re-scraped (not RSS-parsed) by `_check_website`, deduping on a sha1 of the page text stored in `episode_url`; one-shot scrapes (`POST /api/scrape`) live under a shared "Web-Clips" collection and are never re-scanned.
- **`exporter.py`** — TXT, Markdown, PDF, "AI Copy" (XML) export formatters.
- **`notifier.py`** — ntfy.sh push notifications (topic/URL from `settings`); fails silently when unconfigured. Optional `click_path` adds a tappable deep link (ntfy `Click` header) when `public_base_url` is set.
- **`newsletter.py`** — IMAP newsletter inbox (stdlib `imaplib`+`email`, blocking work in an executor). `check_inbox()` fetches new mails, groups them **per sender** into pseudo-podcasts (`feed_type='newsletter'`, `rss_url='newsletter:<addr>'`), and feeds each mail through `processor.insert_new_episodes` like a newsfeed article. Dedup via Message-ID in `episodes.episode_url`; IMAP `SINCE` only bounds the scan. On first sight of a sender it best-effort sets `artwork_url` (sender-domain og:image/favicon via `processor._fetch_site_image`, else a logo harvested from the mail HTML). Polled daily by a dedicated scheduler job and on demand (`POST /api/newsletter/check`).
- **`mailer.py`** — Outbound SMTP (stdlib `smtplib`+`email`) for digest delivery. `maybe_email_digest()` is called at the end of `_build_issue` (no-op unless `digest_email_enabled`); manual send via `POST /api/digests/{id}/email`.
- **`rag.py`** — Semantic search over the whole library. Each processed episode is chunked along `segments_json` and embedded (`transcriber.embed_texts`, Gemini embeddings) into `episode_chunks` (vectors as packed float32 BLOBs, no numpy). `answer()` embeds the question, ranks chunks by cosine similarity in pure Python, and has Gemini Flash compose a cited answer. `reindex_all()` backfills existing episodes. Indexing is best-effort — a missing API key never breaks transcription.
- **`auth.py`** — Owner/guest/friend access control, stdlib-only (PBKDF2 password hashing + HMAC-signed session cookie `ps_session`, which now encodes `role.username.ts.sig`; legacy `role.ts.sig` cookies still parse). **Open-by-default**: with no owner password set, `current_role()` returns `"owner"` for everyone (back-compat single-user mode). Once an owner password is configured, the `access_mode` setting decides anonymous access: `open`/`guest_password` → read-only `"guest"`, `friends_only` → `"anon"` (no read; must log in). Named friends live in the `users` table; `login_user(username,password)` returns `(role, username)` (empty username = owner/shared-guest password). Enforcement is a single ASGI middleware in `main.py` (`@app.middleware("http")`) that gates writes + cost actions to owners, blocks `anon` to login/terms/static only, and lets guests/friends `GET` an allowlist of read paths (`_guest_get_ok`) plus, if `guest_rag_enabled`, `POST /api/ask` + `/api/chat` under a per-IP token bucket (`allow_rag`). Friend CRUD: `GET/POST /api/users`, `DELETE /api/users/{id}` (max 10, owner-only). The auth config is cached and refreshed via `auth.invalidate()` after settings changes.

### Frontend (`app/static/`)

Vanilla JS SPA — no build step, no framework. Pages share `app.js` utilities. Every page loads `icons.js` **before** `app.js`.

- **`icons.js`** — Unified inline-SVG icon set (Lucide outline, ISC license) bundled locally so it works offline (no CDN, no build). `icon(name, {size,cls})` returns an `<svg>` string used inside JS templates; `hydrateIcons()` (runs on `DOMContentLoaded`) fills any `data-icon="name"` element in static HTML. Add a new glyph to the `ICONS` map to extend.
- **`app.js`** — Shared utilities: `API` (fetch wrappers), `toast()`, `confirmModal()`, `renderNav()`, `statusBadge()`, plus reusable UI helpers `openSheet(title, items)` (bottom-sheet on mobile / dialog on desktop), `openMenu(anchorEl, items)` (⋮ context popover), `downloadFile(url, filename)` (blob download that works in the installed PWA), and `enableDragScroll(el)` (mouse drag + vertical-wheel→horizontal for ticker-style strips on desktop). Theme: `initTheme()` **defaults to light** (`saved || 'light'`), `toggleTheme()` persists the choice in `localStorage('ps-theme')`. The mobile bottom-nav exposes Radar/Tags/Fragen/Über + the theme toggle (and owner-only Abonnieren/Einstellungen) via `openMoreSheet()` — reachable for guests too.
- **`episode.html`** — The most complex page. Audio player syncs with transcript: clicking a paragraph seeks audio; during playback, current paragraph highlights and auto-scrolls (uses `segments_json` for timestamp mapping). Action bar uses the export bottom-sheet + ⋮ menu; the summary card always shows Zusammenfassung + Themenübersicht + Key Takeaways and offers a regenerate button (`POST /api/episodes/{id}/regenerate-summary`) plus an "Artikel in Redaktion erstellen" action (and ⋮-menu item) that `POST`s a single-episode Magazin digest to `/api/digests` and opens `/digest/{id}`.
- Other pages: `index.html` (library, grouped by category with drag-&-drop reordering via `POST /api/podcasts/reorder`), `podcast.html` (single feed; includes a category dropdown), `inbox.html` ("Neuzugänge" — pending/queued items across all feeds via `GET /api/inbox`, with on-demand transcribe + `POST /api/feeds/check-all`; replaces "Entdecken" in the nav), `category.html` (one category's bundled podcasts/episodes/tags via `GET /api/categories/{id}`), `digest.html` (digest/"Redaktion" builder + recipes/scheduling — UI label is "Redaktion", routes/tables stay `digest`/`digests`; includes the "Freier Artikel" format with a free prompt + "KI wählt Folgen ↔ manuell" toggle), `digest-reader.html` (renders a generated digest article), `discover.html` (recommended feeds — still routed, linked from Abonnieren/Neuzugänge), `tags.html` (browse by topic tag), `topic.html` (single tag's episodes), `radar.html` ("Themen-Radar" topic overview), `search.html` ("Frag deine Bibliothek" — RAG Q&A / chat UI over `/api/ask` + `/api/chat`, with Markdown export + print via `POST /api/chat/export`), `login.html` (owner/guest/friend login — optional name + password), `settings.html` (API key, backend, intervals, access control + **friends** + **Tageszeitung**, category management), `about.html`, `terms.html` (Nutzungsbedingungen, public at `/terms`).
- **`sw.js`** — Service Worker (cache `podscribe-v14`); caches the app shell incl. `icons.js` for offline reading, and **skips** `/audio` and `/export` so streaming and downloads always hit the network. Bump the cache name when shipping shell changes to force-refresh clients.
- **`style.css`** — CSS variables design system, dark mode, mobile-first (bottom nav) + desktop (top nav) responsive layout. Reusable components: `.sheet`, `.menu-popover`, `.fab`, `.icon`. `.action-bar` and `.meta-line` are single-line, horizontally scrollable (hidden scrollbar) on mobile.

### Data Flow

1. User adds RSS feed → `feed_parser` checks for episodes → new episodes inserted (`pending`, or `queued` when auto-transcribe is on). `feed_parser` normalizes `pub_date` to a sortable `YYYY-MM-DD HH:MM:SS` string (from feedparser's `published_parsed`) so episode lists sort newest-first
2. Scheduler (all feeds, hourly) or the per-podcast check button → new episodes get listed; `processor.process_queued()` then transcribes only the `queued` ones
3. Fast path: feed has transcript URL → `transcript_fetch.fetch_transcript()` parses it → `enrich_text()` → done
4. Standard path: `downloader` fetches audio (yt-dlp) → ffmpeg to 32kbps mono 16kHz MP3 → `transcribe_audio()` → structured JSON stored in `transcripts` + `summaries`
5. Auto-tagging (`tagging.py`): Gemini Flash reads summary+takeaways+chapters → generates tag slugs → stored in `tags` with alias dedup

### Key Design Decisions

- **Single-file backend**: All routes in `main.py`. Before adding new routes, check what already exists there.
- **Runtime config from DB**: API key, backend selection, and intervals are stored in the `settings` table and loaded at startup/on-change — not hardcoded env vars (though env var bootstrap is supported).
- **Segments JSON**: Transcript timing lives in `transcripts.segments_json` as a JSON array of `{time, speaker, text}` objects, where `time` is an `HH:MM:SS` string. The player frontend fetches this and builds a seek map client-side.
- **FTS5**: Full-text search uses SQLite's FTS5 extension. The `transcripts_fts` virtual table is kept in sync via triggers. Search queries go through ranked FTS5 SQL, not Python-level filtering.
- **No ORM**: Raw SQL via aiosqlite throughout `database.py`.
- **Open-by-default auth**: The app stays fully open (single-user) until an owner password is set in Settings. After that, a single ASGI middleware splits requests into owner (full access) vs read-only guest. When adding a new write/cost route, it's owner-gated automatically; when adding a guest-readable `GET` route, add it to the allowlist in `auth.py`/`main.py`.
