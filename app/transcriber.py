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

# Model names overridable via env — 2.5 Flash for transcription, Lite for enrichment/tagging, Pro for digests
FLASH_MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
PRO_MODEL = os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
# Lite model: cheaper for enrichment + tagging; falls back to FLASH if not set
LITE_MODEL = os.getenv("GEMINI_LITE_MODEL", FLASH_MODEL)

# Runtime-overridable settings (set from DB at startup / on change)
_runtime = {
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
    "backend": os.getenv("TRANSCRIPTION_BACKEND", "gemini"),
    "whisper_model": os.getenv("WHISPER_MODEL", "base"),
}


def configure(gemini_api_key=None, backend=None, whisper_model=None):
    if gemini_api_key is not None:
        _runtime["gemini_api_key"] = gemini_api_key
    if backend:
        _runtime["backend"] = backend
    if whisper_model:
        _runtime["whisper_model"] = whisper_model


def get_backend():
    return _runtime["backend"]


def _get_key():
    return _runtime["gemini_api_key"] or os.getenv("GEMINI_API_KEY", "")


def _configure_gemini():
    return bool(_get_key())


def _client():
    """Fresh client per use — new google-genai SDK, supports AQ. keys."""
    return genai.Client(api_key=_get_key())


# ── Prompts ──────────────────────────────────────────────────────────────────

GEMINI_FULL_PROMPT = """Analyze this podcast audio completely and return a structured JSON transcription.

Return ONLY valid JSON — no markdown fences, no prose:
{
  "language": "ISO code, e.g. 'de' or 'en'",
  "speakers": ["Host", "Guest"],
  "segments": [
    {"time": "00:00:00", "speaker": "Host", "text": "Exact spoken words"}
  ],
  "summary": "5-sentence comprehensive summary covering all major topics and insights",
  "takeaways": ["First key insight", "Second key insight"],
  "chapters": [
    {"title": "Introduction", "start_time": "00:00:00", "summary": "What is covered here"}
  ]
}

Rules:
- Transcribe ALL spoken content, complete and untruncated
- Accurate HH:MM:SS timestamps at each speaker turn
- Identify speakers consistently (real names if mentioned, else 'Host'/'Guest')
- 5-10 meaningful chapters at real topic transitions
- summary + takeaways in the SAME language as the audio
- 6-10 takeaways, each a clear 1-2 sentence insight"""

ENRICH_PROMPT = """You are given a podcast transcript. Produce structured metadata.

Return ONLY valid JSON — no markdown fences, no prose:
{
  "language": "ISO code of the transcript language",
  "summary": "5-sentence comprehensive summary covering all major topics",
  "takeaways": ["6-10 clear insights, each 1-2 sentences"],
  "chapters": [
    {"title": "Chapter title", "start_time": "HH:MM:SS", "summary": "What is covered"}
  ]
}

Use the timestamps present in the transcript to place chapters at real topic
transitions. summary + takeaways in the SAME language as the transcript.

TRANSCRIPT:
{transcript}"""

DIGEST_PROMPT = """Du bist ein erfahrener Journalist und Chefredakteur. Erstelle einen ausführlichen, qualitativ hochwertigen journalistischen Artikel basierend auf den folgenden Podcast-Transkripten.

{mode_instruction}

ANFORDERUNGEN:
- Länge: 1200-2000 Wörter (Pflicht — nicht kürzer!)
- Stil: Journalistisch, analytisch, fließend — KEIN Stichpunkt-Stil im Haupttext
- Struktur: Packende Einleitung, Hauptteil mit ## Zwischenüberschriften, Fazit
- Wörtliche Zitate aus den Transkripten verwenden (in „Anführungszeichen" mit Sprecher-Name)
- Kontext und Einordnung: Bedeutung der Themen für den Leser erklären
- Ton: Sachlich-interessiert, journalistisch neutral
- Sprache: Deutsch (englische Zitate übersetzen, Original in Klammern)

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


# ── Zeitung 2.0: tagging + multi-section issues ───────────────────────────────

TAG_EXTRACT_PROMPT = """Du bist ein Wissens-Kurator. Extrahiere aus dieser Podcast-Zusammenfassung 4–8 normalisierte Themen-Tags.

Regeln:
- label: kanonische Schreibweise (Eigennamen korrekt, Singular bevorzugt, z.B. "Prompt Engineering" statt "Prompts schreiben", "GitHub Copilot" statt "Copilot")
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

# Length slider 1-5 → target words + section count
_LENGTH_MAP = {
    1: {"label": "Kompakt",     "words": 700,  "sections": 3, "tokens": 8192},
    2: {"label": "Knapp",       "words": 1200, "sections": 3, "tokens": 12000},
    3: {"label": "Standard",    "words": 1800, "sections": 4, "tokens": 16000},
    4: {"label": "Ausführlich", "words": 2800, "sections": 5, "tokens": 24000},
    5: {"label": "Magazin",     "words": 4000, "sections": 6, "tokens": 32000},
}

# Style slider 1-5 → German instruction block
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
    {{"kind": "quote",   "heading": "Zitat der Woche", "body_md": "> „Zitat…“ — Sprecher, Folge"}}
  ],
  "tldr_md": "- Punkt 1\\n- Punkt 2 …"
}}

