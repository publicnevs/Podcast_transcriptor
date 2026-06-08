import logging

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .database import DB_PATH
from .notifier import send_notification

logger = logging.getLogger(__name__)
_scheduler = AsyncIOScheduler()


def _feed_due(podcast) -> bool:
    """True if a feed is due for a check based on its check_interval_hours.
    The job runs hourly; feeds with a longer interval are skipped until due."""
    interval = podcast["check_interval_hours"] or 0
    last = podcast["last_checked"]
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


async def check_all_feeds():
    from .feed_parser import parse_rss_feed
    from .processor import process_queued, insert_new_episodes

    # Newsfeeds are checked even without auto_transcribe so new articles keep
    # flowing in; podcasts only when the user opted into auto-transcription.
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM podcasts
               WHERE auto_transcribe = 1 OR feed_type = 'newsfeed'"""
        ) as cur:
            podcasts = await cur.fetchall()

    new_count = 0
    for podcast in podcasts:
        if not _feed_due(podcast):
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
            except Exception:
                pass

    if new_count > 0:
        await send_notification(
            "PodScribe — Neue Folgen",
            f"{new_count} neue Folge(n) wurden zur Transkription hinzugefügt.",
        )
        await process_queued()


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
                    f"Deine automatische Ausgabe ist fertig.\nÖffnen: /digests",
                )
        except Exception as e:
            logger.error(f"Scheduled issue (recipe {s['recipe_id']}) failed: {e}")


def start_scheduler():
    _scheduler.add_job(
        check_all_feeds,
        trigger=IntervalTrigger(hours=1),
        id="check_feeds",
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
    if not _scheduler.running:
        _scheduler.start()
    logger.info("Scheduler started")
