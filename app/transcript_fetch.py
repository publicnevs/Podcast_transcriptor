"""Fetch and parse pre-existing transcripts from podcast feeds.

Many modern feeds expose a <podcast:transcript> tag (Podcast Index standard)
pointing to a VTT / SRT / JSON / plain-text transcript. When present we use it
directly — no audio download, no Gemini transcription cost. Gemini is then only
used (cheaply, text-only) to add summary / takeaways / chapters.
"""
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; PodScribe/1.0; +https://github.com/publicnevs/Podcast_transcriptor)"


async def fetch_transcript(url: str, type_hint: str = "") -> dict:
    """Download and parse a transcript. Returns {language, segments:[{time,speaker,text}]}.

    Raises on network errors or unparseable content so the caller can fall back
    to audio transcription.
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                 headers={"User-Agent": _UA}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text
        ctype = (resp.headers.get("content-type", "") or "").lower()

    fmt = _detect_format(url, type_hint, ctype, text)
    logger.info(f"Parsing feed transcript as '{fmt}' from {url}")

    if fmt == "json":
        segments = _parse_json_transcript(text)
    elif fmt == "srt":
        segments = _parse_srt(text)
    elif fmt == "vtt":
        segments = _parse_vtt(text)
    else:
        segments = _parse_plain(text)

    if not segments:
        raise ValueError("Transcript parsed to zero segments")

    return {"language": "", "speakers": [], "segments": segments}


def _detect_format(url, type_hint, ctype, text):
    hay = f"{type_hint} {ctype} {url}".lower()
    if "json" in hay:
        return "json"
    if "srt" in hay:
        return "srt"
    if "vtt" in hay:
        return "vtt"
    # sniff content
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    if stripped.upper().startswith("WEBVTT"):
        return "vtt"
    if re.match(r"^\d+\s*\n\d{2}:\d{2}:\d{2}", stripped):
        return "srt"
    return "text"


def _parse_json_transcript(text: str) -> list:
    """Podcast Index JSON transcript format: {"segments":[{"startTime","speaker","body"}]}"""
    data = json.loads(text)
    raw = data.get("segments", data if isinstance(data, list) else [])
    out = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        start = s.get("startTime", s.get("start", 0))
        body = s.get("body", s.get("text", "")).strip()
        speaker = s.get("speaker", "") or ""
        if body:
            out.append({"time": _sec_to_hms(_to_seconds(start)),
                        "speaker": speaker, "text": body})
    return _merge_short(out)


def _parse_srt(text: str) -> list:
    out = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        ts_line = next((l for l in lines if "-->" in l), None)
        if not ts_line:
            continue
        start = ts_line.split("-->")[0].strip()
        body_lines = lines[lines.index(ts_line) + 1:]
        body = " ".join(body_lines).strip()
        if body:
            out.append({"time": _normalize_ts(start), "speaker": "", "text": body})
    return _merge_short(out)


def _parse_vtt(text: str) -> list:
    out = []
    text = re.sub(r"^WEBVTT.*?\n", "", text, flags=re.DOTALL | re.IGNORECASE, count=1)
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        ts_line = next((l for l in lines if "-->" in l), None)
        if not ts_line:
            continue
        start = ts_line.split("-->")[0].strip()
        body_lines = lines[lines.index(ts_line) + 1:]
        # VTT may carry <v Speaker> voice tags
        body = " ".join(body_lines).strip()
        speaker = ""
        m = re.match(r"<v\s+([^>]+)>", body)
        if m:
            speaker = m.group(1).strip()
        body = re.sub(r"</?v[^>]*>", "", body)
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            out.append({"time": _normalize_ts(start), "speaker": speaker, "text": body})
    return _merge_short(out)


def _parse_plain(text: str) -> list:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        paras = [text] if text else []
    return [{"time": "", "speaker": "", "text": p} for p in paras]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _merge_short(segments: list, min_chars: int = 180) -> list:
    """Merge tiny caption cues into readable paragraphs, keeping the first
    timestamp/speaker of each merged block."""
    if not segments:
        return []
    merged = []
    cur = None
    for s in segments:
        if cur is None:
            cur = dict(s)
            continue
        same_speaker = (s.get("speaker", "") == cur.get("speaker", ""))
        if same_speaker and len(cur["text"]) < min_chars:
            cur["text"] = (cur["text"] + " " + s["text"]).strip()
        else:
            merged.append(cur)
            cur = dict(s)
    if cur:
        merged.append(cur)
    return merged


def _to_seconds(val) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    return _ts_to_seconds(str(val))


def _ts_to_seconds(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except Exception:
        return 0.0


def _normalize_ts(ts: str) -> str:
    return _sec_to_hms(_ts_to_seconds(ts))


def _sec_to_hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"
