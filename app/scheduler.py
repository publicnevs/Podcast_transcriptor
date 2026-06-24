import logging

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .database import DB_PATH
from .notifier import send_notification

logger = logging.getLogger(__name__)
_scheduler = AsyncIOScheduler()


async def _log_check(db, podcast_id: int, new_episodes: int, ok: bool = True,
                     error_msg: str = None):
    """Record a single feed poll in feed_check_log (powers the statistics page)."""
    try:
        await db.execute(
            """INSERT INTO feed_check_log (podcast_id, new_episodes, ok, error_msg)
               VALUES (?, ?, ?, ?)""",
            (podcast_id, int(new_episodes), 1 if ok else 0,
             (error_msg or "")[:300] or None),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"feed_check_log insert failed: {e}")


def _feed_due(podcast) -> bool:
    """True if a feed is due for a check based on its check_interval_hours.
    The job runs hourly; feeds with a longer interval are skipped until due.

    Repeatedly failing feeds get exponential backoff so we don't hammer a broken
    server every hour: effective interval = base * 2^errors, capped at 24h."""
    interval = podcast["check_interval_hours"] or 0
    last = podcast["last_checked"]
    # Backoff widens the interval after consecutive errors (reset to 0 on success).
    try:
        errors = int(podcast["consecutive_fetch_errors"] or 0)
    except (KeyError, IndexError, TypeError):
        errors = 0
    if errors > 0:
        base = interval if interval and interval > 1 else 1
        interval = min(base * (2 ** min(errors, 5)), 24)
    if not interval or interval <= 1 or not last:
        return True
    from datetime import datetime, timedelta
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            last_dt = datetime.strptime(str(last), fmt)
            return datetime.utcnow() - last_dt >= timedelta(hours=interval)
        except ValueError:
            continue
    return True  # unparseable timestamp → check anyway


async def _check_website(db, podcast) -> int:
    """Re-scrape a monitored web page; add a new episode only when the page
    content changed (deduped via a sha1 hash stored in episode_url)."""
    import hashlib
    from datetime import datetime
    from .processor import _fetch_article_text, _looks_paywalled

    text = await _fetch_article_text(podcast["rss_url"])
    if not text or _looks_paywalled(text):
        return 0

    # Best-effort: give the source a real logo the first time we scrape it
    # (covers websites subscribed before logo capture existed).
    try:
        async with db.execute(
            "SELECT artwork_url FROM podcasts WHERE id=?", (podcast["id"],)) as cur:
            art = await cur.fetchone()
        if not (art and art[0]):
            from .processor import _fetch_site_image
            img = await _fetch_site_image(podcast["rss_url"])
            if img:
                await db.execute("UPDATE podcasts SET artwork_url=? WHERE id=?",
                                 (img, podcast["id"]))
    except Exception:
        pass

    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
    marker = f"website-hash:{digest}"
    async with db.execute(
        "SELECT 1 FROM episodes WHERE podcast_id=? AND episode_url=?",
        (podcast["id"], marker),
    ) as cur:
        if await cur.fetchone():
            return 0  # unchanged since last scrape
    await db.execute(
        """INSERT INTO episodes (podcast_id, title, audio_url, episode_url,
               pub_date, description, status)
           VALUES (?, ?, '', ?, ?, ?, 'queued')""",
        (podcast["id"], f"{podcast['title']} — {datetime.now().strftime('%d.%m.%Y %H:%M')}",
         marker, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text),
    )
    return 1


