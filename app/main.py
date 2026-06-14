import asyncio
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import aiosqlite
import httpx
import markdown2
from fastapi import (BackgroundTasks, FastAPI, File, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                                RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth
from .database import DB_PATH, init_db, get_setting, set_setting
from .exporter import (bulk_export_markdown, export_ai_copy, export_chat_markdown,
                        export_markdown, export_txt)
from .feed_parser import parse_rss_feed, parse_opml
from .processor import process_episode, process_queued, insert_new_episodes
from .scheduler import start_scheduler
from . import transcriber
from .transcriber import (generate_digest, generate_issue, translate_to_german,
                          extract_tags, _LENGTH_MAP, _STYLE_MAP)
from . import tagging

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


async def _apply_runtime_config():
    """Push DB settings into the transcriber runtime (api key, backend, model)."""
    api_key = await get_setting("gemini_api_key")
    backend = await get_setting("transcription_backend")
    whisper_model = await get_setting("whisper_model")
    digest_model = await get_setting("digest_model")
    transcriber.configure(
        gemini_api_key=api_key or os.getenv("GEMINI_API_KEY", ""),
        backend=backend or os.getenv("TRANSCRIPTION_BACKEND", "gemini"),
        whisper_model=whisper_model or os.getenv("WHISPER_MODEL", "base"),
        digest_model=digest_model or "",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _apply_runtime_config()
    start_scheduler()
    yield


app = FastAPI(title="PodScribe", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Role guard (owner vs read-only guest) ───────────────────────────────────────
# Open-by-default: when no owner password is configured, current_role() returns
# "owner" for everyone. Once configured, guests may only GET the allowlisted
# read paths below; everything else (writes + cost actions) is owner-only.
_GUEST_PAGE_RE = re.compile(
    r"^/(?:$|podcast/\d+$|episode/\d+$|digests$|digest/\d+$|tags$|tags/\d+$"
    r"|topic/\d+$|about$|discover$|radar$|search$|login$|inbox$|category/\d+$)"
)
_GUEST_STATIC_RE = re.compile(r"^/(?:static/|sw\.js$|manifest\.json$|health$|s/|favicon)")
_GUEST_API_RE = re.compile(
    r"^/api/(?:me$|podcasts$|podcasts/\d+$|podcasts/\d+/episodes$|podcasts/\d+/export$"
    r"|episodes/\d+$|episodes/\d+/audio$|episodes/\d+/tags$|episodes/\d+/related$"
    r"|episodes/\d+/export$|search$|tags$|tags/\d+/episodes$|digests$|digests/\d+$"
    r"|radar$|overview/week$|recommended$|recipes$|issue-options$"
    r"|recent-takeaways$|topics/\d+$|categories$|categories/\d+$|inbox$)$"
)


def _guest_get_ok(path: str) -> bool:
    return bool(_GUEST_PAGE_RE.match(path)
                or _GUEST_STATIC_RE.match(path)
                or _GUEST_API_RE.match(path))


@app.middleware("http")
async def role_guard(request: Request, call_next):
    role = await auth.current_role(request)
    request.state.role = role
    if role == "owner":
        return await call_next(request)
    path, method = request.url.path, request.method
    if method in ("GET", "HEAD") and _guest_get_ok(path):
        return await call_next(request)
    if method == "POST" and path in ("/api/login", "/api/logout"):
        return await call_next(request)
    # Guest RAG/chat: opt-in via setting + per-IP rate limit (LLM cost).
    if method == "POST" and path in ("/api/ask", "/api/chat") and await auth.guest_rag_enabled():
        ip = request.client.host if request.client else "?"
        if auth.allow_rag(ip):
            return await call_next(request)
        return JSONResponse({"detail": "Limit erreicht. Bitte später erneut."}, status_code=429)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Nur für Eigentümer"}, status_code=403)
    return RedirectResponse("/login")


# ── Pydantic models ────────────────────────────────────────────────────────────

class PodcastCreate(BaseModel):
    rss_url: str
    auto_transcribe: bool = False
    max_episodes: int = 0
    # Default ON: article feeds that only ship a teaser get the full page fetched.
    full_text_extraction: bool = True


class PodcastUpdate(BaseModel):
    auto_transcribe: Optional[bool] = None
    max_episodes: Optional[int] = None
    check_interval_hours: Optional[int] = None
    full_text_extraction: Optional[bool] = None
    max_transcripts: Optional[int] = None
    category_id: Optional[int] = None
    position: Optional[int] = None
    artwork_url: Optional[str] = None


class CategoryCreate(BaseModel):
    name: str


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    position: Optional[int] = None


class ReorderItem(BaseModel):
    id: int
    position: int
    category_id: Optional[int] = None


class WebsiteSource(BaseModel):
    url: str
    title: str = ""


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
    digest_model: Optional[str] = None
    public_base_url: Optional[str] = None
    # Newsletter inbox (IMAP)
    newsletter_enabled: Optional[bool] = None
    newsletter_imap_host: Optional[str] = None
    newsletter_imap_port: Optional[int] = None
    newsletter_imap_user: Optional[str] = None
    newsletter_imap_password: Optional[str] = None
    newsletter_check_interval_hours: Optional[int] = None
    # Digest email delivery (SMTP)
    digest_email_enabled: Optional[bool] = None
    digest_email_to: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    # Access control (owner/guest)
    owner_password: Optional[str] = None
    guest_password: Optional[str] = None
    guest_rag_enabled: Optional[bool] = None
    # Weekly auto digest from trending tags
    auto_digest_enabled: Optional[bool] = None
    auto_digest_dow: Optional[int] = None
    auto_digest_hour: Optional[int] = None


class LoginRequest(BaseModel):
    password: str


class NewsletterTest(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None


class SmtpTest(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None


class AskRequest(BaseModel):
    question: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = []


class ChatExportMessage(BaseModel):
    role: str
    content: str = ""
    sources: List[dict] = []


class ChatExportRequest(BaseModel):
    messages: List[ChatExportMessage] = []


class DigestRequest(BaseModel):
    episode_ids: List[int] = []
    title: str
    mode: str = "theme"          # legacy
    format: str = "daily_briefing"
    length: int = 3              # 1..5
    style: int = 3               # 1..5
    model: str = ""              # pro | flash | lite | "" (format default)
    custom_style: str = ""
    focus: str = ""
    prompt: str = ""             # free-text instruction for the "artikel" format
    selection_mode: str = "manual"  # manual | ai (RAG picks episodes from prompt)
    recipe: Optional[dict] = None  # {date_window_days?, date_from?, date_to?, podcast_ids[], tag_ids[], match}


class IssueSelect(BaseModel):
    date_window_days: Optional[int] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    podcast_ids: List[int] = []
    tag_ids: List[int] = []
    match: str = "any"           # any | all


class RecipeCreate(BaseModel):
    name: str
    format: str = "daily_briefing"
    date_window_days: Optional[int] = 7
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    podcast_ids: List[int] = []
    tag_ids: List[int] = []
    match: str = "any"
    length: int = 3
    style: int = 3
    model: str = ""
    custom_style: str = ""
    focus: str = ""


class ScheduleUpsert(BaseModel):
    recipe_id: int
    cron_dow: int = 0
    cron_hour: int = 7
    enabled: bool = True


class TagRename(BaseModel):
    label: str


class EpisodeTagsUpdate(BaseModel):
    add: List[str] = []
    remove: List[int] = []


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/health/whisper")
async def health_whisper():
    """Check whether faster-whisper is installed and which models are cached."""
    try:
        import faster_whisper  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    cached_models: list[str] = []
    cache_dir = os.getenv("WHISPER_CACHE", "/app/data/whisper_models")
    try:
        from pathlib import Path as _P
        for p in _P(cache_dir).iterdir():
            if p.is_dir():
                cached_models.append(p.name)
    except Exception:
        pass
    return {"installed": installed, "cached_models": cached_models, "cache_dir": cache_dir}


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


@app.get("/discover", response_class=HTMLResponse)
async def page_discover():
    return FileResponse(STATIC_DIR / "discover.html")


@app.get("/tags", response_class=HTMLResponse)
async def page_tags():
    return FileResponse(STATIC_DIR / "tags.html")


@app.get("/tags/{tag_id}", response_class=HTMLResponse)
async def page_tag_detail(tag_id: int):
    return FileResponse(STATIC_DIR / "tags.html")


@app.get("/digest/{digest_id}", response_class=HTMLResponse)
async def page_digest_reader(digest_id: int):
    return FileResponse(STATIC_DIR / "digest-reader.html")


@app.get("/search", response_class=HTMLResponse)
async def page_search():
    return FileResponse(STATIC_DIR / "search.html")


@app.get("/radar", response_class=HTMLResponse)
async def page_radar():
    return FileResponse(STATIC_DIR / "radar.html")


@app.get("/topic/{tag_id}", response_class=HTMLResponse)
async def page_topic(tag_id: int):
    return FileResponse(STATIC_DIR / "topic.html")


@app.get("/login", response_class=HTMLResponse)
async def page_login():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/inbox", response_class=HTMLResponse)
async def page_inbox():
    return FileResponse(STATIC_DIR / "inbox.html")


@app.get("/category/{category_id}", response_class=HTMLResponse)
async def page_category(category_id: int):
    return FileResponse(STATIC_DIR / "category.html")


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
            ORDER BY p.position ASC, p.title COLLATE NOCASE
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
    {"title": "Me, Myself, and AI (MIT Sloan)", "lang": "en",
     "rss": "https://feeds.megaphone.fm/TPG7603691495",
     "desc": "MIT Sloan Management Review: wie Unternehmen KI strategisch einsetzen. 118 Folgen.",
     "transcripts": False},
    {"title": "The AI in Business Podcast (Emerj)", "lang": "en",
     "rss": "https://techemergence.libsyn.com/rss",
     "desc": "Daniel Faggella über KI im Unternehmenseinsatz. Riesiges, aktives Archiv (1100+ Folgen).",
     "transcripts": False},
    {"title": "Everyday AI Podcast", "lang": "en",
     "rss": "https://rss.buzzsprout.com/2175779.rss",
     "desc": "Täglicher, praxisnaher Podcast zu KI-Tools, ChatGPT und Workflows.",
     "transcripts": False},
    {"title": "Your Copilot (Microsoft 365)", "lang": "de",
     "rss": "https://podcast.yourcopilot.de/feed/mp3",
     "desc": "KI in der Microsoft-365-Welt verstehen und anwenden. Transkripte im Feed enthalten.",
     "transcripts": True},
    {"title": "KI verstehen (Deutschlandfunk)", "lang": "de",
     "rss": "https://www.deutschlandfunk.de/ki-verstehen-102.xml",
     "desc": "DLF erklärt, was KI kann, was nicht und was sie mit uns macht. Gut recherchiert.",
     "transcripts": False},
    {"title": "KI Kompakt", "lang": "de",
     "rss": "https://kikompakt.podigee.io/feed/mp3",
     "desc": "Kompakte Einordnung aktueller KI-Themen auf Deutsch.",
     "transcripts": False},
    {"title": "Prompt mich mal!", "lang": "de",
     "rss": "https://letscast.fm/podcasts/prompt-mich-mal-dd164d90/feed",
     "desc": "Podcast über KI, ChatGPT und kreative Prompts — praxisnah auf Deutsch.",
     "transcripts": False},
    {"title": "KI REVOLUTION (Everlast AI)", "lang": "de",
     "rss": "https://anchor.fm/s/d9945e24/podcast/rss",
     "desc": "Business-Podcast von Everlast AI: Künstliche Intelligenz, Zukunft, Automation.",
     "transcripts": False},
    {"title": "KI TALK (Maxi Raabe & Niklas Volland)", "lang": "de",
     "rss": "https://anchor.fm/s/f48c2e50/podcast/rss",
     "desc": "Zwei Praktiker diskutieren aktuelle KI-Entwicklungen und Tools.",
     "transcripts": False},
    {"title": "Der KI-Unternehmer", "lang": "de",
     "rss": "https://feeds.libsyn.com/323729/rss",
     "desc": "Strategien zum Erfolg mit KI im Unternehmen. Großes Archiv (500+ Folgen).",
     "transcripts": False},
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
    feed_type = feed_data.get("feed_type", "podcast")
    # When a web page URL was given, autodiscovery resolves the real feed URL —
    # persist that so future scheduler checks hit the feed directly.
    feed_url = podcast.get("rss_url") or data.rss_url

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """INSERT INTO podcasts
                       (title, rss_url, artwork_url, description, website_url,
                        language, auto_transcribe, max_episodes, feed_type, full_text_extraction)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (podcast["title"], feed_url, podcast["artwork_url"],
                 podcast["description"], podcast["website_url"], podcast["language"],
                 1 if data.auto_transcribe else 0, data.max_episodes,
                 feed_type, 1 if data.full_text_extraction else 0),
            )
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(400, "Podcast bereits abonniert")
            raise

        async with db.execute("SELECT last_insert_rowid()") as cur:
            podcast_id = (await cur.fetchone())[0]

        # On the first subscribe, only pull in the 3 newest episodes so the
        # Neuzugänge inbox isn't flooded with a feed's entire back catalogue.
        # Later scheduler runs use the podcast's own max_episodes unchanged.
        initial_limit = min(3, data.max_episodes) if data.max_episodes > 0 else 3
        await insert_new_episodes(
            db, podcast_id, episodes,
            feed_type=feed_type,
            full_text_extraction=data.full_text_extraction,
            auto_transcribe=data.auto_transcribe,
            limit=initial_limit,
        )
        await db.commit()

    if data.auto_transcribe or (feed_type == "newsfeed" and data.full_text_extraction):
        background_tasks.add_task(process_queued)

    return {"id": podcast_id, "title": podcast["title"],
            "episode_count": len(episodes), "feed_type": feed_type}


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
    if data.full_text_extraction is not None:
        fields["full_text_extraction"] = 1 if data.full_text_extraction else 0
    if data.artwork_url is not None:
        # Empty string clears the override → fall back to the generated avatar.
        fields["artwork_url"] = data.artwork_url.strip() or None
    if data.max_transcripts is not None:
        fields["max_transcripts"] = data.max_transcripts
    if data.category_id is not None:
        # 0 (or negative) means "no category" → store NULL
        fields["category_id"] = data.category_id if data.category_id > 0 else None
    if data.position is not None:
        fields["position"] = data.position
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
        await db.execute(
            "DELETE FROM transcripts_fts WHERE episode_id IN "
            "(SELECT id FROM episodes WHERE podcast_id=?)", (podcast_id,))
        await db.execute("DELETE FROM podcasts WHERE id=?", (podcast_id,))
        await db.commit()
    return {"ok": True}


@app.post("/api/podcasts/reorder")
async def reorder_podcasts(items: List[ReorderItem]):
    """Persist drag-&-drop ordering (and optional category assignment) from the
    library homepage. category_id=0/negative clears the category (NULL)."""
    async with aiosqlite.connect(DB_PATH) as db:
        for it in items:
            if it.category_id is None:
                await db.execute("UPDATE podcasts SET position=? WHERE id=?",
                                 (it.position, it.id))
            else:
                cat = it.category_id if it.category_id > 0 else None
                await db.execute(
                    "UPDATE podcasts SET position=?, category_id=? WHERE id=?",
                    (it.position, cat, it.id))
        await db.commit()
    return {"ok": True}


# ── Categories (user-managed) ────────────────────────────────────────────────

@app.get("/api/categories")
async def list_categories():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT c.id, c.name, c.position,
                   (SELECT COUNT(*) FROM podcasts p WHERE p.category_id=c.id) AS podcast_count
            FROM categories c
            ORDER BY c.position ASC, c.name COLLATE NOCASE
        """) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/categories", status_code=201)
async def create_category(data: CategoryCreate):
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Name darf nicht leer sein.")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(MAX(position), -1)+1 FROM categories") as cur:
            pos = (await cur.fetchone())[0]
        try:
            cur = await db.execute(
                "INSERT INTO categories (name, position) VALUES (?, ?)", (name, pos))
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(400, "Kategorie existiert bereits.")
            raise
        return {"id": cur.lastrowid, "name": name, "position": pos}


@app.patch("/api/categories/{category_id}")
async def update_category(category_id: int, data: CategoryUpdate):
    fields = {}
    if data.name is not None and data.name.strip():
        fields["name"] = data.name.strip()
    if data.position is not None:
        fields["position"] = data.position
    if not fields:
        return {"ok": True}
    set_clause = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(f"UPDATE categories SET {set_clause} WHERE id=?",
                             (*fields.values(), category_id))
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(400, "Kategorie existiert bereits.")
            raise
    return {"ok": True}


@app.delete("/api/categories/{category_id}")
async def delete_category(category_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # Detach podcasts first (FK ON DELETE SET NULL only fires on new DBs).
        await db.execute("UPDATE podcasts SET category_id=NULL WHERE category_id=?",
                         (category_id,))
        await db.execute("DELETE FROM categories WHERE id=?", (category_id,))
        await db.commit()
    return {"ok": True}


@app.get("/api/categories/{category_id}")
async def get_category(category_id: int):
    """Bundle a category's podcasts, their latest episodes, and topic tags."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories WHERE id=?", (category_id,)) as cur:
            cat = await cur.fetchone()
        if not cat:
            raise HTTPException(404, "Kategorie nicht gefunden")
        async with db.execute("""
            SELECT p.*,
                COUNT(CASE WHEN e.status='done' AND e.read_at IS NULL THEN 1 END) AS unread_count,
                COUNT(CASE WHEN e.status='done' THEN 1 END) AS done_count,
                COUNT(e.id) AS total_count
            FROM podcasts p
            LEFT JOIN episodes e ON e.podcast_id = p.id
            WHERE p.category_id=?
            GROUP BY p.id
            ORDER BY p.position ASC, p.title COLLATE NOCASE
        """, (category_id,)) as cur:
            podcasts = [dict(r) for r in await cur.fetchall()]
        async with db.execute("""
            SELECT e.id, e.title, e.pub_date, e.status, p.title AS podcast_title
            FROM episodes e JOIN podcasts p ON p.id=e.podcast_id
            WHERE p.category_id=? AND e.status='done'
            ORDER BY e.pub_date IS NULL, e.pub_date DESC, e.created_at DESC
            LIMIT 20
        """, (category_id,)) as cur:
            episodes = [dict(r) for r in await cur.fetchall()]
        async with db.execute("""
            SELECT t.id, t.label, COUNT(*) AS cnt
            FROM episode_tags et
            JOIN tags t ON t.id=et.tag_id
            JOIN episodes e ON e.id=et.episode_id
            JOIN podcasts p ON p.id=e.podcast_id
            WHERE p.category_id=?
            GROUP BY t.id ORDER BY cnt DESC LIMIT 30
        """, (category_id,)) as cur:
            tags = [dict(r) for r in await cur.fetchall()]
    return {"category": dict(cat), "podcasts": podcasts,
            "episodes": episodes, "tags": tags}


# ── Website sources (scrape a URL: one-shot or recurring) ────────────────────

WEBCLIPS_RSS = "webclips:local"


async def _ensure_webclips_podcast(db) -> int:
    """Return the id of the shared 'Web-Clips' collection podcast, creating it once."""
    await db.execute(
        """INSERT OR IGNORE INTO podcasts (title, rss_url, feed_type, description)
           VALUES ('Web-Clips', ?, 'website', 'Einmalig aufbereitete Web-Seiten')""",
        (WEBCLIPS_RSS,))
    async with db.execute("SELECT id FROM podcasts WHERE rss_url=?", (WEBCLIPS_RSS,)) as cur:
        return (await cur.fetchone())[0]


@app.post("/api/scrape", status_code=202)
async def scrape_url(data: WebsiteSource, background_tasks: BackgroundTasks):
    """One-shot: fetch a web page, prepare it as an article episode under Web-Clips."""
    from .processor import _fetch_article_text
    url = data.url.strip()
    if not url:
        raise HTTPException(400, "URL fehlt.")
    text = await _fetch_article_text(url)
    if not text:
        raise HTTPException(400, "Konnte keinen Text von der Seite extrahieren.")
    title = data.title.strip() or url.split("//")[-1][:120]
    async with aiosqlite.connect(DB_PATH) as db:
        podcast_id = await _ensure_webclips_podcast(db)
        await db.execute(
            """INSERT INTO episodes (podcast_id, title, audio_url, episode_url,
                   pub_date, description, status)
               VALUES (?, ?, '', ?, ?, ?, 'queued')""",
            (podcast_id, title, url,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
        await db.commit()
    background_tasks.add_task(process_queued)
    return {"ok": True, "title": title}


@app.post("/api/podcasts/website", status_code=201)
async def add_website_source(data: WebsiteSource, background_tasks: BackgroundTasks):
    """Subscribe a web page as a recurring 'website' source — the scheduler
    re-scrapes it and adds a new episode whenever the page content changes."""
    import hashlib
    from .processor import _fetch_html, _extract_main_text, _fetch_site_image
    url = data.url.strip()
    if not url:
        raise HTTPException(400, "URL fehlt.")
    html = await _fetch_html(url)
    text = _extract_main_text(html, url) if html else ""
    if not text:
        raise HTTPException(400, "Konnte keinen Text von der Seite extrahieren.")
    title = data.title.strip() or url.split("//")[-1][:120]
    # Best-effort logo from the same page we already fetched (og:image/favicon).
    artwork = await _fetch_site_image(url, html=html)
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                """INSERT INTO podcasts (title, rss_url, website_url, feed_type,
                       auto_transcribe, artwork_url)
                   VALUES (?, ?, ?, 'website', 1, ?)""",
                (title, url, url, artwork or None))
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(400, "Diese Website wird bereits überwacht.")
            raise
        podcast_id = cur.lastrowid
        await db.execute(
            """INSERT INTO episodes (podcast_id, title, audio_url, episode_url,
                   pub_date, description, status)
               VALUES (?, ?, '', ?, ?, ?, 'queued')""",
            (podcast_id, title, f"website-hash:{digest}",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
        await db.commit()
    background_tasks.add_task(process_queued)
    return {"id": podcast_id, "title": title}


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
            SELECT e.*, s.summary, s.chapters_json,
                (SELECT GROUP_CONCAT(t.label || '|' || t.id, ',')
                 FROM episode_tags et JOIN tags t ON t.id=et.tag_id
                 WHERE et.episode_id=e.id) AS tags_csv,
                strftime('%Y-%m-%d %H:%M:%S', 'now') AS server_now
            FROM episodes e
            LEFT JOIN summaries s ON s.episode_id = e.id
            {where}
            ORDER BY e.pub_date IS NULL, e.pub_date DESC, e.created_at DESC
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

@app.delete("/api/episodes/{episode_id}")
async def delete_episode(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # FTS virtual table is not covered by CASCADE — delete manually first
        await db.execute("DELETE FROM transcripts_fts WHERE episode_id=?", (episode_id,))
        await db.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
        await db.commit()
    return {"ok": True}


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


@app.post("/api/episodes/{episode_id}/transcribe", status_code=202)
async def transcribe_one(episode_id: int, background_tasks: BackgroundTasks):
    """Queue a single, already-known episode for transcription/enrichment on
    demand (used by the Neuzugänge inbox 'Transkribieren' button)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM episodes WHERE id=?", (episode_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(404, "Folge nicht gefunden")
        await db.execute("UPDATE episodes SET status='queued', error_msg=NULL WHERE id=?",
                         (episode_id,))
        await db.commit()
    background_tasks.add_task(process_queued)
    return {"ok": True, "episode_id": episode_id}


@app.get("/api/inbox")
async def get_inbox():
    """Timeline of recent arrivals across all feeds: everything added in the last
    30 days (including already-processed 'done' items) plus any still-unprocessed
    episode regardless of age (pending opt-in, queued/running, or failed)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.id, e.title, e.status, e.pub_date, e.created_at, e.audio_url,
                   e.error_msg, p.id AS podcast_id, p.title AS podcast_title,
                   p.feed_type, p.artwork_url
            FROM episodes e
            LEFT JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.created_at >= datetime('now', '-30 days')
               OR e.status IN ('pending','queued','downloading','transcribing','error')
            ORDER BY e.created_at DESC, e.pub_date DESC
            LIMIT 200
        """) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/feeds/check-all")
async def check_all_feeds_now():
    """On-demand scan of every feed plus the newsletter inbox (the inbox
    'Aktualisieren' button). Returns how many new items were listed."""
    from .scheduler import check_all_feeds
    from . import newsletter
    new_feeds = 0
    try:
        new_feeds = await check_all_feeds(force=True)
    except Exception as e:
        logger.warning(f"check-all feeds failed: {e}")
    new_mail = 0
    if (await get_setting("newsletter_enabled")) == "1":
        try:
            new_mail = await newsletter.check_inbox()
        except Exception as e:
            logger.warning(f"check-all newsletter failed: {e}")
    return {"ok": True, "new_feeds": new_feeds, "new_mail": new_mail}


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
        await db.execute("DELETE FROM transcripts_fts WHERE episode_id=?", (episode_id,))
        await db.execute("DELETE FROM transcripts WHERE episode_id=?", (episode_id,))
        await db.execute("DELETE FROM summaries WHERE episode_id=?", (episode_id,))
        await db.execute("DELETE FROM episode_tags WHERE episode_id=?", (episode_id,))
        await db.execute(
            "UPDATE episodes SET status='queued', error_msg=NULL, processing_started_at=NULL WHERE id=?",
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

    import re as _re
    slug = _re.sub(r"[^A-Za-z0-9._-]+", "_", (title or "transcript")).strip("_")[:60] or "transcript"

    if format == "txt":
        body = export_txt(title, podcast, date, transcript, summary, takeaways)
        return Response(body.encode("utf-8"), media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{slug}.txt"'})
    if format == "md":
        body = export_markdown(title, podcast, date, transcript, summary, takeaways, chapters)
        return Response(body.encode("utf-8"), media_type="text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="{slug}.md"'})
    if format == "ai":
        body = export_ai_copy(title, podcast, date, transcript, summary, takeaways)
        return Response(body.encode("utf-8"), media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{slug}_ai.txt"'})

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


@app.post("/api/episodes/{episode_id}/regenerate-summary")
async def regenerate_summary(episode_id: int, background_tasks: BackgroundTasks):
    """(Re)generate the German summary/takeaways/chapters for a done episode.

    Works whether a summary already exists (overwrites) or not (creates it) —
    so summaries can be produced retroactively per episode on demand.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content FROM transcripts WHERE episode_id=?", (episode_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return {"error": "no transcript"}
    background_tasks.add_task(_do_regenerate_summary, episode_id, row[0])
    return {"ok": True, "message": "Zusammenfassung wird neu erstellt…"}


async def _do_regenerate_summary(episode_id: int, content: str):
    from .transcriber import enrich_text
    try:
        data = await enrich_text(content)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO summaries (episode_id, summary, takeaways_json, chapters_json, summary_lang)
                   VALUES (?, ?, ?, ?, 'de')
                   ON CONFLICT(episode_id) DO UPDATE SET
                       summary=excluded.summary, takeaways_json=excluded.takeaways_json,
                       chapters_json=excluded.chapters_json, summary_lang='de'""",
                (episode_id, data.get("summary", ""),
                 json.dumps(data.get("takeaways", [])),
                 json.dumps(data.get("chapters", []))),
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Summary regeneration failed for episode {episode_id}: {e}")


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
                    snippet(transcripts_fts, 1, '<mark>', '</mark>', '…', 40) AS snippet,
                    (SELECT GROUP_CONCAT(t.label || '|' || t.id, ',')
                     FROM episode_tags et JOIN tags t ON t.id=et.tag_id
                     WHERE et.episode_id=e.id) AS tags_csv
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


@app.delete("/api/queue")
async def clear_queue():
    """Clear the queue: reset waiting ('queued') and failed ('error') jobs back
    to 'pending' (idle, not retried) and wipe their error messages. Running jobs
    (downloading/transcribing) are left untouched."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE episodes SET status='pending', error_msg=NULL, processing_started_at=NULL "
            "WHERE status IN ('queued','error')"
        )
        await db.commit()
        cleared = cur.rowcount
    return {"ok": True, "cleared": cleared}


@app.post("/api/episodes/{episode_id}/cancel")
async def cancel_episode(episode_id: int):
    """Cancel a single queued or errored episode (reset to pending)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE episodes SET status='pending', error_msg=NULL "
            "WHERE id=? AND status IN ('queued','error')",
            (episode_id,),
        )
        await db.commit()
    return {"ok": True}


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
    # Never leak secrets — only signal whether each one is set
    out["gemini_api_key_set"] = bool(out.pop("gemini_api_key", "") or os.getenv("GEMINI_API_KEY", ""))
    out["newsletter_imap_password_set"] = bool(out.pop("newsletter_imap_password", ""))
    out["smtp_password_set"] = bool(out.pop("smtp_password", ""))
    # Access control: expose status flags, never the hashes/secret
    out["owner_configured"] = bool(out.pop("owner_password_hash", ""))
    out["guest_password_set"] = bool(out.pop("guest_password_hash", ""))
    out.pop("session_secret", None)
    out["guest_rag_enabled"] = out.get("guest_rag_enabled", "0") == "1"
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
    if data.digest_model is not None:
        await set_setting("digest_model", data.digest_model)
    if data.public_base_url is not None:
        await set_setting("public_base_url", data.public_base_url.strip())
    # Newsletter inbox
    if data.newsletter_enabled is not None:
        await set_setting("newsletter_enabled", "1" if data.newsletter_enabled else "0")
    if data.newsletter_imap_host is not None:
        await set_setting("newsletter_imap_host", data.newsletter_imap_host.strip())
    if data.newsletter_imap_port is not None:
        await set_setting("newsletter_imap_port", str(data.newsletter_imap_port))
    if data.newsletter_imap_user is not None:
        await set_setting("newsletter_imap_user", data.newsletter_imap_user.strip())
    if data.newsletter_imap_password:  # only overwrite when non-empty
        await set_setting("newsletter_imap_password", data.newsletter_imap_password)
    if data.newsletter_check_interval_hours is not None:
        await set_setting("newsletter_check_interval_hours",
                          str(data.newsletter_check_interval_hours))
    # Digest email (SMTP)
    if data.digest_email_enabled is not None:
        await set_setting("digest_email_enabled", "1" if data.digest_email_enabled else "0")
    if data.digest_email_to is not None:
        await set_setting("digest_email_to", data.digest_email_to.strip())
    if data.smtp_host is not None:
        await set_setting("smtp_host", data.smtp_host.strip())
    if data.smtp_port is not None:
        await set_setting("smtp_port", str(data.smtp_port))
    if data.smtp_user is not None:
        await set_setting("smtp_user", data.smtp_user.strip())
    if data.smtp_password:  # only overwrite when non-empty
        await set_setting("smtp_password", data.smtp_password)
    # Access control (owner/guest)
    owner_just_set = False
    if data.owner_password is not None:
        # Empty string disables auth → back to fully-open single-user mode.
        await set_setting(
            "owner_password_hash",
            auth.hash_password(data.owner_password) if data.owner_password else "",
        )
        owner_just_set = bool(data.owner_password)
        auth.invalidate()
    if data.guest_password is not None:
        await set_setting(
            "guest_password_hash",
            auth.hash_password(data.guest_password) if data.guest_password else "",
        )
        auth.invalidate()
    if data.guest_rag_enabled is not None:
        await set_setting("guest_rag_enabled", "1" if data.guest_rag_enabled else "0")
        auth.invalidate()
    # Auto digest
    if data.auto_digest_enabled is not None:
        await set_setting("auto_digest_enabled", "1" if data.auto_digest_enabled else "0")
    if data.auto_digest_dow is not None:
        await set_setting("auto_digest_dow", str(max(0, min(6, data.auto_digest_dow))))
    if data.auto_digest_hour is not None:
        await set_setting("auto_digest_hour", str(max(0, min(23, data.auto_digest_hour))))
    await _apply_runtime_config()
    # When enabling protection from open mode, keep THIS browser signed in as owner
    # so the user doesn't lock themselves out (they had no cookie before).
    if owner_just_set:
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            auth.COOKIE_NAME,
            auth.make_cookie("owner", await auth.session_secret()),
            max_age=auth.COOKIE_TTL, httponly=True, samesite="lax",
        )
        return resp
    return {"ok": True}


# ── Auth (owner / guest) ────────────────────────────────────────────────────────

@app.post("/api/login")
async def api_login(data: LoginRequest):
    role = await auth.login_role(data.password)
    if not role:
        raise HTTPException(401, "Falsches Passwort")
    resp = JSONResponse({"role": role})
    resp.set_cookie(
        auth.COOKIE_NAME,
        auth.make_cookie(role, await auth.session_secret()),
        max_age=auth.COOKIE_TTL, httponly=True, samesite="lax",
    )
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    return {
        "role": getattr(request.state, "role", "owner"),
        "owner_configured": await auth.owner_configured(),
        "guest_rag_enabled": await auth.guest_rag_enabled(),
    }


# ── Recent takeaways (start-screen ticker) ─────────────────────────────────────

@app.get("/api/recent-takeaways")
async def recent_takeaways(limit: int = 12):
    """Newest processed episodes with their first key takeaway (for the ticker)."""
    limit = max(1, min(limit, 30))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.id AS episode_id, e.title, p.id AS podcast_id,
                   p.title AS podcast_title, p.artwork_url, p.feed_type,
                   s.takeaways_json
            FROM episodes e
            JOIN podcasts p ON p.id = e.podcast_id
            JOIN summaries s ON s.episode_id = e.id
            WHERE e.status='done'
              AND s.takeaways_json IS NOT NULL
              AND s.takeaways_json NOT IN ('', '[]')
            ORDER BY datetime(e.pub_date) DESC, e.id DESC
            LIMIT ?
        """, (limit,)) as cur:
            rows = await cur.fetchall()
    out = []
    for r in rows:
        try:
            takes = json.loads(r["takeaways_json"] or "[]")
        except Exception:
            takes = []
        if not takes:
            continue
        out.append({
            "episode_id": r["episode_id"],
            "title": r["title"],
            "podcast_id": r["podcast_id"],
            "podcast_title": r["podcast_title"],
            "artwork_url": r["artwork_url"],
            "feed_type": r["feed_type"],
            "takeaway": takes[0],
        })
    return out


# ── Scheduler ──────────────────────────────────────────────────────────────────

@app.post("/api/scheduler/trigger")
async def trigger_check(background_tasks: BackgroundTasks):
    from .scheduler import check_all_feeds
    background_tasks.add_task(check_all_feeds)
    return {"ok": True, "message": "Feed-Check gestartet"}


# ── Newsletter inbox (IMAP) ────────────────────────────────────────────────────

@app.post("/api/newsletter/test")
async def newsletter_test(data: NewsletterTest):
    from . import newsletter
    host = (data.host or await get_setting("newsletter_imap_host") or "imap.hosting.de").strip()
    port = data.port or int((await get_setting("newsletter_imap_port")) or "993")
    user = (data.user or await get_setting("newsletter_imap_user") or "").strip()
    password = data.password or await get_setting("newsletter_imap_password")
    if not user or not password:
        raise HTTPException(400, "Benutzer und Passwort erforderlich.")
    try:
        return await newsletter.test_connection(host, port, user, password)
    except Exception as e:
        raise HTTPException(400, f"Verbindung fehlgeschlagen: {e}")


@app.post("/api/newsletter/check")
async def newsletter_check():
    from . import newsletter
    if (await get_setting("newsletter_enabled")) != "1":
        raise HTTPException(400, "Newsletter-Postfach ist deaktiviert.")
    try:
        n = await newsletter.check_inbox()
    except Exception as e:
        raise HTTPException(400, f"Postfach-Abruf fehlgeschlagen: {e}")
    return {"ok": True, "new": n}


# ── Digest email (SMTP) ─────────────────────────────────────────────────────────

@app.post("/api/email/test")
async def email_test(data: SmtpTest):
    from . import mailer
    host = (data.host or await get_setting("smtp_host") or "smtp.hosting.de").strip()
    port = data.port or int((await get_setting("smtp_port")) or "465")
    user = (data.user or await get_setting("smtp_user") or "").strip()
    password = data.password or await get_setting("smtp_password")
    if not user or not password:
        raise HTTPException(400, "Benutzer und Passwort erforderlich.")
    try:
        return await mailer.test_connection(host, port, user, password)
    except Exception as e:
        raise HTTPException(400, f"Verbindung fehlgeschlagen: {e}")


@app.post("/api/digests/{digest_id}/email")
async def email_digest(digest_id: int):
    from . import mailer
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT title, content_html, tldr_md, status FROM digests WHERE id=?",
            (digest_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Digest nicht gefunden")
    if row["status"] != "done":
        raise HTTPException(400, "Digest ist noch nicht fertig.")
    to_addr = await get_setting("digest_email_to")
    if not to_addr:
        raise HTTPException(400, "Keine Empfänger-Adresse konfiguriert.")
    try:
        await mailer.send_email(to_addr, row["title"] or "PodScribe Redaktion",
                                row["content_html"], row["tldr_md"] or "")
    except Exception as e:
        raise HTTPException(400, f"Versand fehlgeschlagen: {e}")
    return {"ok": True}


# ── Topic radar ─────────────────────────────────────────────────────────────────

@app.get("/api/radar")
async def topic_radar(days: int = 7):
    """Tag frequency in the current window vs. the previous one (cross-source:
    podcasts, news, newsletters). Returns trending/top/quiet topics."""
    days = max(1, min(days, 90))
    now = datetime.now()
    cur_from = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    prev_from = (now - timedelta(days=2 * days)).strftime("%Y-%m-%d %H:%M:%S")

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
            HAVING count_now > 0 OR count_prev > 0
            ORDER BY count_now DESC, t.label ASC
        """, (cur_from, prev_from, cur_from, prev_from)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    for r in rows:
        r["delta"] = (r["count_now"] or 0) - (r["count_prev"] or 0)
    trending = sorted([r for r in rows if r["delta"] > 0],
                      key=lambda r: (r["delta"], r["count_now"]), reverse=True)[:15]
    top = [r for r in rows if r["count_now"] > 0][:15]
    return {"days": days, "trending": trending, "top": top}


# ── Semantic search (RAG) ───────────────────────────────────────────────────────

@app.post("/api/ask")
async def ask_library(data: AskRequest):
    from . import rag
    q = (data.question or "").strip()
    if not q:
        raise HTTPException(400, "Frage fehlt.")
    try:
        return await rag.answer(q)
    except Exception as e:
        raise HTTPException(400, f"Anfrage fehlgeschlagen: {e}")


@app.post("/api/chat")
async def chat_library(data: ChatRequest):
    from . import rag
    messages = [{"role": m.role, "content": m.content} for m in data.messages if m.content.strip()]
    if not messages:
        raise HTTPException(400, "Keine Nachricht.")
    try:
        return await rag.chat(messages)
    except Exception as e:
        raise HTTPException(400, f"Anfrage fehlgeschlagen: {e}")


@app.post("/api/chat/export")
async def export_chat(data: ChatExportRequest):
    """Export a client-side chat conversation (the chat is stateless) as a
    Markdown download, sources included as deep links."""
    msgs = [{"role": m.role, "content": m.content, "sources": m.sources}
            for m in data.messages if (m.content or "").strip()]
    if not msgs:
        raise HTTPException(400, "Kein Gesprächsverlauf.")
    body = export_chat_markdown(msgs)
    return Response(
        body.encode("utf-8"), media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="podscribe-chat.md"'},
    )


@app.get("/api/episodes/{episode_id}/related")
async def episode_related(episode_id: int, limit: int = 6):
    from . import rag
    try:
        return await rag.related(episode_id, max(1, min(limit, 12)))
    except Exception as e:
        logger.warning(f"related({episode_id}) failed: {e}")
        return []


@app.get("/api/rag/stats")
async def rag_stats():
    from . import rag
    return await rag.stats()


@app.post("/api/rag/reindex", status_code=202)
async def rag_reindex(background_tasks: BackgroundTasks):
    from . import rag
    background_tasks.add_task(rag.reindex_all)
    return {"status": "started"}


@app.post("/api/podcasts/{podcast_id}/check")
async def check_podcast(podcast_id: int, background_tasks: BackgroundTasks):
    """Re-scan a single feed for new episodes — runs regardless of the podcast's
    auto_transcribe setting (unlike the scheduler), so the manual 'check for new
    episodes' button always lists newly published episodes as 'pending'."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM podcasts WHERE id=?", (podcast_id,)) as cur:
            p = await cur.fetchone()
    if not p:
        raise HTTPException(404, "Podcast nicht gefunden")

    # Newsletter pseudo-feeds have no RSS to parse — poll the shared inbox.
    if p["feed_type"] == "newsletter":
        from . import newsletter
        try:
            n = await newsletter.check_inbox()
        except Exception as e:
            raise HTTPException(400, f"Postfach-Abruf fehlgeschlagen: {e}")
        return {"ok": True, "new": n}

    try:
        feed_data = await parse_rss_feed(p["rss_url"])
    except Exception as e:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE podcasts SET last_fetch_error=?,
                   consecutive_fetch_errors=COALESCE(consecutive_fetch_errors,0)+1
                   WHERE id=?""",
                (str(e)[:300], podcast_id),
            )
            await db.commit()
        raise HTTPException(400, f"Feed konnte nicht gelesen werden: {e}")

    async with aiosqlite.connect(DB_PATH) as db:
        inserted = await insert_new_episodes(
            db, podcast_id, feed_data["episodes"],
            feed_type=p["feed_type"] or "podcast",
            full_text_extraction=bool(p["full_text_extraction"]),
            auto_transcribe=bool(p["auto_transcribe"]),
            limit=p["max_episodes"] or 0,
        )
        await db.execute(
            """UPDATE podcasts SET last_checked=CURRENT_TIMESTAMP,
               consecutive_fetch_errors=0, last_fetch_error=NULL WHERE id=?""",
            (podcast_id,),
        )
        await db.commit()

    if inserted and (p["auto_transcribe"]
                     or (p["feed_type"] == "newsfeed" and p["full_text_extraction"])):
        background_tasks.add_task(process_queued)

    return {"ok": True, "new": inserted}


# ── Digests ────────────────────────────────────────────────────────────────────

@app.get("/api/digests")
async def list_digests():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, subtitle, mode, format, status, created_at FROM digests ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def _select_episode_ids(db, *, date_window_days=None, date_from=None, date_to=None,
                              podcast_ids=None, tag_ids=None, match="any"):
    """Resolve a recipe/selection into ordered episode rows (status='done')."""
    if date_window_days:
        cutoff = (datetime.now() - timedelta(days=int(date_window_days))).strftime("%Y-%m-%d %H:%M:%S")
        date_from = date_from or cutoff
    where = ["e.status='done'"]
    params: list = []
    if date_from:
        where.append("e.pub_date >= ?"); params.append(date_from)
    if date_to:
        where.append("e.pub_date <= ?"); params.append(date_to)
    if podcast_ids:
        where.append(f"e.podcast_id IN ({','.join('?'*len(podcast_ids))})"); params += list(podcast_ids)
    join = ""
    having = ""
    if tag_ids:
        join = "JOIN episode_tags et ON et.episode_id = e.id"
        where.append(f"et.tag_id IN ({','.join('?'*len(tag_ids))})"); params += list(tag_ids)
        if match == "all":
            having = "HAVING COUNT(DISTINCT et.tag_id) = ?"
    sql = f"""
        SELECT e.id, e.title, e.pub_date, p.title AS podcast_title
        FROM episodes e
        LEFT JOIN podcasts p ON p.id = e.podcast_id
        {join}
        WHERE {' AND '.join(where)}
        GROUP BY e.id
        {having}
        ORDER BY e.pub_date IS NULL, e.pub_date DESC, e.created_at DESC
    """
    if having:
        params.append(len(tag_ids))
    db.row_factory = aiosqlite.Row
    async with db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


@app.post("/api/issues/select")
async def issues_select(data: IssueSelect):
    async with aiosqlite.connect(DB_PATH) as db:
        eps = await _select_episode_ids(
            db, date_window_days=data.date_window_days, date_from=data.date_from,
            date_to=data.date_to, podcast_ids=data.podcast_ids, tag_ids=data.tag_ids,
            match=data.match)
        ids = [e["id"] for e in eps]
        tag_breakdown = []
        if ids:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"""
                SELECT t.id, t.label, COUNT(*) AS count
                FROM episode_tags et JOIN tags t ON t.id = et.tag_id
                WHERE et.episode_id IN ({','.join('?'*len(ids))})
                GROUP BY t.id ORDER BY count DESC LIMIT 30
            """, ids) as cur:
                tag_breakdown = [dict(r) for r in await cur.fetchall()]
    return {"count": len(eps), "episodes": eps, "tag_breakdown": tag_breakdown}


@app.get("/api/issue-options")
async def issue_options():
    from .transcriber import FORMATS
    return {
        "length": [{"value": k, "label": v["label"], "words": v["words"],
                    "sections": v["sections"]} for k, v in _LENGTH_MAP.items()],
        "style": [{"value": k, "label": v[0]} for k, v in _STYLE_MAP.items()],
        "formats": [
            {"value": k, "label": v["label"], "uses_sliders": v["uses_sliders"]}
            for k, v in FORMATS.items()
        ],
    }


@app.post("/api/digests", status_code=201)
async def create_digest(data: DigestRequest, background_tasks: BackgroundTasks):
    # Resolve episode set: explicit ids, or a recipe selection
    episode_ids = list(data.episode_ids)
    recipe = data.recipe
    if not episode_ids and recipe:
        async with aiosqlite.connect(DB_PATH) as db:
            eps = await _select_episode_ids(
                db, date_window_days=recipe.get("date_window_days"),
                date_from=recipe.get("date_from"), date_to=recipe.get("date_to"),
                podcast_ids=recipe.get("podcast_ids"), tag_ids=recipe.get("tag_ids"),
                match=recipe.get("match", "any"))
            episode_ids = [e["id"] for e in eps]

    # Free article with AI selection: the RAG retrieval that picks library
    # episodes runs inside _build_issue (background task), NOT here — otherwise
    # the Gemini embedding call blocks the HTTP response and the UI hangs on
    # "Generiere…". So episode_ids may legitimately stay empty at this point.
    ai_select = data.format == "artikel" and data.selection_mode == "ai" and data.prompt.strip()

    # Every format needs source episodes, except a free article (the article can
    # come purely from the prompt, or from episodes the AI picks in the background).
    if not episode_ids and not ai_select and not (data.format == "artikel" and data.prompt.strip()):
        raise HTTPException(400, "Keine passenden Folgen gefunden")

    # Persist the article parameters in recipe_json so a retry can rebuild fully.
    recipe_json = json.dumps({
        **(recipe or {}),
        "prompt": data.prompt, "selection_mode": data.selection_mode,
        "focus": data.focus, "custom_style": data.custom_style,
    })
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO digests (title, mode, format, length, style, episode_ids_json, recipe_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'generating')""",
            (data.title, data.mode, data.format, data.length, data.style,
             json.dumps(episode_ids), recipe_json),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            digest_id = (await cur.fetchone())[0]

    background_tasks.add_task(_build_issue, digest_id, episode_ids,
                              data.format, data.length, data.style, data.title,
                              data.model, data.custom_style, data.focus, data.prompt,
                              data.selection_mode)
    return {"id": digest_id, "status": "generating"}


def _sections_to_md(result: dict) -> str:
    parts = []
    for s in result.get("sections", []):
        heading = s.get("heading", "")
        body = s.get("body_md", "")
        if heading and not heading.lstrip().startswith("#"):
            heading = f"## {heading}"
        if heading:
            parts.append(heading)
        if body:
            parts.append(body)
    return "\n\n".join(parts).strip()


async def _build_issue(digest_id: int, episode_ids: list, fmt: str, length: int,
                       style: int, title: str, model: str = "",
                       custom_style: str = "", focus: str = "", prompt: str = "",
                       selection_mode: str = "manual"):
    try:
        episode_ids = list(episode_ids)
        # Free-article AI episode selection runs HERE (background task), not in the
        # request handler — the Gemini embedding call would otherwise block the
        # HTTP response and leave the UI stuck on "Generiere…".
        if selection_mode == "ai" and prompt.strip() and not episode_ids:
            from . import rag
            try:
                # Timeout-guarded: the Gemini embedding call has no timeout of
                # its own, so an unguarded hang here would never resolve the digest.
                _, sources, err = await asyncio.wait_for(rag._retrieve(prompt, k=12), timeout=90)
                if not err and sources:
                    seen = set()
                    for s in sources:
                        if s["episode_id"] not in seen:
                            seen.add(s["episode_id"])
                            episode_ids.append(s["episode_id"])
                    episode_ids = episode_ids[:8]
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE digests SET episode_ids_json=? WHERE id=?",
                            (json.dumps(episode_ids), digest_id))
                        await db.commit()
            except Exception as e:
                logger.warning(f"AI episode selection failed: {e}")

        episode_data = []
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            for ep_id in episode_ids:
                async with db.execute("""
                    SELECT e.id, e.podcast_id, e.title, e.pub_date, p.title AS podcast_title,
                           t.content AS transcript, s.summary, s.takeaways_json
                    FROM episodes e
                    LEFT JOIN podcasts p ON p.id=e.podcast_id
                    LEFT JOIN transcripts t ON t.episode_id=e.id
                    LEFT JOIN summaries s ON s.episode_id=e.id
                    WHERE e.id=?
                """, (ep_id,)) as cur:
                    row = await cur.fetchone()
                    if row:
                        episode_data.append(dict(row))

        # Auto-title if empty. Wrapped in a timeout so a stalled Gemini call can
        # never leave the digest stuck on 'generating' (frontend polls forever).
        if not title.strip():
            from .transcriber import generate_title
            title = await asyncio.wait_for(generate_title(episode_data, fmt), timeout=120)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE digests SET title=? WHERE id=?", (title, digest_id))
                await db.commit()

        result = await asyncio.wait_for(
            generate_issue(episode_data, fmt=fmt, length=length,
                           style=style, title=title,
                           model=model, custom_style=custom_style,
                           focus=focus, prompt=prompt),
            timeout=240)
        content_md = _sections_to_md(result)
        content_html = markdown2.markdown(
            content_md, extras=["fenced-code-blocks", "tables", "header-ids"])

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE digests SET content_html=?, content_md=?, sections_json=?,
                   tldr_md=?, subtitle=?, status='done' WHERE id=?""",
                (content_html, content_md, json.dumps(result.get("sections", [])),
                 result.get("tldr_md", ""), result.get("subtitle", ""), digest_id),
            )
            await db.commit()

        # Email delivery (no-op unless enabled in settings)
        from .mailer import maybe_email_digest
        await maybe_email_digest(digest_id, title, content_html,
                                 result.get("tldr_md", ""))
    except (asyncio.TimeoutError, TimeoutError):
        logger.error(f"Issue {digest_id} timed out")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE digests SET status='error', error_msg=? WHERE id=?",
                ("Zeitüberschreitung bei der KI-Generierung. Bitte erneut versuchen "
                 "(ggf. mit kürzerer Länge oder schnellerem Modell).", digest_id))
            await db.commit()
    except Exception as e:
        logger.error(f"Issue {digest_id} failed: {e}", exc_info=True)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE digests SET status='error', error_msg=? WHERE id=?",
                (str(e)[:500] or e.__class__.__name__, digest_id))
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


