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

# Model names overridable via env (Gemini 1.5 was retired; default to 2.5)
FLASH_MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
PRO_MODEL = os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")

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


# ── Gemini implementations ────────────────────────────────────────────────────

def _gemini_full_sync(audio_path: Path) -> dict:
    client = _client()
    logger.info(f"Uploading {audio_path.name} to Gemini...")
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

    logger.info("Generating transcription...")
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
        model=FLASH_MODEL,
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