async def check_all_feeds(force: bool = False) -> int:
    from .feed_parser import parse_rss_feed
    from .processor import process_queued, insert_new_episodes

    # Check ALL feeds so new episodes get listed even when auto-transcription is
    # off. insert_new_episodes assigns 'pending' for non-auto podcasts, and
    # process_queued() below only processes 'queued' rows — so nothing is
    # auto-transcribed that the user didn't opt into. force=True bypasses the
    # per-feed interval gate (used by the on-demand 'check all' button).
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM podcasts") as cur:
            podcasts = await cur.fetchall()

    new_count = 0
    for podcast in podcasts:
        # Newsletter pseudo-feeds have no RSS URL — they're polled via IMAP by
        # the separate check_newsletter_inbox job, not parsed as feeds here.
        if (podcast["feed_type"] or "") == "newsletter":
            continue
        if not force and not _feed_due(podcast):
            continue
        # Monitored web pages are re-scraped, not RSS-parsed. The 'Web-Clips'
        # collection (non-http rss_url) holds one-shot clips and is never rescanned.
        if (podcast["feed_type"] or "") == "website":
            if not str(podcast["rss_url"]).startswith("http"):
                continue
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    found = await _check_website(db, podcast)
                    new_count += found
                    await db.execute(
                        "UPDATE podcasts SET last_checked=CURRENT_TIMESTAMP WHERE id=?",
                        (podcast["id"],))
                    await db.commit()
                    await _log_check(db, podcast["id"], found, ok=True)
            except Exception as e:
                logger.error(f"Website check failed for {podcast['rss_url']}: {e}")
                async with aiosqlite.connect(DB_PATH) as db:
                    await _log_check(db, podcast["id"], 0, ok=False, error_msg=str(e))
            continue
        try:
            feed_data = await parse_rss_feed(podcast["rss_url"])
            episodes = feed_data["episodes"]

            async with aiosqlite.connect(DB_PATH) as db:
                inserted = await insert_new_episodes(
                    db, podcast["id"], episodes,
                    feed_type=podcast["feed_type"] or "podcast",
                    full_text_extraction=bool(podcast["full_text_extraction"]),
                    auto_transcribe=bool(podcast["auto_transcribe"]),
                    limit=podcast["max_episodes"] or 0,
                )
                new_count += inserted
                await db.execute(
                    """UPDATE podcasts SET last_checked=CURRENT_TIMESTAMP,
                       consecutive_fetch_errors=0, last_fetch_error=NULL WHERE id=?""",
                    (podcast["id"],),
                )
                await db.commit()
                await _log_check(db, podcast["id"], inserted, ok=True)
        except Exception as e:
            logger.error(f"Feed check failed for {podcast['rss_url']}: {e}")
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        """UPDATE podcasts SET
                           last_fetch_error=?,
                           consecutive_fetch_errors=COALESCE(consecutive_fetch_errors,0)+1
                           WHERE id=?""",
                        (str(e)[:300], podcast["id"]),
                    )
                    await db.commit()
                    await _log_check(db, podcast["id"], 0, ok=False, error_msg=str(e))
            except Exception:
                pass

    if new_count > 0:
        await send_notification(
            "PodScribe — Neue Folgen",
            f"{new_count} neue Folge(n) wurden zur Transkription hinzugefügt.",
            click_path="/",
        )
        await process_queued()
    return new_count


async def run_due_issues():
    """Hourly: generate recurring newsletters whose schedule matches now."""
    from datetime import datetime
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.id, s.recipe_id, s.cron_dow, s.cron_hour, s.last_run_at, r.name
            FROM scheduled_issues s JOIN issue_recipes r ON r.id = s.recipe_id
            WHERE s.enabled = 1
        """) as cur:
            schedules = [dict(r) for r in await cur.fetchall()]

    for s in schedules:
        if s["cron_dow"] != now.weekday() or s["cron_hour"] != now.hour:
            continue
        if (s["last_run_at"] or "").startswith(today):
            continue  # already ran today
        try:
            from .main import run_recipe
            digest_id = await run_recipe(s["recipe_id"])
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE scheduled_issues SET last_run_at=CURRENT_TIMESTAMP WHERE id=?",
                    (s["id"],))
                await db.commit()
            if digest_id:
                await send_notification(
                    f"📰 Neue Ausgabe: {s['name']}",
                    f"Deine automatische Ausgabe ist fertig.",
                    click_path=f"/digest/{digest_id}",
                )
        except Exception as e:
            logger.error(f"Scheduled issue (recipe {s['recipe_id']}) failed: {e}")


async def run_auto_digest():
    """Weekly 'Trending' issue built from the radar's hottest tags. Opt-in via
    the auto_digest_enabled setting; dow/hour gated like run_due_issues."""
    import json
    from datetime import datetime, timedelta
    from .database import get_setting, set_setting

    if (await get_setting("auto_digest_enabled")) != "1":
        return
    now = datetime.now()
    dow = int((await get_setting("auto_digest_dow")) or "0")
    hour = int((await get_setting("auto_digest_hour")) or "8")
    if now.weekday() != dow or now.hour != hour:
        return
    today = now.strftime("%Y-%m-%d")
    if (await get_setting("auto_digest_last_run")).startswith(today):
        return  # already ran today

    # Trending tags over the last 7 days (same shape as /api/radar).
    cur_from = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    prev_from = (now - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT t.id, t.label,
                   SUM(CASE WHEN e.pub_date >= ? THEN 1 ELSE 0 END) AS count_now,
                   SUM(CASE WHEN e.pub_date >= ? AND e.pub_date < ? THEN 1 ELSE 0 END) AS count_prev
            FROM episode_tags et
            JOIN episodes e ON e.id = et.episode_id
            JOIN tags t ON t.id = et.tag_id
            WHERE e.pub_date >= ?
            GROUP BY t.id
            HAVING count_now > 0
            ORDER BY (count_now - count_prev) DESC, count_now DESC
            LIMIT 5
        """, (cur_from, prev_from, cur_from, prev_from)) as cur:
            tags = [dict(r) for r in await cur.fetchall()]
    if not tags:
        await set_setting("auto_digest_last_run", now.strftime("%Y-%m-%d %H:%M:%S"))
        return

    try:
        from .main import _select_episode_ids, _build_issue
        tag_ids = [t["id"] for t in tags]
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await _select_episode_ids(db, date_window_days=7, tag_ids=tag_ids)
            episode_ids = [r["id"] for r in rows]
            if not episode_ids:
                await set_setting("auto_digest_last_run", now.strftime("%Y-%m-%d %H:%M:%S"))
                return
            title = f"Trending in deiner Bibliothek — {now.strftime('%d.%m.%Y')}"
            await db.execute(
                """INSERT INTO digests (title, mode, format, length, style, episode_ids_json, recipe_json, status)
                   VALUES (?, 'auto', 'daily_briefing', 3, 3, ?, ?, 'generating')""",
                (title, json.dumps(episode_ids),
                 json.dumps({"auto": True, "tag_ids": tag_ids, "date_window_days": 7})),
            )
            await db.commit()
            async with db.execute("SELECT last_insert_rowid()") as cur:
                digest_id = (await cur.fetchone())[0]
        await set_setting("auto_digest_last_run", now.strftime("%Y-%m-%d %H:%M:%S"))
        focus = "Themen: " + ", ".join(t["label"] for t in tags)
        await _build_issue(digest_id, episode_ids, "daily_briefing", 3, 3, title, "", "", focus)
        await send_notification(
            "📈 Trending-Ausgabe ist fertig",
            f"Dein automatischer Wochenüberblick ({focus}) wurde erstellt.",
            click_path=f"/digest/{digest_id}",
        )
    except Exception as e:
        logger.error(f"Auto digest failed: {e}")


