import logging

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .database import DB_PATH
from .notifier import send_notification

logger = logging.getLogger(__name__)
_scheduler = AsyncIOScheduler()


async def check_all_feeds():
    from .feed_parser import parse_rss_feed
    from .processor import process_queued

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM podcasts WHERE auto_transcribe = 1") as cur:
            podcasts = await cur.fetchall()

    new_count = 0
    for podcast in podcasts:
        try:
            feed_data = await parse_rss_feed(podcast["rss_url"])
            episodes = feed_data["episodes"]

            async with aiosqlite.connect(DB_PATH) as db:
                limit = podcast["max_episodes"] or len(episodes)
                for ep in episodes[:limit]:
                    async with db.execute(
                        "SELECT id FROM episodes WHERE audio_url = ?", (ep["audio_url"],)
                    ) as cur:
                        if await cur.fetchone():
                            continue
                    await db.execute(
                        """INSERT INTO episodes
                               (podcast_id, title, audio_url, episode_url, pub_date,
                                duration_sec, description, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
                        (podcast["id"], ep["title"], ep["audio_url"], ep["episode_url"],
                         ep["pub_date"], ep["duration_sec"], ep["description"]),
                    )
                    new_count += 1
                await db.execute(
                    "UPDATE podcasts SET last_checked = CURRENT_TIMESTAMP WHERE id = ?",
                    (podcast["id"],),
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Feed check failed for {podcast['rss_url']}: {e}")

    if new_count > 0:
        await send_notification(
            "PodScribe — Neue Folgen",
            f"{new_count} neue Folge(n) wurden zur Transkription hinzugefügt.",
        )
        await process_queued()


def start_scheduler():
    _scheduler.add_job(
        check_all_feeds,
        trigger=IntervalTrigger(hours=1),
        id="check_feeds",
        replace_existing=True,
        max_instances=1,
    )
    if not _scheduler.running:
        _scheduler.start()
    logger.info("Scheduler started")
