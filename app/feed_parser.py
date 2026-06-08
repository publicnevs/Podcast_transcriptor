import asyncio
import re
from typing import Optional

import feedparser
import httpx


async def parse_rss_feed(url: str) -> dict:
    if "spotify.com" in url:
        url = await _resolve_spotify(url)

    loop = asyncio.get_event_loop()
    feed = await loop.run_in_executor(None, feedparser.parse, url)

    if not feed.get("feed") or not feed.feed.get("title"):
        raise ValueError(f"Ungültiger RSS-Feed: {url}")

    podcast = {
        "title": feed.feed.get("title", "Unbekannter Podcast"),
        "description": _clean(feed.feed.get("summary", "") or feed.feed.get("subtitle", "")),
        "artwork_url": _get_artwork(feed),
        "website_url": feed.feed.get("link", ""),
        "language": feed.feed.get("language", ""),
        "rss_url": url,
    }

    episodes = []
    has_audio = False
    for entry in feed.entries:
        audio_url = _extract_audio(entry)
        if audio_url:
            has_audio = True
            transcript_url, transcript_type = _extract_transcript(entry)
            episodes.append({
                "title": _clean(entry.get("title", "Unbekannte Folge")),
                "audio_url": audio_url,
                "episode_url": entry.get("link", ""),
                "pub_date": entry.get("published", ""),
                "duration_sec": _parse_duration(entry.get("itunes_duration", "")),
                "description": _clean(entry.get("summary", "")),
                "transcript_url": transcript_url,
                "transcript_type": transcript_type,
            })
        else:
            # Text/article entry (newsfeed)
            link = entry.get("link", "")
            if link:
                episodes.append({
                    "title": _clean(entry.get("title", "Unbekannter Artikel")),
                    "audio_url": "",
                    "episode_url": link,
                    "pub_date": entry.get("published", ""),
                    "duration_sec": 0,
                    "description": _clean(entry.get("summary", "")),
                    "transcript_url": "",
                    "transcript_type": "",
                })

    feed_type = "podcast" if has_audio else "newsfeed"
    return {"podcast": podcast, "episodes": episodes, "feed_type": feed_type}


# Transcript types we can parse (Podcast Index <podcast:transcript> tag)
_SUPPORTED_TRANSCRIPT = ("vtt", "srt", "json", "text", "plain", "html")


def _extract_transcript(entry):
    """Return (url, type) of an embedded transcript if the feed provides one
    via the Podcast Index <podcast:transcript> tag, else ('', '')."""
    t = entry.get("podcast_transcript")
    candidates = []
    if isinstance(t, dict):
        candidates = [t]
    elif isinstance(t, list):
        candidates = [x for x in t if isinstance(x, dict)]
    # Prefer machine-friendly formats: json > vtt > srt > text
    def rank(c):
        ty = (c.get("type", "") or "").lower()
        for i, fmt in enumerate(("json", "vtt", "srt", "text", "plain", "html")):
            if fmt in ty:
                return i
        return 99
    for c in sorted(candidates, key=rank):
        url = c.get("url", "")
        ty = (c.get("type", "") or "").lower()
        if url and any(fmt in ty for fmt in _SUPPORTED_TRANSCRIPT):
            return url, ty
        if url and (url.endswith(".vtt") or url.endswith(".srt") or url.endswith(".json")):
            return url, ty
    return "", ""


async def parse_opml(content: str) -> list:
    urls = re.findall(r'xmlUrl=["\']([^"\']+)["\']', content, re.IGNORECASE)
    return list(dict.fromkeys(urls))  # deduplicate, preserve order


async def _resolve_spotify(spotify_url: str) -> str:
    match = re.search(r"show/([a-zA-Z0-9]+)", spotify_url)
    if not match:
        raise ValueError("Konnte Spotify-Show-ID nicht extrahieren")

    show_id = match.group(1)
    candidates = [
        f"https://anchor.fm/s/{show_id}/podcast/rss",
        f"https://feeds.buzzsprout.com/{show_id}.rss",
    ]
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        for candidate in candidates:
            try:
                r = await client.get(candidate)
                if r.status_code == 200 and b"<rss" in r.content[:500]:
                    return candidate
            except Exception:
                continue

    raise ValueError(
        f"Spotify-RSS konnte nicht automatisch aufgelöst werden. "
        "Bitte suche den RSS-Feed manuell auf podcastindex.org"
    )


def _extract_audio(entry) -> Optional[str]:
    for enc in entry.get("enclosures", []):
        t = enc.get("type", "")
        if "audio" in t or enc.get("href", "").endswith(".mp3"):
            return enc.get("href") or enc.get("url")
    for link in entry.get("links", []):
        if "audio" in link.get("type", ""):
            return link.get("href")
    for media in entry.get("media_content", []):
        if "audio" in media.get("type", ""):
            return media.get("url")
    return None


def _get_artwork(feed) -> str:
    img = getattr(feed.feed, "image", None)
    if img:
        href = getattr(img, "href", None) or getattr(img, "url", None)
        if href:
            return href
    itunes = getattr(feed.feed, "itunes_image", None)
    if isinstance(itunes, dict):
        return itunes.get("href", "")
    return ""


def _parse_duration(s: str) -> int:
    if not s:
        return 0
    try:
        parts = s.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        return int(float(s))
    except Exception:
        return 0


def _clean(text: str) -> str:
    if not text:
        return ""
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:2000]