@app.post("/api/digests/{digest_id}/retry")
async def retry_digest(digest_id: int, background_tasks: BackgroundTasks):
    """Re-run a failed issue with its original parameters (stored in recipe_json)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM digests WHERE id=?", (digest_id,)) as cur:
            d = await cur.fetchone()
        if not d:
            raise HTTPException(404)
        if d["status"] != "error":
            raise HTTPException(400, "Nur fehlgeschlagene Ausgaben können wiederholt werden")
        await db.execute(
            "UPDATE digests SET status='generating', error_msg=NULL WHERE id=?",
            (digest_id,))
        await db.commit()

    recipe = json.loads(d["recipe_json"] or "{}")
    episode_ids = json.loads(d["episode_ids_json"] or "[]")
    background_tasks.add_task(
        _build_issue, digest_id, episode_ids,
        d["format"], d["length"], d["style"], d["title"],
        "", recipe.get("custom_style", ""), recipe.get("focus", ""),
        recipe.get("prompt", ""), recipe.get("selection_mode", "manual"))
    return {"id": digest_id, "status": "generating"}


@app.delete("/api/digests/{digest_id}")
async def delete_digest(digest_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM digests WHERE id=?", (digest_id,))
        await db.commit()
    return {"ok": True}


# ── Tags ─────────────────────────────────────────────────────────────────────

@app.get("/api/tags")
async def list_tags(q: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT id, label, kind, episode_count FROM tags WHERE episode_count > 0"
        params: list = []
        if q:
            sql += " AND label LIKE ?"; params.append(f"%{q}%")
        sql += " ORDER BY episode_count DESC, label ASC"
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


@app.put("/api/tags/{tag_id}")
async def rename_tag_route(tag_id: int, data: TagRename):
    await tagging.rename_tag(tag_id, data.label.strip())
    return {"ok": True}


@app.get("/api/episodes/{episode_id}/tags")
async def get_episode_tags(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT t.id, t.label, t.kind, et.source
            FROM episode_tags et JOIN tags t ON t.id=et.tag_id
            WHERE et.episode_id=?
            ORDER BY t.label
        """, (episode_id,)) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.put("/api/episodes/{episode_id}/tags")