async def run_due_editions():
    """Build any newspaper editions whose schedule is due. Runs every 30 min: a daily
    edition fires when the local hour matches its schedule_hour; a weekly edition also
    requires its schedule_dow (0=Mon … 6=Sun). A per-edition `last_run` date guard
    keeps each to once per period even though this job ticks twice an hour."""
    import aiosqlite
    from datetime import datetime
    from .database import DB_PATH

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name, kind, schedule_hour, schedule_dow, last_run "
            "FROM paper_editions WHERE enabled=1") as cur:
            editions = [dict(r) for r in await cur.fetchall()]

    for ed in editions:
        if now.hour != int(ed["schedule_hour"] or 7):
            continue
        if ed["kind"] == "weekly" and now.weekday() != int(ed["schedule_dow"] or 6):
            continue
        if (ed["last_run"] or "").startswith(today):
            continue  # already ran this period
        try:
            from .main import build_edition
            digest_id = await build_edition(ed["id"])  # also stamps last_run
            if digest_id:
                await send_notification(
                    f"📰 {ed['name']} ist fertig",
                    "Eine neue Ausgabe wurde erstellt.",
                    click_path=f"/digest/{digest_id}",
                )
        except Exception as e:
            logger.error(f"Edition '{ed['name']}' job failed: {e}")


async def check_newsletter_inbox():
    """Hourly tick; actually polls the IMAP inbox only when its own interval is
    due (default daily). Gated like _feed_due but via settings."""
    from .database import get_setting
    from .newsletter import check_inbox

    if (await get_setting("newsletter_enabled")) != "1":
        return
    interval = int((await get_setting("newsletter_check_interval_hours")) or "24")
    last = await get_setting("newsletter_last_checked")
    if interval > 1 and last:
        from datetime import datetime, timedelta
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
            if datetime.now() - last_dt < timedelta(hours=interval):
                return
        except ValueError:
            pass
    try:
        await check_inbox()
    except Exception as e:
        logger.error(f"Newsletter inbox job failed: {e}")


def start_scheduler():
    _scheduler.add_job(
        check_all_feeds,
        trigger=IntervalTrigger(hours=1),
        id="check_feeds",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.add_job(
        check_newsletter_inbox,
        trigger=IntervalTrigger(hours=1),
        id="check_newsletter",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.add_job(
        run_due_issues,
        trigger=IntervalTrigger(minutes=30),
        id="run_issues",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.add_job(
        run_auto_digest,
        trigger=IntervalTrigger(minutes=30),
        id="auto_digest",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.add_job(
        run_due_editions,
        trigger=IntervalTrigger(minutes=30),
        id="paper_editions",
        replace_existing=True,
        max_instances=1,
    )
    # Dedicated queue drainer: keeps the 'queued' backlog moving independently of
    # feed checks (manual transcribe, retranscribe, auto-transcribe toggle, etc.).
    _scheduler.add_job(
        drain_queue,
        trigger=IntervalTrigger(minutes=2),
        id="drain_queue",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if not _scheduler.running:
        _scheduler.start()


async def drain_queue():
    """Scheduled queue drainer (every 2 min). Recovers stuck items first, then
    processes all queued episodes via the lock-guarded processor.process_queued."""
    from .processor import process_queued, requeue_stuck
    try:
        await requeue_stuck()
        await process_queued()
    except Exception as e:
        logger.error(f"drain_queue failed: {e}")
    logger.info("Scheduler started")
