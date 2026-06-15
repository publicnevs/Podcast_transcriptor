"""Transcription orchestration.

Two stages, deliberately decoupled:
  1. TRANSCRIPTION  (audio -> timestamped segments)  -- backend: gemini | whisper
  2. ENRICHMENT     (text  -> summary/takeaways/chapters)  -- always Gemini text (cheap)

This lets a local Whisper run handle the heavy audio work while Gemini adds
summaries/chapters from plain text for almost no cost.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Model names overridable via env
FLASH_MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
PRO_MODEL = os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
# Lite model: cheaper for enrichment + tagging; defaults to flash-lite (not Flash)
LITE_MODEL = os.getenv("GEMINI_LITE_MODEL", "gemini-2.5-flash-lite")
# Article/digest model default; overridable via DB setting or per-request
DIGEST_MODEL = os.getenv("GEMINI_DIGEST_MODEL", PRO_MODEL)
# Embedding model for semantic search (RAG)
EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
# Image generation model for AI cover art (on-demand, owner-only)
IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")


# ── Format registry ──────────────────────────────────────────────────────────
# Each format has: label (UI), default_model (pro/flash/lite), uses_sliders (bool)
FORMATS = {
    "daily_briefing":   {"label": "📅 Daily Briefing",       "default_model": "flash", "uses_sliders": False},
    "magazin":          {"label": "📰 Magazin",               "default_model": "pro",   "uses_sliders": True},
    "newsletter":       {"label": "✉️ Newsletter",            "default_model": "flash", "uses_sliders": True},
    "artikel":          {"label": "📝 Freier Artikel",        "default_model": "pro",   "uses_sliders": True},
    "summary_takeaways":{"label": "📋 Summary + Takeaways",  "default_model": "lite",  "uses_sliders": False},
    "teams_post":       {"label": "💬 Teams Post",            "default_model": "lite",  "uses_sliders": False},
}


# Runtime-overridable settings
_runtime = {
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
    "backend": os.getenv("TRANSCRIPTION_BACKEND", "gemini"),
    "whisper_model": os.getenv("WHISPER_MODEL", "base"),
    "digest_model": "",  # '', 'pro', 'flash', 'lite'; '' = env/DIGEST_MODEL default
}


def configure(gemini_api_key=None, backend=None, whisper_model=None, digest_model=None):
    if gemini_api_key is not None:
        _runtime["gemini_api_key"] = gemini_api_key
    if backend:
        _runtime["backend"] = backend
    if whisper_model:
        _runtime["whisper_model"] = whisper_model
    if digest_model is not None:
        _runtime["digest_model"] = digest_model


def _digest_model() -> str:
    """Resolve the global digest model setting."""
    choice = (_runtime.get("digest_model") or "").strip().lower()
    if choice == "flash":
        return FLASH_MODEL
    if choice == "lite":
        return LITE_MODEL
    if choice == "pro":
        return PRO_MODEL
    return DIGEST_MODEL


def _resolve_model(model_override: str, fmt: str) -> str:
    """Pick model: explicit override > format default > global setting."""
    if model_override == "pro":
        return PRO_MODEL
    if model_override == "flash":
        return FLASH_MODEL
    if model_override == "lite":
        return LITE_MODEL
    fmt_default = FORMATS.get(fmt, {}).get("default_model", "flash")
    if fmt_default == "pro":
        return _digest_model()  # respects global setting
    if fmt_default == "lite":
        return LITE_MODEL
    return FLASH_MODEL


def get_backend():
    return _runtime["backend"]


def _get_key():
    return _runtime["gemini_api_key"] or os.getenv("GEMINI_API_KEY", "")


def _configure_gemini():
    return bool(_get_key())


def _client():
    return genai.Client(api_key=_get_key())


# ── Prompts ──────────────────────────────────────────────────────────────────

GEMINI_FULL_PROMPT = """Analyze this podcast audio completely and return a structured JSON transcription.

Return ONLY valid JSON — no markdown fences, no prose:
{
  "language": "ISO code of the spoken language, e.g. 'de' or 'en'",
  "speakers": ["Host", "Guest"],
  "segments": [
    {"time": "00:00:00", "speaker": "Host", "text": "Exact spoken words in original language"}
  ],
  "summary": "5-sentence comprehensive summary in GERMAN (Deutsch)",
  "takeaways": ["First key insight in GERMAN", "Second key insight in GERMAN"],
  "chapters": [
    {"title": "Kapitel-Titel auf Deutsch", "start_time": "00:00:00", "summary": "Was hier besprochen wird"}
  ]
}

