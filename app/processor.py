import asyncio
import json
import logging
from pathlib import Path

import aiosqlite

from .database import DB_PATH, get_setting
from .downloader import download_audio, AudioTooLongError
from .notifier import send_notification
from .transcriber import transcribe_audio, enrich_text, extract_tags
from .transcript_fetch import fetch_transcript
from .tagging import upsert_tags

logger = logging.getLogger(__name__)


async def _get_transcript_source(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT transcript_url, transcript_type FROM episodes WHERE id=?",
            (episode_id,),
        ) as cur:
            row = await cur.fetchone()
    return (row[0] or "", row[1] or "") if row else ("", "")


async def _get_routing(episode_id: int):
    """Return (feed_type, full_text_extraction, episode_url, audio_url) for routing.
    Routing is decided per-EPISODE (does it actually have audio?), not just by the
    feed's type — a mixed feed flagged 'podcast' can still carry article entries."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT p.feed_type, p.full_text_extraction, e.episode_url, e.audio_url
               FROM episodes e
               LEFT JOIN podcasts p ON p.id = e.podcast_id
               WHERE e.id=?""",
            (episode_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return "podcast", False, "", ""
    return row[0] or "podcast", bool(row[1]), row[2] or "", row[3] or ""


# Markers that indicate a login wall / paywall / consent gate rather than real
# article content — used so we surface a clear error instead of storing junk.
_PAYWALL_MARKERS = (
    "subscribe to continue", "subscribe to read", "create a free account",
    "create an account to", "register to read", "sign in to read",
    "please sign in", "please log in", "log in to continue", "members only",
    "this content is for subscribers", "to continue reading", "enable javascript",
    "please enable javascript", "verify you are a human", "are you a robot",
)


def _looks_paywalled(text: str) -> bool:
    """Heuristic: does this extracted text look like a login/paywall/JS gate rather
    than a real article? True for empty or very short text dominated by such markers."""
    if not text:
        return True
    t = text.strip()
    low = t.lower()
    if any(m in low for m in _PAYWALL_MARKERS) and len(t) < 1200:
        return True
    return False


async def _log_processing(episode_id: int, action: str, ok: bool, detail: str = ""):
    """Append a row to processing_log (what was loaded/transcribed, success or not).
    Best-effort: never let logging break the pipeline. Trims old rows opportunistically."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT podcast_id FROM episodes WHERE id=?", (episode_id,)
            ) as cur:
                row = await cur.fetchone()
            podcast_id = row[0] if row else None
            await db.execute(
                """INSERT INTO processing_log (episode_id, podcast_id, action, ok, detail)
                   VALUES (?, ?, ?, ?, ?)""",
                (episode_id, podcast_id, action, 1 if ok else 0, (detail or "")[:500]),
            )
            # Keep the table bounded (retain newest ~2000 rows).
            await db.execute(
                """DELETE FROM processing_log WHERE id < (
                       SELECT MAX(id) - 2000 FROM processing_log)""")
            await db.commit()
    except Exception as e:
        logger.warning(f"processing_log insert failed: {e}")


async def _fetch_html(url: str) -> str:
    """GET a web page and return its HTML text (empty on any failure)."""
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        logger.warning(f"Fetch failed for {url}: {e}")
        return ""


def _extract_main_text(html: str, url: str = "") -> str:
    """Extract the main readable article text from HTML.

    Prefers trafilatura (strips nav/ads/boilerplate, keeps the real body);
    falls back to a simple lxml grab of <article>/<main>/<body> when trafilatura
    is unavailable or returns nothing. Best-effort: returns '' on any failure."""
    import re
    try:
        import trafilatura
        extracted = trafilatura.extract(
            html, url=url or None, include_comments=False,
            include_tables=False, favor_precision=True)
        if extracted and extracted.strip():
            return re.sub(r'[ \t]+', ' ', extracted).strip()[:16000]
    except Exception as e:
        logger.debug(f"trafilatura extraction failed for {url}: {e}")
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html)
        for tag in tree.iter('script', 'style', 'nav', 'footer', 'header', 'aside'):
            tag.clear()
        for xpath in ('//article', '//main', '//body'):
            nodes = tree.xpath(xpath)
            if nodes:
                text = re.sub(r'\s+', ' ', nodes[0].text_content()).strip()
                if text:
                    return text[:16000]
    except Exception as e:
        logger.warning(f"lxml extraction failed for {url}: {e}")
    return ""


def _render_sync(url: str) -> str:
    """Render a page in headless Chromium (Playwright). Returns '' when Playwright
    isn't installed — the feature is opt-in via the INSTALL_BROWSER build arg."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return ""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page(user_agent="Mozilla/5.0")
            page.goto(url, wait_until="networkidle", timeout=20000)
            return page.content()
        finally:
            browser.close()


async def _render_html(url: str) -> str:
    """Async wrapper around the headless-browser render (best-effort, '' on error)."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _render_sync, url)
    except Exception as e:
        logger.warning(f"JS render failed for {url}: {e}")
        return ""


async def _fetch_article_text(url: str) -> str:
    """Fetch a web page and extract its main article text. Best-effort: any
    failure returns '' so ingestion never breaks. When the plain fetch yields
    nothing usable (JS-only page / paywall) and JS rendering is enabled, retry
    with a headless browser."""
    html = await _fetch_html(url)
    text = _extract_main_text(html, url) if html else ""
    if (not text or _looks_paywalled(text)) and (await get_setting("js_render_enabled")) == "1":
        rendered = await _render_html(url)
        if rendered:
            rtext = _extract_main_text(rendered, url)
            if rtext and not _looks_paywalled(rtext) and len(rtext) > len(text):
                return rtext
    return text


async def _url_ok(url: str) -> bool:
    """True if the URL responds 200 (HEAD, falling back to GET)."""
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            r = await client.head(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code >= 400:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code == 200
    except Exception:
        return False


async def _favicon_service(url: str) -> str:
    """Optional third-party favicon (DuckDuckGo). Off by default — only used
    when 'favicon_service_enabled' is set, since it leaks the domain name."""
    try:
        from .database import get_setting
        if (await get_setting("favicon_service_enabled")) != "1":
            return ""
        from urllib.parse import urlparse
        parsed = urlparse(url if "//" in url else "https://" + url)
        domain = parsed.netloc or parsed.path.split("/")[0]
        return f"https://icons.duckduckgo.com/ip3/{domain}.ico" if domain else ""
    except Exception:
        return ""


async def _fetch_site_image(url: str, html: str | None = None) -> str:
    """Best-effort logo/preview image for a site (for newsletters & websites).

    Order: og:image → twitter:image → apple-touch-icon → icon → /favicon.ico,
    then an optional (settings-gated) favicon service. Returns '' if nothing
    usable is found. Pass `html` to avoid a second fetch when the page is in hand."""
    from urllib.parse import urljoin, urlparse
    try:
        if html is None:
            html = await _fetch_html(url)
        if html:
            from lxml import html as lxml_html
            tree = lxml_html.fromstring(html)
            for xp in (
                "//meta[@property='og:image']/@content",
                "//meta[@property='og:image:url']/@content",
                "//meta[@name='twitter:image']/@content",
                "//meta[@name='twitter:image:src']/@content",
                "//link[contains(@rel,'apple-touch-icon')]/@href",
                "//link[@rel='icon']/@href",
                "//link[contains(@rel,'shortcut')]/@href",
            ):
                hits = [h.strip() for h in tree.xpath(xp) if h and h.strip()]
                if hits:
                    return urljoin(url, hits[0])
        # No embedded image → try the conventional /favicon.ico.
        parsed = urlparse(url if "//" in url else "https://" + url)
        if parsed.scheme and parsed.netloc:
            fallback = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
            if await _url_ok(fallback):
                return fallback
        return await _favicon_service(url)
    except Exception as e:
        logger.warning(f"Site image lookup failed for {url}: {e}")
        return ""


async def _index_chunks(episode_id: int, segments: list):
    """Build + store embeddings for semantic search. Best-effort: a missing API
    key or quota issue must never fail the transcription itself."""
    try:
        from .rag import index_episode
        await index_episode(episode_id, segments)
    except Exception as e:
        logger.warning(f"Episode {episode_id}: embedding index skipped: {e}")


async def _process_as_article(episode_id: int, feed_type: str,
                              full_text_extraction: bool, episode_url: str,
                              force_fetch: bool = False):
    """Process an episode as text/article: prefer feed/email text, fetch the web
    page when it's a teaser (or when forced for a mis-typed podcast entry), then
    enrich → store transcript+summary → tag+index. Surfaces a clear error when a
    forced web fetch yields nothing but a paywall/JS gate."""
    await _set_status(episode_id, "transcribing")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT description FROM episodes WHERE id=?", (episode_id,)
        ) as cur:
            row = await cur.fetchone()
    content = (row[0] or "") if row else ""

    # Only real web URLs are worth scraping (a newsletter's episode_url is a
    # Message-ID; a website snapshot's is a hash). Fetch when the feed text looks
    # like a teaser, or when forced (a podcast-typed entry that has no audio).
    from .feed_parser import is_truncated
    web_url = episode_url if (episode_url or "").startswith(("http://", "https://")) else ""
    should_fetch = bool(web_url) and is_truncated(content) and (
        force_fetch or (feed_type == "newsfeed" and full_text_extraction))
    if should_fetch:
        fetched = await _fetch_article_text(web_url)
        if fetched and not _looks_paywalled(fetched) and len(fetched) > len(content):
            content = fetched

    # A forced fetch with no usable result means there's genuinely nothing to
    # read (mis-tagged audio entry, paywall, or JS-only page) — fail clearly.
    if force_fetch and (not content or _looks_paywalled(content)):
        await _log_processing(episode_id, "article", False,
                              "Kein Artikeltext (evtl. Paywall/JavaScript-Seite).")
        raise ValueError(
            "Artikeltext konnte nicht geladen werden (evtl. Paywall oder JavaScript-Seite).")

    if content:
        data = await enrich_text(content)
        segments = [{"time": "00:00:00", "speaker": "", "text": content}]
        data["segments"] = segments
        word_count = len(content.split())
        model_used = "email" if feed_type == "newsletter" else "article"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO transcripts (episode_id, content, segments_json, language, word_count, model_used)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(episode_id) DO UPDATE SET
                       content=excluded.content, segments_json=excluded.segments_json,
                       language=excluded.language, word_count=excluded.word_count,
                       model_used=excluded.model_used""",
                (episode_id, content, json.dumps(segments),
                 data.get("language", ""), word_count, model_used),
            )
            await db.execute(
                "INSERT INTO transcripts_fts (episode_id, content) VALUES (?, ?)",
                (episode_id, content),
            )
            await db.execute(
                """INSERT INTO summaries (episode_id, summary, takeaways_json, chapters_json, summary_lang)
                   VALUES (?, ?, ?, ?, 'de')
                   ON CONFLICT(episode_id) DO UPDATE SET
                       summary=excluded.summary, takeaways_json=excluded.takeaways_json,
                       chapters_json=excluded.chapters_json,
                       summary_lang='de'""",
                (episode_id, data.get("summary", ""),
                 json.dumps(data.get("takeaways", [])),
                 json.dumps(data.get("chapters", []))),
            )
            await db.execute("UPDATE episodes SET status='done' WHERE id=?", (episode_id,))
            await db.commit()
        # Tag + index for search/radar so news & newsletters are first-class
        # alongside podcasts (best-effort, never fatal).
        try:
            raw_tags = await extract_tags(
                data.get("summary", ""), data.get("takeaways", []),
                data.get("chapters", []))
            if raw_tags:
                await upsert_tags(episode_id, raw_tags)
        except Exception as e:
            logger.warning(f"Episode {episode_id}: tagging skipped: {e}")
        await _index_chunks(episode_id, segments)
        await _log_processing(episode_id, "article", True, f"{word_count} Wörter ({model_used})")
    else:
        await _set_status(episode_id, "done")
        await _log_processing(episode_id, "article", True, "Kein Textinhalt")
    await enforce_retention(episode_id)


