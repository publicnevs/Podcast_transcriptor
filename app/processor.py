import json
import logging
from pathlib import Path

import aiosqlite

from .database import DB_PATH
from .downloader import download_audio
from .notifier import send_notification
from .transcriber import transcribe_audio, enrich_text
from .transcript_fetch import fetch_transcript

logger = logging.getLogger(__name__)


async def _get_transcript_source(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT transcript_url, transcript_type FROM episodes WHERE id=?",
            (episode_id,),
        ) as cur:
            row = await cur.fetchone()
    return (row[0] or "", row[1] or "") if row else ("", "")


async def process_episode(episode_id: int, audio_url: str, title: str, podcast_title: str):
    audio_path: Path | None = None
    try:
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
                """INSERT INTO summaries (episode_id, summary, takeaways_json, chapters_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(episode_id) DO UPDATE SET
                       summary=excluded.summary,
                       takeaways_json=excluded.takeaways_json,
                       chapters_json=excluded.chapters_json""",
                (episode_id, data.get("summary", ""),
                 json.dumps(data.get("takeaways", [])),
                 json.dumps(data.get("chapters", []))),
            )
            await db.execute("UPDATE episodes SET status='done' WHERE id=?", (episode_id,))
            await db.commit()

        await send_notification(
            f"Transkript fertig: {title[:50]}",
            f"Podcast: {podcast_title}\n{word_count} Wörter transkribiert.",
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
            """SELECT e.id, e.audio_url, e.title, p.title as podcast_title
               FROM episodes e
               LEFT JOIN podcasts p ON p.id = e.podcast_id
               WHERE e.status = 'queued'
               ORDER BY e.created_at ASC
               LIMIT 3"""
        ) as cur:
            rows = await cur.fetchall()

    for row in rows:
        await process_episode(row["id"], row["audio_url"], row["title"],
                               row["podcast_title"] or "Manuell")


async def _set_status(episode_id: int, status: str, error_msg: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if error_msg:
            await db.execute(
                "UPDATE episodes SET status=?, error_msg=? WHERE id=?",
                (status, error_msg, episode_id),
            )
        else:
            await db.execute("UPDATE episodes SET status=? WHERE id=?", (status, episode_id))
        await db.commit()