Rules:
- Transcribe ALL spoken content in the ORIGINAL language (no translation)
- summary, takeaways and chapter titles/summaries ALWAYS IN GERMAN
- Accurate HH:MM:SS timestamps at each speaker turn
- Identify speakers consistently (real names if mentioned, else 'Host'/'Guest')
- 5-10 meaningful chapters at real topic transitions
- 6-10 takeaways, each a clear 1-2 sentence insight"""

ENRICH_PROMPT = """You are given a podcast transcript. Produce structured metadata.

Return ONLY valid JSON — no markdown fences, no prose:
{{
  "language": "ISO code of the transcript language",
  "summary": "5-sentence comprehensive summary IN GERMAN (Deutsch)",
  "takeaways": ["6-10 clear insights IN GERMAN, each 1-2 sentences"],
  "chapters": [
    {{"title": "Kapitel-Titel auf Deutsch", "start_time": "HH:MM:SS", "summary": "Was hier besprochen wird"}}
  ]
}}

Use timestamps in the transcript to place chapters at real topic transitions.
summary, takeaways and chapter titles/summaries MUST BE IN GERMAN even if the transcript is in another language.

TRANSCRIPT:
{transcript}"""

DIGEST_PROMPT = """Du bist ein erfahrener Journalist und Chefredakteur. Erstelle einen ausführlichen, qualitativ hochwertigen journalistischen Artikel basierend auf den folgenden Podcast-Transkripten.

{mode_instruction}