async def update_episode_tags(episode_id: int, data: EpisodeTagsUpdate):
    for label in data.add:
        await tagging.add_manual_tag(episode_id, label)
    for tag_id in data.remove:
        await tagging.remove_tag(episode_id, tag_id)
    return {"ok": True}


@app.get("/api/tags/{tag_id}/episodes")
async def get_tag_episodes(tag_id: int, page: int = 1, limit: int = 30):
    offset = (page - 1) * limit
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.id, e.title, e.pub_date, e.status, e.read_at, t.word_count,
                   p.title AS podcast_title, p.id AS podcast_id, p.artwork_url,
                   s.summary
            FROM episode_tags et
            JOIN episodes e ON e.id = et.episode_id
            LEFT JOIN podcasts p ON p.id = e.podcast_id
            LEFT JOIN transcripts t ON t.episode_id = e.id
            LEFT JOIN summaries s ON s.episode_id = e.id
            WHERE et.tag_id = ?
            ORDER BY e.pub_date IS NULL, e.pub_date DESC
            LIMIT ? OFFSET ?
        """, (tag_id, limit, offset)) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Topic explorer ──────────────────────────────────────────────────────────────

@app.get("/api/topics/{tag_id}")
async def topic_detail(tag_id: int):
    """A tag's episodes (chronological) + the latest cross-episode summary, if any."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tags WHERE id=?", (tag_id,)) as cur:
            tag = await cur.fetchone()
        if not tag:
            raise HTTPException(404, "Tag nicht gefunden")
        async with db.execute("""
            SELECT e.id, e.title, e.pub_date, e.read_at,
                   p.id AS podcast_id, p.title AS podcast_title, p.artwork_url, p.feed_type,
                   s.summary
            FROM episode_tags et
            JOIN episodes e ON e.id = et.episode_id
            LEFT JOIN podcasts p ON p.id = e.podcast_id
            LEFT JOIN summaries s ON s.episode_id = e.id
            WHERE et.tag_id = ? AND e.status='done'
            ORDER BY e.pub_date IS NULL, e.pub_date ASC
        """, (tag_id,)) as cur:
            episodes = [dict(r) for r in await cur.fetchall()]
        async with db.execute("""
            SELECT id FROM digests
            WHERE mode='topic' AND recipe_json LIKE ? AND status='done'
            ORDER BY created_at DESC LIMIT 1
        """, (f'%"tag_id": {tag_id}%',)) as cur:
            last = await cur.fetchone()
    return {
        "tag": dict(tag),
        "episodes": episodes,
        "last_summary_digest_id": last["id"] if last else None,
    }