Sprache: Deutsch (fremdsprachige Zitate übersetzen, Original in Klammern).

TRANSKRIPTE:
{episodes_text}"""


# ── Public API ────────────────────────────────────────────────────────────────

async def transcribe_audio(audio_path: Path) -> dict:
    """Return dict: language, speakers, segments, [summary, takeaways, chapters].

    Gemini backend fills everything in one call. Whisper backend fills
    segments only; enrichment runs afterwards as a cheap text call.
    """
    backend = _runtime["backend"]
    loop = asyncio.get_event_loop()

    if backend == "whisper":
        from . import whisper_backend
        result = await loop.run_in_executor(
            None, whisper_backend.transcribe, audio_path, _runtime["whisper_model"]
        )
        # Enrich with Gemini text call (summary/takeaways/chapters)
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

    # default: gemini
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


async def generate_digest(episode_data: list, mode: str, title: str) -> dict:
    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _digest_sync, episode_data, mode, title)


async def extract_tags(summary: str, takeaways: list, chapters: list) -> list:
    """Return [{'label','kind'}] canonical topic tags from compact episode metadata."""
    if not _configure_gemini():
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract_tags_sync, summary, takeaways, chapters)


async def generate_issue(episode_data: list, *, fmt: str, length: int, style: int,
                         title: str, prev_tldr: str = "") -> dict:
    """Multi-section issue (Zeitung/Newsletter). Returns
    {title, subtitle, reading_time_min, sections:[{kind,heading,body_md}], tldr_md}."""
    if not _configure_gemini():
        raise RuntimeError("Kein Gemini API Key konfiguriert")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _issue_sync, episode_data, fmt, length, style, title, prev_tldr)


# ── Gemini implementations ────────────────────────────────────────────────────

_INLINE_LIMIT = 18 * 1024 * 1024   # leave headroom under Gemini's 20 MB cap


def _gemini_full_sync(audio_path: Path) -> dict:
    """Transcribe audio with Gemini.

    Always tries inline_data first because the FileService.CreateFile
    endpoint rejects newer AQ.-prefix API keys (401 ACCESS_TOKEN_TYPE_UNSUPPORTED).
    Files larger than the inline cap fall back to the upload path."""
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

    # Fallback for very long episodes — requires AIza-prefix key
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
        model=FLASH_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=65536),
    )
    return response.text


def _digest_sync(episode_data: list, mode: str, title: str) -> dict:
    mode_instructions = {
        "weekly": "Dies ist eine Wochenzusammenfassung. Verbinde die Themen der Woche zu einem kohärenten Gesamtbild und zeige Querverbindungen auf.",
        "theme": "Dies ist ein thematischer Report. Analysiere das gemeinsame Thema aus verschiedenen Perspektiven und Quellen.",
        "portrait": "Dies ist ein Podcast-Portrait. Charakterisiere Stil, Qualität, wiederkehrende Themen und den Wert dieses Podcasts.",
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
        model=PRO_MODEL,
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


def _issue_sync(episode_data: list, fmt: str, length: int, style: int,
                title: str, prev_tldr: str) -> dict:
    lconf = _LENGTH_MAP.get(int(length), _LENGTH_MAP[3])
    style_name, style_instruction = _STYLE_MAP.get(int(style), _STYLE_MAP[3])

    if fmt == "newsletter":
        format_word = "KI-Newsletter-Redaktion"
        format_desc = ("Newsletter: kurze, scannbare Abschnitte mit je 2–4 Sätzen plus "
                       "Bullet-Takeaways. Oben ein Inhaltsverzeichnis. Knapp und teilbar.")
        model = FLASH_MODEL
        temp = 0.6
    else:
        format_word = "KI-Zeitung"
        format_desc = ("Zeitung: ausführliche, fließende Prosa-Artikel mit Analyse, "
                       "Kontext und wörtlichen Zitaten. Ein redaktionelles Intro.")
        model = PRO_MODEL
        temp = 0.7

    continuity = ""
    if prev_tldr:
        continuity = ("ANSCHLUSS: Beginne mit einem kurzen Abschnitt „Neu seit der letzten Ausgabe“, "
                      "der auf folgende vorige Kurzfassung Bezug nimmt und nur das Neue hervorhebt:\n"
                      + prev_tldr[:1500])

    episodes_text = ""
    for ep in episode_data:
        episodes_text += f"\n\n### {ep.get('title','Folge')} ({ep.get('podcast_title','')}, {ep.get('pub_date','')})\n"
        episodes_text += (ep.get("transcript") or ep.get("summary") or "")[:10000]

    prompt = ISSUE_PROMPT.format(
        format_word=format_word, format_desc=format_desc,
        style_name=style_name, style_instruction=style_instruction,
        total_words=lconf["words"], n_sections=lconf["sections"],
        continuity=continuity, title=title, episodes_text=episodes_text[:120000],
    )
    client = _client()
    response = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(temperature=temp, max_output_tokens=lconf["tokens"]),
    )
    data = _parse_json(response.text)
    if not isinstance(data, dict) or "sections" not in data:
        data = {"title": title, "subtitle": "", "reading_time_min": 10,
                "sections": [{"kind": "article", "heading": "", "body_md": response.text}],
                "tldr_md": ""}
    return data


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
        # try to extract the first {...} block
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
