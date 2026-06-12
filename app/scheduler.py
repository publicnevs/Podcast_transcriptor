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

    # Check ALL feeds so new episodes get listed even when auto-transcription is
    # off. insert_new_episodes assigns 'pending' for non-auto podcasts, and
    # process_queued() below only processes 'queued' rows — so nothing is
    # auto-transcribed that the user didn't opt into.
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
            click_path="/",
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
    if not _scheduler.running:
        _scheduler.start()
    logger.info("Scheduler started")