@app.post("/api/topics/{tag_id}/summary", status_code=201)
async def topic_summary(tag_id: int, background_tasks: BackgroundTasks):
    """Build a cross-episode 'Dossier' for a tag, persisted as a digest (owner-only)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tags WHERE id=?", (tag_id,)) as cur:
            tag = await cur.fetchone()
        if not tag:
            raise HTTPException(404, "Tag nicht gefunden")
        rows = await _select_episode_ids(db, tag_ids=[tag_id])
        episode_ids = [r["id"] for r in rows]
        if not episode_ids:
            raise HTTPException(400, "Keine Folgen für dieses Thema.")
        title = f"Dossier: {tag['label']}"
        await db.execute(
            """INSERT INTO digests (title, mode, format, length, style, episode_ids_json, recipe_json, status)
               VALUES (?, 'topic', 'daily_briefing', 3, 3, ?, ?, 'generating')""",
            (title, json.dumps(episode_ids), json.dumps({"tag_id": tag_id})),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            digest_id = (await cur.fetchone())[0]
    background_tasks.add_task(_build_issue, digest_id, episode_ids,
                              "daily_briefing", 3, 3, title, "", "", "")
    return {"id": digest_id, "status": "generating"}


@app.post("/api/tags/backfill", status_code=202)
async def backfill_tags(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_backfill)
    return {"status": "started"}


async def _run_backfill():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.id, s.summary, s.takeaways_json, s.chapters_json
            FROM episodes e JOIN summaries s ON s.episode_id = e.id
            WHERE e.id NOT IN (SELECT DISTINCT episode_id FROM episode_tags)
        """) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    count = 0
    for r in rows:
        try:
            tk = json.loads(r.get("takeaways_json") or "[]")
            ch = json.loads(r.get("chapters_json") or "[]")
            raw = await extract_tags(r.get("summary", ""), tk, ch)
            if raw:
                await tagging.upsert_tags(r["id"], raw)
                count += 1
        except Exception as e:
            logger.warning(f"Backfill tag for episode {r['id']} failed: {e}")
    try:
        from .notifier import send_notification
        await send_notification("🏷️ Tagging fertig", f"{count} Folgen verschlagwortet.")
    except Exception:
        pass
    logger.info(f"Tag backfill done: {count} episodes")


