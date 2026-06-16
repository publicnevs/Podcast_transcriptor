import asyncio
import os
import uuid
from pathlib import Path

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/app/downloads"))


class AudioTooLongError(Exception):
    """Raised when a source exceeds the configured max duration — a deliberate
    skip, not a download failure, so callers should not retry as an article."""


async def download_audio(url: str, max_minutes: int = 0) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    output_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{output_id}.%(ext)s")

    # Pre-check duration so we don't spend minutes downloading a multi-hour stream
    # only to blow past Gemini's 20 MB inline limit at transcription time.
    if max_minutes and max_minutes > 0:
        loop = asyncio.get_event_loop()
        duration = await loop.run_in_executor(None, _probe_duration, url)
        if duration and duration > max_minutes * 60:
            raise AudioTooLongError(
                f"Audio zu lang ({int(duration // 60)} Min, Limit {max_minutes} Min) — "
                "übersprungen. Limit in den Einstellungen anpassbar.")

    # 32 kbps mono mp3 keeps long podcasts under Gemini's 20 MB inline limit
    # (≈14 MB for a 60-min episode) — speech intelligibility stays excellent.
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "32",
        }],
        "postprocessor_args": ["-ac", "1", "-ar", "16000"],   # mono, 16 kHz
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
        "retries": 3,
    }

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _download_sync, url, ydl_opts)

    mp3_path = DOWNLOAD_DIR / f"{output_id}.mp3"
    if mp3_path.exists():
        return mp3_path

    # fallback: find any file with that id
    for f in DOWNLOAD_DIR.glob(f"{output_id}.*"):
        return f

    raise FileNotFoundError(f"Download produced no output for: {url}")


def _download_sync(url: str, opts: dict):
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _probe_duration(url: str) -> float:
    """Best-effort: return media duration in seconds without downloading (0 if
    unknown). Raises on truly unsupported URLs so the caller can fall back."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                           "socket_timeout": 30}) as ydl:
        info = ydl.extract_info(url, download=False)
    return float(info.get("duration") or 0)
