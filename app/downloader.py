import asyncio
import os
import uuid
from pathlib import Path

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/app/downloads"))


async def download_audio(url: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    output_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{output_id}.%(ext)s")

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