async def process_episode(episode_id: int, audio_url: str, title: str, podcast_title: str):
    audio_path: Path | None = None
    try:
        feed_type, full_text_extraction, episode_url, real_audio_url = (
            await _get_routing(episode_id))
        has_audio = real_audio_url.startswith(("http://", "https://"))
        text_like = feed_type in ("newsfeed", "newsletter", "website")

        # Route per-EPISODE: only items with a real audio URL go to the audio
        # pipeline. A 'podcast'-typed feed can still carry article entries (mixed
        # feeds where one item has an enclosure) — those have no audio_url and must
        # be handled as text, never handed to yt-dlp ("Unsupported URL").
        if text_like or not has_audio:
            if not text_like and not (episode_url or "").startswith(("http://", "https://")):
                raise ValueError("Keine Audio- oder Artikel-URL für diese Episode.")
            await _process_as_article(
                episode_id, feed_type, full_text_extraction, episode_url,
                force_fetch=(not text_like))
            return

        transcript_url, transcript_type = await _get_transcript_source(episode_id)
        data = None
        model_used = "gemini"

        # Fast path: feed already provides a transcript → no download, no Gemini audio
        if transcript_url:
            try:
                await _set_status(episode_id, "transcribing")
                logger.info(f"Episode {episode_id}: using feed transcript {transcript_url}")
                data = await fetch_transcript(transcript_url, transcript_type)
                model_used = "feed-transcript"
                # Enrich text-only (cheap) for summary / takeaways / chapters
                try:
                    full = "\n".join(
                        f"[{s.get('time','')}] {s.get('speaker','')}: {s.get('text','')}"
                        for s in data.get("segments", [])
                    )
                    enrichment = await enrich_text(full)
                    for k in ("summary", "takeaways", "chapters", "language"):
                        if enrichment.get(k):
                            data[k] = enrichment[k]
                except Exception as e:
                    logger.warning(f"Episode {episode_id}: enrichment skipped: {e}")
            except Exception as e:
                logger.warning(f"Episode {episode_id}: feed transcript failed ({e}), "
                               f"falling back to audio transcription")
                data = None

        # Standard path: download audio + transcribe (Gemini or Whisper)
        if data is None:
            await _set_status(episode_id, "downloading")
            try:
                max_min = int((await get_setting("max_audio_minutes")) or "0")
                audio_path = await download_audio(real_audio_url or audio_url, max_minutes=max_min)
            except AudioTooLongError:
                raise  # deliberate skip — surface the clear message, don't scrape
            except Exception as dl_err:
                # yt-dlp rejected the URL (e.g. an article link that slipped into
                # the audio path). If we have a real web URL, fall back to reading
                # it as an article instead of failing outright.
                if (episode_url or "").startswith(("http://", "https://")):
                    logger.warning(
                        f"Episode {episode_id}: audio download failed ({dl_err}); "
                        f"falling back to article extraction")
                    await _log_processing(episode_id, "download", False, str(dl_err)[:200])
                    await _process_as_article(
                        episode_id, feed_type, full_text_extraction, episode_url,
                        force_fetch=True)
                    return
                raise
            await _set_status(episode_id, "transcribing")
            data = await transcribe_audio(audio_path)

        segments = data.get("segments", [])
        full_text = "\n".join(
            f"[{s['time']}] {s.get('speaker', 'Speaker')}: {s['text']}"
            for s in segments
        )
        word_count = len(full_text.split())

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO transcripts (episode_id, content, segments_json, language, word_count, model_used)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(episode_id) DO UPDATE SET
                       content=excluded.content,
                       segments_json=excluded.segments_json,
                       language=excluded.language,
                       word_count=excluded.word_count,
                       model_used=excluded.model_used""",
                (episode_id, full_text, json.dumps(segments),
                 data.get("language", ""), word_count, model_used),
            )
            await db.execute(
                """INSERT INTO transcripts_fts (episode_id, content) VALUES (?, ?)""",
                (episode_id, full_text),
            )
            await db.execute(
                """INSERT INTO summaries (episode_id, summary, takeaways_json, chapters_json, summary_lang)
                   VALUES (?, ?, ?, ?, 'de')
                   ON CONFLICT(episode_id) DO UPDATE SET
                       summary=excluded.summary,
                       takeaways_json=excluded.takeaways_json,
                       chapters_json=excluded.chapters_json,
                       summary_lang='de'""",
                (episode_id, data.get("summary", ""),
                 json.dumps(data.get("takeaways", [])),
                 json.dumps(data.get("chapters", []))),
            )
            await db.execute("UPDATE episodes SET status='done' WHERE id=?", (episode_id,))
            await db.commit()

        # Auto-tagging (best-effort, cheap FLASH call over compact metadata)
        try:
            raw_tags = await extract_tags(
                data.get("summary", ""), data.get("takeaways", []), data.get("chapters", []))
            if raw_tags:
                await upsert_tags(episode_id, raw_tags)
        except Exception as e:
            logger.warning(f"Episode {episode_id}: tagging skipped: {e}")

        await _index_chunks(episode_id, segments)

        await enforce_retention(episode_id)
        await _log_processing(episode_id, "transcribe", True,
                              f"{word_count} Wörter ({model_used})")

        await send_notification(
            f"Transkript fertig: {title[:50]}",
            f"Podcast: {podcast_title}\n{word_count} Wörter transkribiert.",
            click_path=f"/episode/{episode_id}",
        )
        logger.info(f"Episode {episode_id} done ({word_count} words)")

    except Exception as e:
        logger.error(f"Episode {episode_id} failed: {e}", exc_info=True)
        await _set_status(episode_id, "error", str(e)[:500])
        await _log_processing(episode_id, "error", False, str(e)[:300])
    finally:
        if audio_path and audio_path.exists():
            audio_path.unlink(missing_ok=True)


# Serialises queue draining across all callers (scheduler drain job, feed
# checks, HTTP background tasks). A non-blocking acquire means a second caller
# returns immediately instead of selecting the same rows — no double-processing.
_drain_lock = asyncio.Lock()


async def _next_queued():
    """Fetch the oldest still-queued episode, or None when the queue is empty."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.id, e.audio_url, e.episode_url, e.title, p.title as podcast_title
               FROM episodes e
               LEFT JOIN podcasts p ON p.id = e.podcast_id
               WHERE e.status = 'queued'
               ORDER BY e.created_at ASC
               LIMIT 1"""
        ) as cur:
            return await cur.fetchone()


async def process_queued(max_items: int = 500):
    """Drain the queue: process every 'queued' episode one by one until none
    remain (or the safety cap is hit — the next scheduler tick continues).

    Guarded by a module-level lock so concurrent callers don't race on the same
    rows; if a drain is already running this call is a no-op.
    """
    if _drain_lock.locked():
        return
    async with _drain_lock:
        processed = 0
        while processed < max_items:
            row = await _next_queued()
            if row is None:
                break
            url = row["audio_url"] or row["episode_url"] or ""
            await process_episode(row["id"], url, row["title"],
                                  row["podcast_title"] or "Manuell")
            processed += 1


async def requeue_stuck(stale_minutes: int = 30) -> int:
    """Recover episodes left mid-flight (e.g. by a crash/restart): rows stuck in
    'downloading'/'transcribing' whose processing_started_at is older than the
    threshold are reset to 'queued' so the drainer picks them up again."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE episodes SET status='queued', processing_started_at=NULL
               WHERE status IN ('downloading','transcribing')
                 AND (processing_started_at IS NULL
                      OR processing_started_at <= datetime('now', ?))""",
            (f"-{int(stale_minutes)} minutes",),
        )
        await db.commit()
        n = cur.rowcount or 0
    if n:
        logger.info(f"requeue_stuck: reset {n} stuck episode(s) to 'queued'")
    return n


async def _set_status(episode_id: int, status: str, error_msg: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if error_msg:
            await db.execute(
                "UPDATE episodes SET status=?, error_msg=?, processing_started_at=NULL WHERE id=?",
                (status, error_msg, episode_id),
            )
        elif status in ("downloading", "transcribing"):
            # Record when active processing began (preserve first timestamp if already set)
            await db.execute(
                "UPDATE episodes SET status=?, processing_started_at=COALESCE(processing_started_at, CURRENT_TIMESTAMP) WHERE id=?",
                (status, episode_id),
            )
        else:
            await db.execute(
                "UPDATE episodes SET status=?, processing_started_at=NULL WHERE id=?",
                (status, episode_id),
            )
        await db.commit()


def _episode_status(feed_type: str, full_text_extraction: bool,
                    auto_transcribe: bool, has_episode_url: bool) -> str:
    """Initial status for a freshly imported episode."""
    if feed_type == "newsletter":
        return "queued"  # emails always get auto-summarised
    if feed_type == "newsfeed":
        return "queued" if (full_text_extraction and has_episode_url) else "done"
    return "queued" if auto_transcribe else "pending"


async def _episode_exists(db, podcast_id: int, audio_url: str, episode_url: str) -> bool:
    """Dedup check: prefer the article/episode link, fall back to audio URL."""
    if episode_url:
        async with db.execute(
            "SELECT 1 FROM episodes WHERE podcast_id=? AND episode_url=?",
            (podcast_id, episode_url),
        ) as cur:
            if await cur.fetchone():
                return True
    if audio_url:
        async with db.execute(
            "SELECT 1 FROM episodes WHERE podcast_id=? AND audio_url=?",
            (podcast_id, audio_url),
        ) as cur:
            if await cur.fetchone():
                return True
    return False


async def insert_new_episodes(db, podcast_id: int, episodes: list, *,
                              feed_type: str, full_text_extraction: bool,
                              auto_transcribe: bool, limit: int = 0) -> int:
    """Insert not-yet-seen episodes for a feed. Shared by initial subscribe
    (main.add_podcast) and the hourly scheduler so dedup/status stay in sync.
    Returns the number of episodes inserted. Caller commits."""
    eps = episodes[:limit] if limit and limit > 0 else episodes
    count = 0
    for ep in eps:
        episode_url = ep.get("episode_url", "") or ""
        audio_url = ep.get("audio_url") or episode_url or ""
        if await _episode_exists(db, podcast_id, audio_url, episode_url):
            continue
        status = _episode_status(feed_type, full_text_extraction,
                                 auto_transcribe, bool(episode_url))
        await db.execute(
            """INSERT OR IGNORE INTO episodes
                   (podcast_id, title, audio_url, episode_url, pub_date,
                    duration_sec, description, status,
                    transcript_url, transcript_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (podcast_id, ep["title"], audio_url, episode_url,
             ep.get("pub_date", ""), ep.get("duration_sec", 0),
             ep.get("description", ""), status,
             ep.get("transcript_url", ""), ep.get("transcript_type", "")),
        )
        count += 1
    return count


async def enforce_retention(episode_id: int):
    """Keep at most podcasts.max_transcripts transcripts per feed (0 = keep all).
    Deletes the oldest done episodes beyond the limit. Audio isn't stored, so
    only transcript/summary rows (and the FTS row) are removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        async with db.execute(
            """SELECT p.id, p.max_transcripts
               FROM podcasts p JOIN episodes e ON e.podcast_id = p.id
               WHERE e.id=?""",
            (episode_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row[1] or row[1] <= 0:
            return
        podcast_id, limit = row[0], row[1]
        async with db.execute(
            """SELECT e.id FROM episodes e
               JOIN transcripts t ON t.episode_id = e.id
               WHERE e.podcast_id=? AND e.status='done'
               ORDER BY e.pub_date DESC, e.id DESC""",
            (podcast_id,),
        ) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        stale = ids[limit:]
        for eid in stale:
            await db.execute("DELETE FROM transcripts_fts WHERE episode_id=?", (eid,))
            await db.execute("DELETE FROM episodes WHERE id=?", (eid,))
        if stale:
            await db.commit()
            logger.info(f"Retention: removed {len(stale)} old transcript(s) "
                        f"from podcast {podcast_id} (limit {limit})")