@app.post("/api/summaries/backfill", status_code=202)
async def backfill_summaries(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_summary_backfill)
    return {"status": "started"}


async def _run_summary_backfill():
    from .transcriber import enrich_text, translate_summary
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Episodes with transcript but no summary
        async with db.execute("""
            SELECT e.id, t.content
            FROM episodes e
            JOIN transcripts t ON t.episode_id = e.id
            WHERE e.id NOT IN (SELECT episode_id FROM summaries WHERE summary != '')
        """) as cur:
            missing = [dict(r) for r in await cur.fetchall()]
        # Episodes with non-German summary
        async with db.execute("""
            SELECT e.id, s.summary, s.takeaways_json
            FROM episodes e
            JOIN summaries s ON s.episode_id = e.id
            WHERE s.summary != '' AND (s.summary_lang IS NULL OR s.summary_lang != 'de')
        """) as cur:
            non_de = [dict(r) for r in await cur.fetchall()]

    count = 0
    for r in missing:
        try:
            data = await enrich_text(r["content"])
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO summaries (episode_id, summary, takeaways_json, chapters_json, summary_lang)
                    VALUES (?, ?, ?, ?, 'de')
                    ON CONFLICT(episode_id) DO UPDATE SET
                        summary=excluded.summary, takeaways_json=excluded.takeaways_json,
                        chapters_json=excluded.chapters_json, summary_lang='de'
                """, (r["id"], data.get("summary", ""),
                      json.dumps(data.get("takeaways", [])),
                      json.dumps(data.get("chapters", []))))
                await db.commit()
            count += 1
        except Exception as e:
            logger.warning(f"Summary backfill for episode {r['id']} failed: {e}")

    for r in non_de:
        try:
            takeaways = json.loads(r.get("takeaways_json") or "[]")
            translated = await translate_summary(r["summary"], takeaways)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE summaries SET summary=?, takeaways_json=?, summary_lang='de'
                    WHERE episode_id=?
                """, (translated.get("summary", r["summary"]),
                      json.dumps(translated.get("takeaways", takeaways)),
                      r["id"]))
                await db.commit()
            count += 1
        except Exception as e:
            logger.warning(f"Summary translation for episode {r['id']} failed: {e}")

    try:
        from .notifier import send_notification
        await send_notification("🧾 Zusammenfassungen fertig", f"{count} Folgen verarbeitet.")
    except Exception:
        pass
    logger.info(f"Summary backfill done: {count} episodes")


