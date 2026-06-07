import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import aiosqlite
import httpx
import markdown2
from fastapi import (BackgroundTasks, FastAPI, File, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, Response,
                                StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .database import DB_PATH, init_db, get_setting, set_setting
from .exporter import (bulk_export_markdown, export_ai_copy,
                        export_markdown, export_txt)
from .feed_parser import parse_rss_feed, parse_opml
from .processor import process_episode, process_queued
from .scheduler import start_scheduler
from . import transcriber
from .transcriber import generate_digest, translate_to_german

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


async def _apply_runtime_config():
    """Push DB settings into the transcriber runtime (api key, backend, model)."""
    api_key = await get_setting("gemini_api_key")
    backend = await get_setting("transcription_backend")
    whisper_model = await get_setting("whisper_model")
    transcriber.configure(
        gemini_api_key=api_key or os.getenv("GEMINI_API_KEY", ""),
        backend=backend or os.getenv("TRANSCRIPTION_BACKEND", "gemini"),
        whisper_model=whisper_model or os.getenv("WHISPER_MODEL", "base"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _apply_runtime_config()
    start_scheduler()
    yield


app = FastAPI(title="PodScribe", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Pydantic models ────────────────────────────────────────────────────────────

class PodcastCreate(BaseModel):
    rss_url: str
    auto_transcribe: bool = False
    max_episodes: int = 0


class PodcastUpdate(BaseModel):
    auto_transcribe: Optional[bool] = None
    max_episodes: Optional[int] = None
    check_interval_hours: Optional[int] = None


class TranscribeRequest(BaseModel):
    urls: List[str]
    podcast_id: Optional[int] = None


class NoteCreate(BaseModel):
    content: str


class SettingsUpdate(BaseModel):
    ntfy_topic: Optional[str] = None
    ntfy_url: Optional[str] = None
    check_interval_hours: Optional[int] = None
    transcription_backend: Optional[str] = None
    whisper_model: Optional[str] = None
    gemini_api_key: Optional[str] = None


class DigestRequest(BaseModel):
    episode_ids: List[int]
    title: str
    mode: str = "theme"


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Frontend pages ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def page_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/podcast/{podcast_id}", response_class=HTMLResponse)
async def page_podcast(podcast_id: int):
    return FileResponse(STATIC_DIR / "podcast.html")


@app.get("/episode/{episode_id}", response_class=HTMLResponse)
async def page_episode(episode_id: int):
    return FileResponse(STATIC_DIR / "episode.html")


@app.get("/settings", response_class=HTMLResponse)
async def page_settings():
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/digests", response_class=HTMLResponse)
async def page_digests():
    return FileResponse(STATIC_DIR / "digest.html")


@app.get("/sw.js")
async def service_worker():
    # Served from root so the PWA scope covers the whole app
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/about", response_class=HTMLResponse)
async def page_about():
    return FileResponse(STATIC_DIR / "about.html")


# ── Podcasts ───────────────────────────────────────────────────────────────────

@app.get("/api/podcasts")
async def list_podcasts():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT p.*,
                COUNT(CASE WHEN e.status='done' AND e.read_at IS NULL THEN 1 END) AS unread_count,
                COUNT(CASE WHEN e.status='done' THEN 1 END) AS done_count,
                COUNT(e.id) AS total_count
            FROM podcasts p
            LEFT JOIN episodes e ON e.podcast_id = p.id
            GROUP BY p.id
            ORDER BY p.title COLLATE NOCASE
        """) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# Curated starter feeds — KI/AI podcasts. Feeds providing <podcast:transcript>
# tags (e.g. Latent Space, Practical AI) are transcribed for free from the feed.
RECOMMENDED_PODCASTS = [
    {"title": "Latent Space", "lang": "en",
     "rss": "https://api.substack.com/feed/podcast/1084081.rss",
     "desc": "Entwickler-Deep-Dives: Code, Agenten, LLM-Architekturen, Gründer-Interviews. Liefert Transkripte im Feed.",
     "transcripts": True},
    {"title": "The Cognitive Revolution", "lang": "en",
     "rss": "https://api.substack.com/feed/podcast/1033902.rss",
     "desc": "Nathan Labenz interviewt KI-Forscher an der Front. Analytisch, Safety & Transformation.",
     "transcripts": False},
    {"title": "Practical AI", "lang": "en",
     "rss": "https://changelog.com/practicalai/feed",
     "desc": "Brücke zwischen ML-Theorie und produktivem Einsatz. Transkripte (VTT) im Feed enthalten.",
     "transcripts": True},
    {"title": "The AI Podcast (NVIDIA)", "lang": "en",
     "rss": "https://feeds.content.audioboom.com/podcasts/4929837.rss",
     "desc": "Kurze, gut produzierte Episoden zu konkreten KI-Use-Cases von Biologie bis autonomes Fahren.",
     "transcripts": False},
    {"title": "KI-Update (heise)", "lang": "de",
     "rss": "https://kiupdate.podigee.io/feed/mp3",
     "desc": "Werktägliches kurzes Update zu KI in Wirtschaft, Forschung und Praxis. Transkripte (JSON+VTT) im Feed.",
     "transcripts": True},
    {"title": "Tech, KI & Schmetterlinge (Sascha Lobo)", "lang": "de",
     "rss": "https://tech-ki.podigee.io/feed/mp3",
     "desc": "Sascha Lobo über Technologie, KI und digitale Souveränität. Transkripte (JSON+VTT) im Feed.",
     "transcripts": True},
    {"title": "Der KI-Podcast (ARD/BR)", "lang": "de",
     "rss": "https://feeds.br.de/der-ki-podcast/feed.xml",
     "desc": "Gesellschaftliche & praktische Auswirkungen von KI im Alltag. Gut recherchiert (Transkription via Gemini).",
     "transcripts": False},
]


@app.get("/api/recommended")
async def recommended_podcasts():
    """Curated starter list + which ones the user already subscribes to."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT rss_url FROM podcasts") as cur:
            subscribed = {r[0] for r in await cur.fetchall()}
    return [{**p, "subscribed": p["rss"] in subscribed} for p in RECOMMENDED_PODCASTS]


@app.post("/api/podcasts", status_code=201)
async def add_podcast(data: PodcastCreate, background_tasks: BackgroundTasks):
    try:
        feed_data = await parse_rss_feed(data.rss_url)
    except Exception as e:
        raise HTTPException(400, f"Feed konnte nicht gelesen werden: {e}")

    podcast = feed_data["podcast"]
    episodes = feed_data["episodes"]

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """INSERT INTO podcasts
                       (title, rss_url, artwork_url, description, website_url,
                        language, auto_transcribe, max_episodes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (podcast["title"], data.rss_url, podcast["artwork_url"],
                 podcast["description"], podcast["website_url"], podcast["language"],
                 1 if data.auto_transcribe else 0, data.max_episodes),
            )
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(400, "Podcast bereits abonniert")
            raise

        async with db.execute("SELECT last_insert_rowid()") as cur:
            podcast_id = (await cur.fetchone())[0]

        limit = data.max_episodes if data.max_episodes > 0 else len(episodes)
        for ep in episodes[:limit]:
            status = "queued" if data.auto_transcribe else "pending"
            try:
                await db.execute(
                    """INSERT OR IGNORE INTO episodes
                           (podcast_id, title, audio_url, episode_url, pub_date,
                            duration_sec, description, status,
                            transcript_url, transcript_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (podcast_id, ep["title"], ep["audio_url"], ep["episode_url"],
                     ep["pub_date"], ep["duration_sec"], ep["description"], status,
                     ep.get("transcript_url", ""), ep.get("transcript_type", "")),
                )
            except Exception:
                pass
        await db.commit()

    if data.auto_transcribe:
        background_tasks.add_task(process_queued)

    return {"id": podcast_id, "title": podcast["title"], "episode_count": len(episodes)}


@app.post("/api/podcasts/opml")
async def import_opml(file: UploadFile = File(...)):
    content = await file.read()
    urls = await parse_opml(content.decode("utf-8", errors="replace"))
    return {"found": len(urls), "urls": urls}


@app.get("/api/podcasts/{podcast_id}")
async def get_podcast(podcast_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM podcasts WHERE id=?", (podcast_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Podcast nicht gefunden")
    return dict(row)


@app.patch("/api/podcasts/{podcast_id}")
async def update_podcast(podcast_id: int, data: PodcastUpdate):
    fields = {}
    if data.auto_transcribe is not None:
        fields["auto_transcribe"] = 1 if data.auto_transcribe else 0
    if data.max_episodes is not None:
        fields["max_episodes"] = data.max_episodes
    if data.check_interval_hours is not None:
        fields["check_interval_hours"] = data.check_interval_hours
    if not fields:
        return {"ok": True}
    set_clause = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE podcasts SET {set_clause} WHERE id=?",
                         (*fields.values(), podcast_id))
        await db.commit()
    return {"ok": True}


@app.delete("/api/podcasts/{podcast_id}")
async def delete_podcast(podcast_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM podcasts WHERE id=?", (podcast_id,))
        await db.commit()
    return {"ok": True}


@app.get("/api/podcasts/{podcast_id}/episodes")
async def get_podcast_episodes(podcast_id: int, status: Optional[str] = None,
                                page: int = 1, limit: int = 50):
    offset = (page - 1) * limit
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where = "WHERE e.podcast_id=?"
        params: list = [podcast_id]
        if status:
            where += " AND e.status=?"
            params.append(status)
        async with db.execute(f"""
            SELECT e.*, s.summary, s.chapters_json
            FROM episodes e
            LEFT JOIN summaries s ON s.episode_id = e.id
            {where}
            ORDER BY e.pub_date DESC NULLS LAST, e.created_at DESC
            LIMIT ? OFFSET ?
        """, (*params, limit, offset)) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/podcasts/{podcast_id}/export")
async def bulk_export(podcast_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT title FROM podcasts WHERE id=?", (podcast_id,)) as cur:
            p = await cur.fetchone()
        if not p:
            raise HTTPException(404)
        async with db.execute("""
            SELECT e.title, e.pub_date, t.content AS transcript, s.summary
            FROM episodes e
            LEFT JOIN transcripts t ON t.episode_id = e.id
            LEFT JOIN summaries s ON s.episode_id = e.id
            WHERE e.podcast_id=? AND e.status='done'
            ORDER BY e.pub_date DESC
        """, (podcast_id,)) as cur:
            episodes = [dict(r) for r in await cur.fetchall()]

    content = bulk_export_markdown(p["title"], episodes)
    safe_title = p["title"].replace(" ", "_")[:40]
    return Response(
        content.encode("utf-8"),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}-transcripts.md"'},
    )


# ── Episodes ───────────────────────────────────────────────────────────────────

@app.post("/api/episodes/transcribe", status_code=202)
async def transcribe_urls(data: TranscribeRequest, background_tasks: BackgroundTasks):
    episode_ids = []
    async with aiosqlite.connect(DB_PATH) as db:
        for url in data.urls:
            url = url.strip()
            if not url:
                continue
            async with db.execute("SELECT id FROM episodes WHERE audio_url=?", (url,)) as cur:
                existing = await cur.fetchone()
            if existing:
                ep_id = existing[0]
                await db.execute("UPDATE episodes SET status='queued' WHERE id=?", (ep_id,))
            else:
                label = url.split("/")[-1][:100] or url[:80]
                await db.execute(
                    """INSERT INTO episodes (podcast_id, title, audio_url, status)
                       VALUES (?, ?, ?, 'queued')""",
                    (data.podcast_id, label, url),
                )
                await db.commit()
                async with db.execute("SELECT last_insert_rowid()") as cur:
                    ep_id = (await cur.fetchone())[0]
            episode_ids.append(ep_id)
            background_tasks.add_task(process_episode, ep_id, url, url.split("/")[-1], "Manuell")
        await db.commit()
    return {"queued": len(episode_ids), "episode_ids": episode_ids}


@app.get("/api/episodes/{episode_id}")
async def get_episode(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.*, p.title AS podcast_title, p.artwork_url,
                t.content AS transcript, t.segments_json, t.language,
                t.word_count, t.translation_de,
                s.summary, s.takeaways_json, s.chapters_json,
                n.content AS note
            FROM episodes e
            LEFT JOIN podcasts p ON p.id = e.podcast_id
            LEFT JOIN transcripts t ON t.episode_id = e.id
            LEFT JOIN summaries s ON s.episode_id = e.id
            LEFT JOIN notes n ON n.episode_id = e.id
            WHERE e.id=?
        """, (episode_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Episode nicht gefunden")
    return dict(row)


@app.get("/api/episodes/{episode_id}/audio")
async def stream_audio(episode_id: int, request: Request):
    """Proxy the source audio so the in-app player works regardless of CORS,
    forwarding Range headers so the browser can seek."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT audio_url FROM episodes WHERE id=?", (episode_id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(404, "Keine Audio-URL")
    audio_url = row[0]

    fwd_headers = {}
    if "range" in request.headers:
        fwd_headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    req = client.build_request("GET", audio_url, headers=fwd_headers)
    upstream = await client.send(req, stream=True)

    resp_headers = {"Accept-Ranges": "bytes"}
    for h in ("content-length", "content-range", "content-type"):
        if h in upstream.headers:
            resp_headers[h] = upstream.headers[h]

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=resp_headers.get("content-type", "audio/mpeg"),
    )


@app.patch("/api/episodes/{episode_id}/read")
async def mark_read(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE episodes SET read_at=CURRENT_TIMESTAMP WHERE id=?", (episode_id,)
        )
        await db.commit()
    return {"ok": True}


@app.patch("/api/episodes/{episode_id}/scroll")
async def save_scroll(episode_id: int, pos: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE episodes SET scroll_pos=? WHERE id=?", (pos, episode_id))
        await db.commit()
    return {"ok": True}


@app.patch("/api/episodes/{episode_id}/watchlist")
async def toggle_watchlist(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE episodes SET watchlist = CASE WHEN watchlist=1 THEN 0 ELSE 1 END WHERE id=?",
            (episode_id,),
        )
        await db.commit()
    return {"ok": True}


@app.post("/api/episodes/{episode_id}/retranscribe", status_code=202)
async def retranscribe(episode_id: int, background_tasks: BackgroundTasks):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.audio_url, e.title, p.title AS podcast_title
            FROM episodes e LEFT JOIN podcasts p ON p.id=e.podcast_id
            WHERE e.id=?
        """, (episode_id,)) as cur:
            ep = await cur.fetchone()
        if not ep:
            raise HTTPException(404)
        await db.execute("UPDATE episodes SET status='queued', error_msg=NULL WHERE id=?",
                         (episode_id,))
        await db.commit()
    background_tasks.add_task(
        process_episode, episode_id, ep["audio_url"], ep["title"],
        ep["podcast_title"] or "Manuell",
    )
    return {"ok": True}


@app.get("/api/episodes/{episode_id}/export")
async def export_episode(episode_id: int, format: str = "txt"):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.title, e.pub_date, p.title AS podcast_title,
                t.content, s.summary, s.takeaways_json, s.chapters_json
            FROM episodes e
            LEFT JOIN podcasts p ON p.id=e.podcast_id
            LEFT JOIN transcripts t ON t.episode_id=e.id
            LEFT JOIN summaries s ON s.episode_id=e.id
            WHERE e.id=?
        """, (episode_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404)

    d = dict(row)
    takeaways = json.loads(d.get("takeaways_json") or "[]")
    chapters = json.loads(d.get("chapters_json") or "[]")
    title = d.get("title") or "Episode"
    podcast = d.get("podcast_title") or ""
    date = d.get("pub_date") or ""
    transcript = d.get("content") or ""
    summary = d.get("summary") or ""

    if format == "txt":
        body = export_txt(title, podcast, date, transcript, summary, takeaways)
        return Response(body.encode("utf-8"), media_type="text/plain",
                        headers={"Content-Disposition": "attachment; filename=transcript.txt"})
    if format == "md":
        body = export_markdown(title, podcast, date, transcript, summary, takeaways, chapters)
        return Response(body.encode("utf-8"), media_type="text/markdown",
                        headers={"Content-Disposition": "attachment; filename=transcript.md"})
    if format == "ai":
        body = export_ai_copy(title, podcast, date, transcript, summary, takeaways)
        return Response(body.encode("utf-8"), media_type="text/plain")

    raise HTTPException(400, "Unbekanntes Format: txt, md, ai")


@app.post("/api/episodes/{episode_id}/translate")
async def translate_episode(episode_id: int, background_tasks: BackgroundTasks):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT content, translation_de FROM transcripts WHERE episode_id=?", (episode_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Kein Transkript vorhanden")
    if row["translation_de"]:
        return {"already_translated": True}
    background_tasks.add_task(_do_translate, episode_id, row["content"])
    return {"ok": True, "message": "Übersetzung wird erstellt…"}


async def _do_translate(episode_id: int, content: str):
    try:
        translation = await translate_to_german(content)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE transcripts SET translation_de=? WHERE episode_id=?",
                (translation, episode_id),
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Translation failed for episode {episode_id}: {e}")


@app.post("/api/episodes/{episode_id}/notes")
async def save_note(episode_id: int, data: NoteCreate):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO notes (episode_id, content) VALUES (?, ?)
               ON CONFLICT(episode_id) DO UPDATE SET
                   content=excluded.content, updated_at=CURRENT_TIMESTAMP""",
            (episode_id, data.content),
        )
        await db.commit()
    return {"ok": True}


# ── Search ─────────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str = Query(..., min_length=2), limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("""
                SELECT e.id, e.title, e.pub_date, p.title AS podcast_title, p.id AS podcast_id,
                    snippet(transcripts_fts, 1, '<mark>', '</mark>', '…', 25) AS snippet
                FROM transcripts_fts
                JOIN transcripts t ON transcripts_fts.rowid = t.id
                JOIN episodes e ON t.episode_id = e.id
                LEFT JOIN podcasts p ON p.id = e.podcast_id
                WHERE transcripts_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (q, limit)) as cur:
                rows = await cur.fetchall()
        except Exception:
            rows = []
    return [dict(r) for r in rows]


# ── Queue ──────────────────────────────────────────────────────────────────────

@app.get("/api/queue")
async def get_queue():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.id, e.title, e.status, e.error_msg, e.created_at,
                p.title AS podcast_title
            FROM episodes e
            LEFT JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.status IN ('queued','downloading','transcribing','error')
            ORDER BY e.created_at DESC
            LIMIT 50
        """) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Watchlist ──────────────────────────────────────────────────────────────────

@app.get("/api/watchlist")
async def get_watchlist():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.id, e.title, e.status, e.pub_date, e.read_at,
                p.title AS podcast_title, p.artwork_url, p.id AS podcast_id
            FROM episodes e
            LEFT JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.watchlist=1
            ORDER BY e.created_at DESC
        """) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Settings ───────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
    out = {r[0]: r[1] for r in rows}
    # Never leak the raw API key — only signal whether one is set
    out["gemini_api_key_set"] = bool(out.pop("gemini_api_key", "") or os.getenv("GEMINI_API_KEY", ""))
    return out


@app.put("/api/settings")
async def update_settings(data: SettingsUpdate):
    if data.ntfy_topic is not None:
        await set_setting("ntfy_topic", data.ntfy_topic)
    if data.ntfy_url is not None:
        await set_setting("ntfy_url", data.ntfy_url)
    if data.check_interval_hours is not None:
        await set_setting("check_interval_hours", str(data.check_interval_hours))
    if data.transcription_backend is not None:
        await set_setting("transcription_backend", data.transcription_backend)
    if data.whisper_model is not None:
        await set_setting("whisper_model", data.whisper_model)
    if data.gemini_api_key:  # only overwrite when a non-empty value is sent
        await set_setting("gemini_api_key", data.gemini_api_key.strip())
    await _apply_runtime_config()
    return {"ok": True}


# ── Scheduler ──────────────────────────────────────────────────────────────────

@app.post("/api/scheduler/trigger")
async def trigger_check(background_tasks: BackgroundTasks):
    from .scheduler import check_all_feeds
    background_tasks.add_task(check_all_feeds)
    return {"ok": True, "message": "Feed-Check gestartet"}


# ── Digests ────────────────────────────────────────────────────────────────────

@app.get("/api/digests")
async def list_digests():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, mode, status, created_at FROM digests ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/digests", status_code=201)
async def create_digest(data: DigestRequest, background_tasks: BackgroundTasks):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO digests (title, mode, episode_ids_json) VALUES (?, ?, ?)",
            (data.title, data.mode, json.dumps(data.episode_ids)),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            digest_id = (await cur.fetchone())[0]

    background_tasks.add_task(_build_digest, digest_id, data.episode_ids, data.mode, data.title)
    return {"id": digest_id, "status": "generating"}


async def _build_digest(digest_id: int, episode_ids: list, mode: str, title: str):
    try:
        episode_data = []
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            for ep_id in episode_ids:
                async with db.execute("""
                    SELECT e.title, e.pub_date, p.title AS podcast_title, t.content AS transcript
                    FROM episodes e
                    LEFT JOIN podcasts p ON p.id=e.podcast_id
                    LEFT JOIN transcripts t ON t.episode_id=e.id
                    WHERE e.id=?
                """, (ep_id,)) as cur:
                    row = await cur.fetchone()
                    if row:
                        episode_data.append(dict(row))

        result = await generate_digest(episode_data, mode, title)
        content_md = result.get("content_md", "")
        content_html = markdown2.markdown(
            content_md, extras=["fenced-code-blocks", "tables", "header-ids"]
        )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE digests SET content_html=?, content_md=?, status='done' WHERE id=?",
                (content_html, content_md, digest_id),
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Digest {digest_id} failed: {e}")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE digests SET status='error' WHERE id=?", (digest_id,)
            )
            await db.commit()


@app.get("/api/digests/{digest_id}")
async def get_digest(digest_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM digests WHERE id=?", (digest_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)


@app.delete("/api/digests/{digest_id}")
async def delete_digest(digest_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM digests WHERE id=?", (digest_id,))
        await db.commit()
    return {"ok": True}
