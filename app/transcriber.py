import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import google.generativeai as genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
FLASH_MODEL = "gemini-1.5-flash"
PRO_MODEL = "gemini-1.5-pro"

TRANSCRIPTION_PROMPT = """Analyze this podcast audio episode completely and return a structured JSON transcription.

Return ONLY a valid JSON object — no markdown, no explanation, just JSON:
{
  "language": "language code, e.g. 'de' or 'en'",
  "speakers": ["Host", "Guest"],
  "segments": [
    {"time": "00:00:00", "speaker": "Host", "text": "Exact spoken words here"}
  ],
  "summary": "5-sentence comprehensive summary of the episode covering all major topics and key insights",
  "takeaways": [
    "First important insight or learning from this episode",
    "Second key point"
  ],
  "chapters": [
    {"title": "Introduction", "start_time": "00:00:00", "summary": "Brief description of what is covered"}
  ]
}

Rules:
- Include ALL spoken content, complete and untruncated
- Timestamps in HH:MM:SS format, accurate to each speaker turn
- Identify and name speakers consistently (use actual names if mentioned, else 'Host'/'Guest')
- Create 5-10 meaningful chapters at major topic transitions
- summary, takeaways in the SAME language as the audio
- 6-10 takeaways, each a clear insight in 1-2 sentences"""

DIGEST_PROMPT = """Du bist ein erfahrener Journalist und Chefredakteur. Erstelle einen ausführlichen, qualitativ hochwertigen journalistischen Artikel basierend auf den folgenden Podcast-Transkripten.

{mode_instruction}

ANFORDERUNGEN:
- Länge: 1200-2000 Wörter (Pflicht — nicht kürzer!)
- Stil: Journalistisch, analytisch, fließend — KEIN Stichpunkt-Stil im Haupttext
- Struktur: Packende Einleitung, Hauptteil mit H2-Unterabschnitten, Fazit
- Wörtliche Zitate aus den Transkripten verwenden (in „Anführungszeichen" mit Sprecher-Name)
- Kontext und Einordnung: Bedeutung der Themen für den Leser erklären
- Ton: Sachlich-interessiert, nicht werbend, journalistisch neutral
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


def _configure_gemini():
    key = os.getenv("GEMINI_API_KEY", "")
    if key:
        genai.configure(api_key=key)


async def transcribe_audio(audio_path: Path) -> dict:
    _configure_gemini()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, audio_path)


def _transcribe_sync(audio_path: Path) -> dict:
    model = genai.GenerativeModel(FLASH_MODEL)

    logger.info(f"Uploading {audio_path} to Gemini...")
    uploaded = genai.upload_file(str(audio_path), mime_type="audio/mpeg")

    # Wait for processing
    for _ in range(120):
        if uploaded.state.name != "PROCESSING":
            break
        time.sleep(3)
        uploaded = genai.get_file(uploaded.name)

    if uploaded.state.name == "FAILED":
        raise RuntimeError("Gemini audio processing failed")

    logger.info("Generating transcription...")
    response = model.generate_content(
        [TRANSCRIPTION_PROMPT, uploaded],
        generation_config={"temperature": 0.1, "max_output_tokens": 65536},
    )

    try:
        genai.delete_file(uploaded.name)
    except Exception:
        pass

    return _parse_json_response(response.text)


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "language": "unknown",
            "speakers": [],
            "segments": [{"time": "00:00:00", "speaker": "Speaker", "text": text}],
            "summary": "",
            "takeaways": [],
            "chapters": [],
        }


async def translate_to_german(text: str) -> str:
    _configure_gemini()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _translate_sync, text)


def _translate_sync(text: str) -> str:
    model = genai.GenerativeModel(FLASH_MODEL)
    prompt = (
        "Übersetze das folgende Podcast-Transkript ins Deutsche. "
        "Behalte Zeitstempel und Sprecher-Labels bei. "
        "Gib NUR den übersetzten Text zurück, keine Erklärungen.\n\n"
        + text[:50000]
    )
    response = model.generate_content(
        [prompt],
        generation_config={"temperature": 0.2, "max_output_tokens": 65536},
    )
    return response.text


async def generate_digest(episode_data: list, mode: str, title: str) -> dict:
    _configure_gemini()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _digest_sync, episode_data, mode, title)


def _digest_sync(episode_data: list, mode: str, title: str) -> dict:
    mode_instructions = {
        "weekly": "Dies ist eine Wochenzusammenfassung. Verbinde die Themen der Woche zu einem kohärenten Gesamtbild und zeige Querverbindungen auf.",
        "theme": "Dies ist ein thematischer Report. Analysiere das gemeinsame Thema aus verschiedenen Perspektiven und Quellen.",
        "portrait": "Dies ist ein Podcast-Portrait. Charakterisiere den Stil, die Qualität, die wiederkehrenden Themen und den Wert dieses Podcasts für den Hörer.",
    }.get(mode, "Erstelle einen umfassenden journalistischen Artikel.")

    episodes_text = ""
    for ep in episode_data:
        episodes_text += f"\n\n### Episode: {ep.get('title', 'Unbekannt')}\n"
        episodes_text += f"Podcast: {ep.get('podcast_title', '')}\n"
        episodes_text += f"Datum: {ep.get('pub_date', '')}\n\n"
        episodes_text += (ep.get("transcript") or "")[:12000]

    prompt = DIGEST_PROMPT.format(
        mode_instruction=mode_instructions,
        title=title,
        episodes_text=episodes_text[:60000],
    )

    model = genai.GenerativeModel(PRO_MODEL)
    response = model.generate_content(
        [prompt],
        generation_config={"temperature": 0.7, "max_output_tokens": 8192},
    )

    data = _parse_json_response(response.text)
    if "content_md" not in data:
        data = {"title": title, "subtitle": "", "content_md": response.text, "reading_time_min": 10}
    return data