@app.post("/api/issues/title")
async def generate_issue_title(data: DigestRequest):
    from .transcriber import generate_title, FORMATS
    episode_data = []
    if data.episode_ids:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            for ep_id in data.episode_ids:
                async with db.execute("""
                    SELECT e.id, e.title, e.pub_date, p.title AS podcast_title, s.summary
                    FROM episodes e
                    LEFT JOIN podcasts p ON p.id=e.podcast_id
                    LEFT JOIN summaries s ON s.episode_id=e.id
                    WHERE e.id=?
                """, (ep_id,)) as cur:
                    row = await cur.fetchone()
                    if row:
                        episode_data.append(dict(row))
    title = await generate_title(episode_data, data.format)
    return {"title": title}


# ── Recipes + scheduling ──────────────────────────────────────────────────────

@app.get("/api/recipes")
async def list_recipes():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT r.*, s.id AS schedule_id, s.cron_dow, s.cron_hour, s.enabled, s.last_run_at
            FROM issue_recipes r
            LEFT JOIN scheduled_issues s ON s.recipe_id = r.id
            ORDER BY r.created_at DESC
        """) as cur:
            return [dict(r) for r in await cur.fetchall()]


@app.post("/api/recipes", status_code=201)
async def create_recipe(data: RecipeCreate):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO issue_recipes
              (name, format, date_window_days, date_from, date_to,
               podcast_ids_json, tag_ids_json, match_mode, length, style,
               model, custom_style, focus)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data.name, data.format, data.date_window_days, data.date_from, data.date_to,
             json.dumps(data.podcast_ids), json.dumps(data.tag_ids), data.match,
             data.length, data.style, data.model, data.custom_style, data.focus))
        await db.commit()
        return {"id": cur.lastrowid}


@app.delete("/api/recipes/{recipe_id}")
async def delete_recipe(recipe_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM issue_recipes WHERE id=?", (recipe_id,))
        await db.commit()
    return {"ok": True}


@app.post("/api/recipes/{recipe_id}/run", status_code=201)
async def run_recipe_route(recipe_id: int, background_tasks: BackgroundTasks):
    digest_id = await run_recipe(recipe_id, background_tasks)
    if digest_id is None:
        raise HTTPException(404, "Template nicht gefunden")
    return {"id": digest_id, "status": "generating"}


async def run_recipe(recipe_id: int, background_tasks: BackgroundTasks = None):
    """Resolve a recipe to a new generating digest. Returns digest_id or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM issue_recipes WHERE id=?", (recipe_id,)) as cur:
            r = await cur.fetchone()
        if not r:
            return None
        r = dict(r)
        eps = await _select_episode_ids(
            db, date_window_days=r["date_window_days"], date_from=r["date_from"],
            date_to=r["date_to"], podcast_ids=json.loads(r["podcast_ids_json"] or "[]"),
            tag_ids=json.loads(r["tag_ids_json"] or "[]"), match=r["match_mode"])
        episode_ids = [e["id"] for e in eps]
        if not episode_ids:
            return None
        title = f"{r['name']} — {datetime.now():%d.%m.%Y}"
        cur = await db.execute(
            """INSERT INTO digests (title, mode, format, length, style, episode_ids_json, status)
               VALUES (?, 'recipe', ?, ?, ?, ?, 'generating')""",
            (title, r["format"], r["length"], r["style"], json.dumps(episode_ids)))
        await db.commit()
        digest_id = cur.lastrowid

    coro_args = (digest_id, episode_ids, r["format"], r["length"], r["style"], title,
                 r.get("model", ""), r.get("custom_style", ""), r.get("focus", ""))
    if background_tasks is not None:
        background_tasks.add_task(_build_issue, *coro_args)
    else:
        import asyncio
        asyncio.create_task(_build_issue(*coro_args))
    return digest_id


