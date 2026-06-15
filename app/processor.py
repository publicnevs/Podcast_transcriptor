import json
import logging
from pathlib import Path

import aiosqlite

from .database import DB_PATH
from .downloader import download_audio
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


async def _get_feed_type(episode_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT p.feed_type, p.full_text_extraction, e.episode_url
               FROM episodes e
               LEFT JOIN podcasts p ON p.id = e.podcast_id
               WHERE e.id=?""",
            (episode_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return "podcast"
    return row[0] or "podcast", bool(row[1]), row[2] or ""


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


async def _fetch_article_text(url: str) -> str:
    """Fetch a web page and extract its main article text. Best-effort: any
    failure returns '' so ingestion never breaks."""
    html = await _fetch_html(url)
    if not html:
        return ""
    return _extract_main_text(html, url)


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


async def process_episode(episode_id: int, audio_url: str, title: str, podcast_title: str):
    audio_path: Path | None = None
    try:
        feed_info = await _get_feed_type(episode_id)
        feed_type, full_text_extraction, episode_url = (
            feed_info if isinstance(feed_info, tuple) else (feed_info, False, "")
        )

        # Newsfeed path: no audio download. Strategy: feed content first
        # (often already full text via content:encoded), fetch the web page
        # only as a fallback when the feed text is too short.
        if feed_type in ("newsfeed", "newsletter", "website"):
            await _set_status(episode_id, "transcribing")
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT description FROM episodes WHERE id=?", (episode_id,)
                ) as cur:
                    row = await cur.fetchone()
            content = (row[0] or "") if row else ""
            # Only newsfeed articles carry a real web URL worth scraping; a
            # newsletter's episode_url is a Message-ID, never fetch it. Fetch the
            # full page whenever the feed text looks like a teaser (short, ends
            # with a "Read more"-style marker, or is just a link).
            from .feed_parser import is_truncated
            if (feed_type == "newsfeed" and full_text_extraction
                    and episode_url and is_truncated(content)):
                fetched = await _fetch_article_text(episode_url)
                if len(fetched) > len(content):
                    content = fetched
            if content:
                # Token policy: newsletters get the full AI enrichment (summary,
                # takeaways, chapters); plain text feeds (newsfeed/website) are
                # ingested raw + auto-tagged only — the summary is produced on
                # demand per article (POST /regenerate-summary). This avoids
                # 100 expensive enrich calls when a feed dumps 100 articles.
                auto_summarise = feed_type == "newsletter"
                if auto_summarise:
                    data = await enrich_text(content)
                else:
                    data = {"language": "", "summary": "", "takeaways": [], "chapters": []}
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
                    # Only store a summary row when we actually summarised. Text
                    # feeds stay summary-less until the user asks → episode page
                    # shows the "Mit KI zusammenfassen" empty state.
                    if auto_summarise:
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
                # Auto-tagging stays on for all sources so news & newsletters are
                # first-class in search/radar (cheap LITE call, best-effort). For
                # summary-less text feeds we tag from the raw article text instead
                # of an (absent) summary.
                try:
                    if auto_summarise:
                        raw_tags = await extract_tags(
                            data.get("summary", ""), data.get("takeaways", []),
                            data.get("chapters", []))
                    else:
                        raw_tags = await extract_tags(content[:6000], [], [])
                    if raw_tags:
                        await upsert_tags(episode_id, raw_tags)
                except Exception as e:
                    logger.warning(f"Episode {episode_id}: tagging skipped: {e}")
                # Embeddings only when we have a real summary path; text feeds get
                # indexed for semantic search on demand (when summarised).
                if auto_summarise:
                    await _index_chunks(episode_id, segments)
            else:
                await _set_status(episode_id, "done")
            await enforce_retention(episode_id)
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
            audio_path = await download_audio(audio_url)
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

        await send_notification(
            f"Transkript fertig: {title[:50]}",
            f"Podcast: {podcast_title}\n{word_count} Wörter transkribiert.",
            click_path=f"/episode/{episode_id}",
        )
        logger.info(f"Episode {episode_id} done ({word_count} words)")

    except Exception as e:
        logger.error(f"Episode {episode_id} failed: {e}", exc_info=True)
        await _set_status(episode_id, "error", str(e)[:500])
    finally:
        if audio_path and audio_path.exists():
            audio_path.unlink(missing_ok=True)


async def process_queued():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.id, e.audio_url, e.episode_url, e.title, p.title as podcast_title
               FROM episodes e
               LEFT JOIN podcasts p ON p.id = e.podcast_id
               WHERE e.status = 'queued'
               ORDER BY e.created_at ASC
               LIMIT 3"""
        ) as cur:
            rows = await cur.fetchall()

    for row in rows:
        url = row["audio_url"] or row["episode_url"] or ""
        await process_episode(row["id"], url, row["title"],
                               row["podcast_title"] or "Manuell")


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
    """Initial status for a freshly imported episode.

    Text feeds (newsfeed/website) are always queued so the lightweight ingest
    runs — full text fetched (no AI) + auto-tagging — but process_episode's
    `auto_summarise` gate keeps them from being summarised automatically. This
    keeps a 100-article feed at ~100 cheap tag calls and 0 expensive summary
    calls; summaries are produced on demand per article. Newsletters keep full
    auto-enrich (low volume, opted into via IMAP)."""
    if feed_type == "newsletter":
        return "queued"  # emails always get auto-summarised
    if feed_type in ("newsfeed", "website"):
        return "queued"  # raw ingest + auto-tag, summary on demand
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