ANFORDERUNGEN:
- Länge: 1200-2000 Wörter (Pflicht — nicht kürzer!)
- Stil: Journalistisch, analytisch, fließend — KEIN Stichpunkt-Stil im Haupttext
- Struktur: Packende Einleitung, Hauptteil mit ## Zwischenüberschriften, Fazit
- Wörtliche Zitate aus den Transkripten verwenden (in „Anführungszeichen" mit Sprecher-Name)
- Ton: Sachlich-interessiert, journalistisch neutral
- Sprache: Deutsch

ARTIKEL-TITEL: {title}

Gib AUSSCHLIESSLICH JSON zurück:
{{
  "title": "{title}",
  "subtitle": "Treffender journalistischer Untertitel",
  "content_md": "Der vollständige Artikel in Markdown mit ## Zwischenüberschriften",
  "reading_time_min": <Zahl>
}}

TRANSKRIPTE:
{episodes_text}"""


TAG_EXTRACT_PROMPT = """Du bist ein Wissens-Kurator. Extrahiere aus dieser Podcast-Zusammenfassung 4–8 normalisierte Themen-Tags.

Regeln:
- label: kanonische Schreibweise (Eigennamen korrekt, Singular bevorzugt)
- kind: eines von topic | tool | person | company | product
- KEINE zu allgemeinen Begriffe wie "KI", "Technologie", "Podcast"

Gib AUSSCHLIESSLICH JSON zurück:
{{"tags": [{{"label": "Prompt Engineering", "kind": "topic"}}, {{"label": "GitHub Copilot", "kind": "tool"}}]}}

ZUSAMMENFASSUNG:
{summary}

TAKEAWAYS:
{takeaways}

KAPITEL:
{chapters}"""

# Length slider 1-5 → target words + section count (used by magazin/newsletter)
_LENGTH_MAP = {
    1: {"label": "Kompakt",     "words": 700,  "sections": 3, "tokens": 8192},
    2: {"label": "Knapp",       "words": 1200, "sections": 3, "tokens": 12000},
    3: {"label": "Standard",    "words": 1800, "sections": 4, "tokens": 16000},
    4: {"label": "Ausführlich", "words": 2800, "sections": 5, "tokens": 24000},
    5: {"label": "Magazin",     "words": 4000, "sections": 6, "tokens": 32000},
}

# Hard cap (milliseconds) on a single issue-generation request so a stalled
# Gemini call can never leave a digest stuck on status='generating' forever.
# Slightly below the asyncio.wait_for guard in main._build_issue so the clean
# SDK error fires first.
_ISSUE_TIMEOUT_MS = 210_000

_STYLE_MAP = {
    1: ("Technisch", "Fachlich präzise. Verwende korrekte Fachbegriffe ohne Vereinfachung, "
        "nenne Tools, Modelle und Versionen exakt, gib konkrete Schritte/Konfigurationen wieder. "
        "Zielgruppe: Profis."),
    2: ("Analytisch", "Sachlich-analytisch. Einordnung, Pro und Contra, Zahlen und Belege betont, "
        "ausgewogene Bewertung. Zielgruppe: Entscheider und Fachleute."),
    3: ("Journalistisch", "Analytisch-journalistischer Stil. Kontext und Einordnung, ausgewogen, "
        "mit wörtlichen Zitaten und Quellenbezug, fließende Prosa. Zielgruppe: informierte Leser."),
    4: ("Erzählend", "Erzählender, szenischer Stil mit narrativem Bogen und Anekdoten aus den Folgen. "
        "Sachlich korrekt, aber lebendig und unterhaltsam."),
    5: ("Leicht verständlich", "Einfache, gut lesbare Sprache. Kurze Sätze, Fachbegriffe kurz erklären, "
        "Analogien erlaubt, roter Faden wichtiger als Vollständigkeit. Zielgruppe: interessierte Einsteiger."),
}

ISSUE_PROMPT = """Du bist Chefredakteur*in einer {format_word}. Erstelle aus den folgenden Podcast-Transkripten KEINEN einzigen Artikel, sondern eine Ausgabe mit MEHREREN eigenständigen Abschnitten (Artikeln), thematisch geclustert.

FORMAT: {format_desc}
STIL ({style_name}): {style_instruction}
GESAMTUMFANG: ca. {total_words} Wörter, verteilt auf {n_sections} thematische Abschnitte.
{continuity}
{focus_block}
Erzeuge zusätzlich eine prägnante teilbare Kurzfassung (tldr_md): 5–8 Bullet-Punkte mit den Kernthemen, als eigenständiger weiterleitbarer Text.
Erzeuge außerdem GENAU EINEN Abschnitt mit kind "quote": das prägnanteste wörtliche Zitat der Ausgabe mit Sprecher und Folge.

Gib AUSSCHLIESSLICH valides JSON zurück (keine Markdown-Fences):
{{
  "title": "{title}",
  "subtitle": "treffender Untertitel der Ausgabe",
  "reading_time_min": <Zahl>,
  "sections": [
    {{"kind": "intro",   "heading": "", "body_md": "Editorial-Einstieg, der die Ausgabe einordnet"}},
    {{"kind": "article", "heading": "## Thementitel", "body_md": "Vollständiger Abschnitt in Markdown"}},
    {{"kind": "quote",   "heading": "Zitat der Woche", "body_md": "> „Zitat…" — Sprecher, Folge"}}
  ],
  "tldr_md": "- Punkt 1\\n- Punkt 2 …"
}}

Sprache: Deutsch.

TRANSKRIPTE:
{episodes_text}"""

DAILY_BRIEFING_PROMPT = """Du bist Redakteur für ein internes Team-Briefing. Fasse die folgenden transkribierten Podcast-Folgen in einem kompakten Tages-Briefing zusammen.

AUFBAU:
1. Kurzes Intro (2-3 Sätze): Was sind heute die zentralen Themen?
2. Pro Folge ein Abschnitt: Podcast-Name, Folgen-Titel, 3-5 Kernaussagen als Bullet-Liste
3. Abschluss-Empfehlung (1-2 Sätze): Welche Folge ist heute besonders relevant?
{focus_block}
Gib AUSSCHLIESSLICH valides JSON zurück (keine Markdown-Fences):
{{
  "title": "{title}",
  "subtitle": "Briefing vom {date}",
  "reading_time_min": <Zahl>,
  "sections": [
    {{"kind": "intro", "heading": "", "body_md": "Überblick"}},
    {{"kind": "article", "heading": "## Podcast: Folgen-Titel", "body_md": "- Kernaussage 1\\n- Kernaussage 2"}},
    {{"kind": "article", "heading": "## Empfehlung", "body_md": "Empfehlungstext"}}
  ],
  "tldr_md": "- Folge 1: Kernsatz\\n- Folge 2: Kernsatz"
}}

Sprache: Deutsch.

FOLGEN:
{episodes_text}"""

ARTIKEL_PROMPT = """Du bist Autor*in und schreibst einen zusammenhängenden Artikel nach einer freien Anweisung.

ANWEISUNG DES NUTZERS (maßgeblich für Thema, Aufbau und Tonfall):
{instruction}

STIL: {style_name} — {style_instruction}
UMFANG: ca. {total_words} Wörter, gegliedert in sinnvolle Abschnitte mit Zwischenüberschriften.
{focus_block}
{sources_block}

Gib AUSSCHLIESSLICH valides JSON zurück (keine Markdown-Fences):
{{
  "title": "{title}",
  "subtitle": "<eine prägnante Zeile>",
  "reading_time_min": <Zahl>,
  "sections": [
    {{"kind": "intro", "heading": "", "body_md": "Einleitung"}},
    {{"kind": "article", "heading": "## Zwischenüberschrift", "body_md": "Fließtext …"}}
  ],
  "tldr_md": "- Kernpunkt 1\\n- Kernpunkt 2"
}}

Sprache: Deutsch. Erfinde keine Fakten; wenn Quellen vorliegen, stütze dich auf sie."""

SUMMARY_TAKEAWAYS_PROMPT = """Du bist ein präziser Redakteur. Erstelle für jede der folgenden Podcast-Folgen eine knappe deutsche Zusammenfassung (3-5 Sätze) und 3-5 Key Takeaways.
{focus_block}
Gib AUSSCHLIESSLICH valides JSON zurück (keine Markdown-Fences):
{{
  "title": "{title}",
  "subtitle": "",
  "reading_time_min": <Zahl>,
  "sections": [
    {{
      "kind": "article",
      "heading": "## Podcast-Name: Folgen-Titel",
      "body_md": "**Zusammenfassung:** ...\\n\\n**Takeaways:**\\n- ...\\n- ..."
    }}
  ],
  "tldr_md": "- Folge 1: Kernsatz\\n- Folge 2: Kernsatz"
}}

Sprache: IMMER DEUTSCH — auch wenn die Originalfolgen auf Englisch sind.

FOLGEN:
{episodes_text}"""

TEAMS_POST_PROMPT = """Du schreibst einen kurzen, ansprechenden Microsoft-Teams-Post, der Kolleg*innen auf interessante Podcast-Folgen hinweist. Schreibe klar und direkt, kein Marketing-Sprech.

Format:
- 2-3 Einleitungssätze (Warum ist das relevant?)
- Pro Folge: eine Zeile mit Podcast-Name und Kernaussage
- Abschluss-Call-to-Action (1 Satz, z.B. „Hör mal rein!")
- Maximal 200 Wörter
{focus_block}
Gib AUSSCHLIESSLICH valides JSON zurück (keine Markdown-Fences):
{{
  "title": "{title}",
  "subtitle": "",
  "reading_time_min": 1,
  "sections": [
    {{"kind": "article", "heading": "", "body_md": "Vollständiger Teams-Post als Markdown"}}
  ],
  "tldr_md": ""
}}

Sprache: Deutsch.

FOLGEN:
{episodes_text}"""

TITLE_PROMPT = """Schlage EINEN kurzen, prägnanten deutschen Titel (max. 8 Wörter) für ein {format_label} vor, das folgende Podcast-Folgen zusammenfasst.

Gib NUR den Titel zurück — kein JSON, keine Erklärung, keine Anführungszeichen.

FOLGEN:
{episodes_text}"""

TRANSLATE_SUMMARY_PROMPT = """Übersetze die folgende Podcast-Zusammenfassung und die Takeaways ins Deutsche.

Gib AUSSCHLIESSLICH valides JSON zurück:
{{"summary": "Deutsche Zusammenfassung", "takeaways": ["Takeaway 1 auf Deutsch", "Takeaway 2 auf Deutsch"]}}

ZUSAMMENFASSUNG:
{summary}

TAKEAWAYS:
{takeaways}"""


# ── Public API ────────────────────────────────────────────────────────────────

async def transcribe_audio(audio_path: Path) -> dict:
    backend = _runtime["backend"]
    loop = asyncio.get_event_loop()

    if backend == "whisper":
        from . import whisper_backend
        result = await loop.run_in_executor(
            None, whisper_backend.transcribe, audio_path, _runtime["whisper_model"]
        )
        if _configure_gemini():
            try:
                full_text = _segments_to_text(result["segments"])
                enrichment = await loop.run_in_executor(None, _enrich_sync, full_text)
                result.update({k: enrichment[k] for k in
                               ("summary", "takeaways", "chapters") if k in enrichment})
                if enrichment.get("language") and not result.get("language"):
                    result["language"] = enrichment["language"]
            except Exception as e:
                logger.warning(f"Enrichment skipped: {e}")
        return result

    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert (Settings oder .env)")
    return await loop.run_in_executor(None, _gemini_full_sync, audio_path)


async def enrich_text(transcript: str) -> dict:
    if not _configure_gemini():
        return {}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _enrich_sync, transcript)


async def translate_to_german(text: str) -> str:
    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _translate_sync, text)


async def translate_summary(summary: str, takeaways: list) -> dict:
    """Cheaply translate an existing summary+takeaways to German (Lite model)."""
    if not _configure_gemini():
        return {}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _translate_summary_sync, summary, takeaways)


async def generate_digest(episode_data: list, mode: str, title: str) -> dict:
    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _digest_sync, episode_data, mode, title)


async def extract_tags(summary: str, takeaways: list, chapters: list) -> list:
    if not _configure_gemini():
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract_tags_sync, summary, takeaways, chapters)


def _generate_cover_sync(prompt: str) -> bytes:
    client = _client()
    # Wrap the user's prompt so we reliably get a square, cover-style artwork.
    full_prompt = (
        "Erzeuge ein quadratisches Cover-Artwork (1:1) für einen Podcast/Feed. "
        "Klar, modern, hoher Kontrast, gut als kleines App-Icon erkennbar, "
        "kein Text/Wasserzeichen. Motiv: " + prompt.strip()
    )
    response = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=full_prompt,
        # TEXT+IMAGE is the broadly-accepted modality combo; we keep only the
        # image part below, so any incidental text response is ignored.
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
    )
    for cand in (response.candidates or []):
        for part in (cand.content.parts or []):
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                return inline.data
    raise RuntimeError("Kein Bild von der Bild-KI erhalten")


async def generate_cover_image(prompt: str) -> bytes:
    """Generate cover art (PNG bytes) from a free-text prompt. Owner-only, on-demand."""
    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert")
    if not (prompt or "").strip():
        raise RuntimeError("Prompt fehlt")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _generate_cover_sync, prompt)


def _embed_sync(texts: list) -> list:
    client = _client()
    resp = client.models.embed_content(
        model=EMBED_MODEL, contents=texts,
        config=types.EmbedContentConfig(
            http_options=types.HttpOptions(timeout=_ISSUE_TIMEOUT_MS)),
    )
    return [list(e.values) for e in resp.embeddings]


RAG_PROMPT = """Du bist der Recherche-Assistent einer persönlichen Podcast- und \
Newsletter-Bibliothek. Beantworte die Frage AUSSCHLIESSLICH anhand der \
nummerierten Auszüge. Erfinde nichts. Wenn die Auszüge die Frage nicht \
beantworten, sage das offen. Antworte auf Deutsch, prägnant, und verweise mit \
[1], [2] … auf die genutzten Quellen.

Frage: {question}

Auszüge:
{context}

Antwort:"""


def _answer_sync(question: str, context: str) -> str:
    client = _client()
    response = client.models.generate_content(
        model=FLASH_MODEL,
        contents=[RAG_PROMPT.format(question=question, context=context)],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=2048),
    )
    return (response.text or "").strip()


async def answer_from_context(question: str, context: str) -> str:
    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _answer_sync, question, context)


CHAT_PROMPT = """Du bist der Recherche-Assistent einer persönlichen Podcast- und \
Newsletter-Bibliothek. Beantworte die LETZTE Nutzerfrage AUSSCHLIESSLICH anhand \
der nummerierten Auszüge. Beziehe den bisherigen Gesprächsverlauf als Kontext \
ein, erfinde aber nichts. Wenn die Auszüge die Frage nicht beantworten, sage das \
offen. Antworte auf Deutsch, prägnant, und verweise mit [1], [2] … auf die \
genutzten Quellen.

Gesprächsverlauf:
{history}

Auszüge:
{context}

Antwort:"""


def _chat_sync(history: str, context: str) -> str:
    client = _client()
    response = client.models.generate_content(
        model=FLASH_MODEL,
        contents=[CHAT_PROMPT.format(history=history, context=context)],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=2048),
    )
    return (response.text or "").strip()


async def answer_chat_from_context(history: str, context: str) -> str:
    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _chat_sync, history, context)


async def embed_texts(texts: list) -> list:
    """Return one embedding vector (list of floats) per input text. Empty list
    when no API key is configured (callers treat this as 'indexing disabled')."""
    if not _configure_gemini() or not texts:
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _embed_sync, texts)


async def generate_issue(episode_data: list, *, fmt: str, length: int = 3, style: int = 3,
                         title: str, prev_tldr: str = "",
                         model: str = "", custom_style: str = "", focus: str = "",
                         prompt: str = "") -> dict:
    """Multi-section issue for any of the formats.
    Returns {title, subtitle, reading_time_min, sections:[{kind,heading,body_md}], tldr_md}."""
    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _issue_sync, episode_data, fmt, length, style, title, prev_tldr,
        model, custom_style, focus, prompt
    )  # `prompt` is threaded as user_prompt into _issue_sync


async def generate_title(episode_data: list, fmt: str) -> str:
    """Auto-generate a short German title for a digest (Lite model)."""
    if not _configure_gemini():
        return ""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _title_sync, episode_data, fmt)


# ── Gemini implementations ────────────────────────────────────────────────────

_INLINE_LIMIT = 18 * 1024 * 1024


def _gemini_full_sync(audio_path: Path) -> dict:
    client = _client()
    size = audio_path.stat().st_size

    if size <= _INLINE_LIMIT:
        logger.info(f"Transcribing {audio_path.name} inline ({size//1024} KB)...")
        audio_bytes = audio_path.read_bytes()
        response = client.models.generate_content(
            model=FLASH_MODEL,
            contents=[
                GEMINI_FULL_PROMPT,
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/mpeg"),
            ],
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=65536),
        )
        return _parse_json(response.text)

    logger.info(f"Audio too large for inline ({size//1024} KB), using File API...")
    uploaded = client.files.upload(
        file=str(audio_path),
        config=types.UploadFileConfig(mime_type="audio/mpeg"),
    )
    for _ in range(160):
        if uploaded.state.name != "PROCESSING":
            break
        time.sleep(3)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state.name == "FAILED":
        raise RuntimeError("Gemini audio processing failed")

    response = client.models.generate_content(
        model=FLASH_MODEL,
        contents=[GEMINI_FULL_PROMPT, uploaded],
        config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=65536),
    )
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass
    return _parse_json(response.text)


def _enrich_sync(transcript: str) -> dict:
    client = _client()
    response = client.models.generate_content(
        model=LITE_MODEL,
        contents=[ENRICH_PROMPT.format(transcript=transcript[:120000])],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=8192),
    )
    return _parse_json(response.text)


def _translate_sync(text: str) -> str:
    client = _client()
    prompt = (
        "Übersetze das folgende Podcast-Transkript ins Deutsche. "
        "Behalte Zeitstempel und Sprecher-Labels bei. "
        "Gib NUR den übersetzten Text zurück.\n\n" + text[:60000]
    )
    response = client.models.generate_content(
        model=LITE_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=65536),
    )
    return response.text


def _translate_summary_sync(summary: str, takeaways: list) -> dict:
    client = _client()
    prompt = TRANSLATE_SUMMARY_PROMPT.format(
        summary=(summary or "")[:3000],
        takeaways="\n".join(f"- {t}" for t in (takeaways or []))[:2000],
    )
    response = client.models.generate_content(
        model=LITE_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=2048),
    )
    data = _parse_json(response.text)
    if isinstance(data, dict) and "summary" in data:
        return data
    return {}


def _digest_sync(episode_data: list, mode: str, title: str) -> dict:
    mode_instructions = {
        "weekly": "Dies ist eine Wochenzusammenfassung. Verbinde die Themen der Woche zu einem kohärenten Gesamtbild.",
        "theme": "Dies ist ein thematischer Report. Analysiere das gemeinsame Thema aus verschiedenen Perspektiven.",
        "portrait": "Dies ist ein Podcast-Portrait. Charakterisiere Stil, Qualität und wiederkehrende Themen.",
    }.get(mode, "Erstelle einen umfassenden journalistischen Artikel.")

    episodes_text = ""
    for ep in episode_data:
        episodes_text += f"\n\n### Episode: {ep.get('title', 'Unbekannt')}\n"
        episodes_text += f"Podcast: {ep.get('podcast_title', '')}\n"
        episodes_text += f"Datum: {ep.get('pub_date', '')}\n\n"
        episodes_text += (ep.get("transcript") or "")[:12000]

    prompt = DIGEST_PROMPT.format(
        mode_instruction=mode_instructions, title=title,
        episodes_text=episodes_text[:60000],
    )
    client = _client()
    response = client.models.generate_content(
        model=_digest_model(),
        contents=[prompt],
        config=types.GenerateContentConfig(temperature=0.7, max_output_tokens=8192),
    )
    data = _parse_json(response.text)
    if "content_md" not in data:
        data = {"title": title, "subtitle": "", "content_md": response.text, "reading_time_min": 10}
    return data


def _extract_tags_sync(summary: str, takeaways: list, chapters: list) -> list:
    chapter_titles = ", ".join(c.get("title", "") for c in (chapters or []) if isinstance(c, dict))
    prompt = TAG_EXTRACT_PROMPT.format(
        summary=(summary or "")[:4000],
        takeaways="; ".join(takeaways or [])[:2000],
        chapters=chapter_titles[:1000],
    )
    client = _client()
    response = client.models.generate_content(
        model=LITE_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=1024),
    )
    data = _parse_json(response.text)
    tags = data.get("tags", []) if isinstance(data, dict) else []
    return [t for t in tags if isinstance(t, dict) and t.get("label")]


def _build_episodes_text(episode_data: list, max_per_ep: int = 10000) -> str:
    text = ""
    for ep in episode_data:
        text += f"\n\n### {ep.get('title','Folge')} ({ep.get('podcast_title','')}, {ep.get('pub_date','')})\n"
        body = ep.get("transcript") or ep.get("summary") or ""
        text += body[:max_per_ep]
    return text


def _issue_sync(episode_data: list, fmt: str, length: int, style: int,
                title: str, prev_tldr: str, model: str = "", custom_style: str = "",
                focus: str = "", user_prompt: str = "") -> dict:
    # Normalise legacy format name
    if fmt == "zeitung":
        fmt = "magazin"

    chosen_model = _resolve_model(model, fmt)
    focus_block = f"\nREDAKTIONELLER FOKUS / Empfehlung: {focus}\n" if focus else ""

    # ── Freier Artikel (free-prompt; library episodes optional) ───────────────
    if fmt == "artikel":
        lconf = _LENGTH_MAP.get(int(length), _LENGTH_MAP[3])
        if custom_style:
            style_name, style_instruction = "Benutzerdefiniert", custom_style
        else:
            style_name, style_instruction = _STYLE_MAP.get(int(style), _STYLE_MAP[3])
        episodes_text = _build_episodes_text(episode_data, 10000) if episode_data else ""
        sources_block = (
            f"QUELLEN AUS DER BIBLIOTHEK (als Faktenbasis nutzen, nicht erfinden):\n{episodes_text[:120000]}"
            if episodes_text.strip()
            else "Es liegen keine Bibliotheks-Quellen vor — schreibe den Artikel allein "
                 "auf Basis der Anweisung und deines Allgemeinwissens."
        )
        prompt = ARTIKEL_PROMPT.format(
            instruction=(user_prompt or focus or title or "Schreibe einen Artikel."),
            style_name=style_name, style_instruction=style_instruction,
            total_words=lconf["words"], focus_block=focus_block,
            title=title or "Artikel", sources_block=sources_block,
        )
        client = _client()
        response = client.models.generate_content(
            model=chosen_model,
            contents=[prompt],
            config=types.GenerateContentConfig(temperature=0.7, max_output_tokens=lconf["tokens"],
                                               http_options=types.HttpOptions(timeout=_ISSUE_TIMEOUT_MS)),
        )
        data = _parse_json(response.text)
        if not isinstance(data, dict) or "sections" not in data:
            data = {"title": title, "subtitle": "", "reading_time_min": 5,
                    "sections": [{"kind": "article", "heading": "", "body_md": response.text}],
                    "tldr_md": ""}
        return data

    # ── Daily Briefing ────────────────────────────────────────────────────────
    if fmt == "daily_briefing":
        from datetime import date
        episodes_text = _build_episodes_text(episode_data, 6000)
        prompt = DAILY_BRIEFING_PROMPT.format(
            title=title or "Daily Briefing",
            date=date.today().strftime("%d.%m.%Y"),
            focus_block=focus_block,
            episodes_text=episodes_text[:80000],
        )
        client = _client()
        response = client.models.generate_content(
            model=chosen_model,
            contents=[prompt],
            config=types.GenerateContentConfig(temperature=0.5, max_output_tokens=8192,
                                               http_options=types.HttpOptions(timeout=_ISSUE_TIMEOUT_MS)),
        )
        data = _parse_json(response.text)
        if not isinstance(data, dict) or "sections" not in data:
            data = {"title": title, "subtitle": "", "reading_time_min": 3,
                    "sections": [{"kind": "article", "heading": "", "body_md": response.text}],
                    "tldr_md": ""}
        return data

    # ── Summary + Takeaways ───────────────────────────────────────────────────
    if fmt == "summary_takeaways":
        # Prefer existing summaries over full transcript (cheaper)
        episodes_text = ""
        for ep in episode_data:
            tk = ep.get("takeaways") or []
            if isinstance(tk, str):
                try: tk = json.loads(tk)
                except: tk = []
            summary_txt = ep.get("summary") or ""
            if summary_txt:
                body = f"Zusammenfassung: {summary_txt}\nTakeaways: {'; '.join(tk)}"
            else:
                body = (ep.get("transcript") or "")[:4000]
            episodes_text += f"\n\n### {ep.get('title','Folge')} ({ep.get('podcast_title','')}, {ep.get('pub_date','')})\n{body}"

        prompt = SUMMARY_TAKEAWAYS_PROMPT.format(
            title=title or "Summary + Takeaways",
            focus_block=focus_block,
            episodes_text=episodes_text[:80000],
        )
        client = _client()
        response = client.models.generate_content(
            model=chosen_model,
            contents=[prompt],
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=8192,
                                               http_options=types.HttpOptions(timeout=_ISSUE_TIMEOUT_MS)),
        )
        data = _parse_json(response.text)
        if not isinstance(data, dict) or "sections" not in data:
            data = {"title": title, "subtitle": "", "reading_time_min": 3,
                    "sections": [{"kind": "article", "heading": "", "body_md": response.text}],
                    "tldr_md": ""}
        return data

    # ── Teams Post ────────────────────────────────────────────────────────────
    if fmt == "teams_post":
        episodes_text = _build_episodes_text(episode_data, 3000)
        prompt = TEAMS_POST_PROMPT.format(
            title=title or "Teams Post",
            focus_block=focus_block,
            episodes_text=episodes_text[:40000],
        )
        client = _client()
        response = client.models.generate_content(
            model=chosen_model,
            contents=[prompt],
            config=types.GenerateContentConfig(temperature=0.6, max_output_tokens=2048,
                                               http_options=types.HttpOptions(timeout=_ISSUE_TIMEOUT_MS)),
        )
        data = _parse_json(response.text)
        if not isinstance(data, dict) or "sections" not in data:
            data = {"title": title, "subtitle": "", "reading_time_min": 1,
                    "sections": [{"kind": "article", "heading": "", "body_md": response.text}],
                    "tldr_md": ""}
        return data

    # ── Magazin / Newsletter (slider-based) ───────────────────────────────────
    lconf = _LENGTH_MAP.get(int(length), _LENGTH_MAP[3])
    if custom_style:
        style_name = "Benutzerdefiniert"
        style_instruction = custom_style
    else:
        style_name, style_instruction = _STYLE_MAP.get(int(style), _STYLE_MAP[3])

    if fmt == "newsletter":
        format_word = "KI-Newsletter-Redaktion"
        format_desc = ("Newsletter: kurze, scannbare Abschnitte mit je 2–4 Sätzen plus "
                       "Bullet-Takeaways. Oben ein Inhaltsverzeichnis. Knapp und teilbar.")
        temp = 0.6
    else:  # magazin
        format_word = "KI-Zeitung"
        format_desc = ("Zeitung: ausführliche, fließende Prosa-Artikel mit Analyse, "
                       "Kontext und wörtlichen Zitaten. Ein redaktionelles Intro.")
        temp = 0.7

    continuity = ""
    if prev_tldr:
        continuity = ("ANSCHLUSS: Beginne mit einem kurzen Abschnitt 'Neu seit der letzten Ausgabe', "
                      "der auf folgende vorige Kurzfassung Bezug nimmt:\n" + prev_tldr[:1500])

    episodes_text = _build_episodes_text(episode_data, 10000)

    prompt = ISSUE_PROMPT.format(
        format_word=format_word, format_desc=format_desc,
        style_name=style_name, style_instruction=style_instruction,
        total_words=lconf["words"], n_sections=lconf["sections"],
        continuity=continuity, focus_block=focus_block,
        title=title, episodes_text=episodes_text[:120000],
    )
    client = _client()
    response = client.models.generate_content(
        model=chosen_model,
        contents=[prompt],
        config=types.GenerateContentConfig(temperature=temp, max_output_tokens=lconf["tokens"],
                                           http_options=types.HttpOptions(timeout=_ISSUE_TIMEOUT_MS)),
    )
    data = _parse_json(response.text)
    if not isinstance(data, dict) or "sections" not in data:
        data = {"title": title, "subtitle": "", "reading_time_min": 10,
                "sections": [{"kind": "article", "heading": "", "body_md": response.text}],
                "tldr_md": ""}
    return data


def _title_sync(episode_data: list, fmt: str) -> str:
    format_label = FORMATS.get(fmt, {}).get("label", "Ausgabe")
    episodes_text = ""
    for ep in episode_data[:5]:
        episodes_text += f"- {ep.get('title','')} ({ep.get('podcast_title','')})\n"
    prompt = TITLE_PROMPT.format(
        format_label=format_label,
        episodes_text=episodes_text[:2000],
    )
    client = _client()
    response = client.models.generate_content(
        model=LITE_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(temperature=0.8, max_output_tokens=64),
    )
    return (response.text or "").strip().strip('"').strip("'")[:120]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _segments_to_text(segments: list) -> str:
    return "\n".join(
        f"[{s.get('time','')}] {s.get('speaker','Speaker')}: {s.get('text','')}"
        for s in segments
    )


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {
            "language": "unknown", "speakers": [],
            "segments": [{"time": "00:00:00", "speaker": "Speaker", "text": text}],
            "summary": "", "takeaways": [], "chapters": [],
        }