@app.post("/api/scheduled-issues")
async def upsert_schedule(data: ScheduleUpsert):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM scheduled_issues WHERE recipe_id=?",
                              (data.recipe_id,)) as cur:
            existing = await cur.fetchone()
        if existing:
            await db.execute(
                "UPDATE scheduled_issues SET cron_dow=?, cron_hour=?, enabled=? WHERE recipe_id=?",
                (data.cron_dow, data.cron_hour, int(data.enabled), data.recipe_id))
        else:
            await db.execute(
                "INSERT INTO scheduled_issues (recipe_id, cron_dow, cron_hour, enabled) VALUES (?,?,?,?)",
                (data.recipe_id, data.cron_dow, data.cron_hour, int(data.enabled)))
        await db.commit()
    return {"ok": True}


# ── Weekly overview / TOC ─────────────────────────────────────────────────────

@app.get("/api/overview/week")
async def overview_week(days: int = 7):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.id, e.title, e.pub_date, p.title AS podcast_title, p.id AS podcast_id,
                   s.summary, s.chapters_json
            FROM episodes e
            LEFT JOIN podcasts p ON p.id = e.podcast_id
            LEFT JOIN summaries s ON s.episode_id = e.id
            WHERE e.status='done' AND e.pub_date >= ?
            ORDER BY p.title, e.pub_date DESC
        """, (cutoff,)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        # tags per episode
        ep_ids = [r["id"] for r in rows]
        tags_by_ep = {}
        if ep_ids:
            async with db.execute(f"""
                SELECT et.episode_id, t.label FROM episode_tags et
                JOIN tags t ON t.id = et.tag_id
                WHERE et.episode_id IN ({','.join('?'*len(ep_ids))})
            """, ep_ids) as cur:
                for r in await cur.fetchall():
                    tags_by_ep.setdefault(r["episode_id"], []).append(r["label"])

    by_podcast = {}
    top_tags = {}
    for r in rows:
        r["tags"] = tags_by_ep.get(r["id"], [])
        for t in r["tags"]:
            top_tags[t] = top_tags.get(t, 0) + 1
        try:
            r["chapters"] = json.loads(r.get("chapters_json") or "[]")
        except Exception:
            r["chapters"] = []
        r.pop("chapters_json", None)
        by_podcast.setdefault(r["podcast_title"] or "—", []).append(r)
    return {
        "range_from": cutoff, "episode_count": len(rows),
        "by_podcast": [{"podcast_title": k, "episodes": v} for k, v in by_podcast.items()],
        "top_tags": sorted(({"label": k, "count": v} for k, v in top_tags.items()),
                           key=lambda x: -x["count"])[:20],
    }


# ── Sharing ───────────────────────────────────────────────────────────────────

@app.post("/api/digests/{digest_id}/share")
async def share_digest(digest_id: int):
    token = secrets.token_urlsafe(10)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM digests WHERE id=?", (digest_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(404)
        await db.execute("UPDATE digests SET share_token=? WHERE id=?", (token, digest_id))
        await db.commit()
    return {"token": token, "url": f"/s/{token}"}


@app.get("/s/{token}", response_class=HTMLResponse)
async def public_share(token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT title, subtitle, content_html FROM digests WHERE share_token=?", (token,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404)
    return HTMLResponse(_share_html(row["title"], row["subtitle"] or "", row["content_html"] or ""))


def _share_html(title: str, subtitle: str, body_html: str) -> str:
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;margin:0 auto;
       padding:2rem 1.2rem;line-height:1.7;color:#1a1a2e;background:#fff}}
  h1{{font-size:1.8rem;margin-bottom:.2rem}} .sub{{color:#666;margin-bottom:2rem}}
  h2{{margin-top:2rem;border-bottom:1px solid #eee;padding-bottom:.3rem}}
  blockquote{{border-left:3px solid #7c6ff7;margin:1rem 0;padding:.3rem 1rem;color:#444;font-style:italic}}
  code{{background:#f4f4f8;padding:.1rem .3rem;border-radius:3px}}
  footer{{margin-top:3rem;padding-top:1rem;border-top:1px solid #eee;color:#999;font-size:.8rem}}
</style></head><body>
<h1>{title}</h1><div class="sub">{subtitle}</div>
{body_html}
<footer>Erstellt mit PodScribe · © 2026 Sven Kompe</footer>
</body></html>"""
