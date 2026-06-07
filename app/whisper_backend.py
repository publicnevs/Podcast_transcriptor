"""Local transcription via faster-whisper (CTranslate2).

Optional backend. Only imported when TRANSCRIPTION_BACKEND=whisper, so the
heavy dependency stays optional for Gemini-only installs.

Hardware note (Synology DS218+ / Intel Celeron J3355, no AVX2):
  - 'tiny'  ~ near real-time, modest quality
  - 'base'  ~ 2-3x real-time, decent quality   <- recommended ceiling
  - 'small' and up are impractically slow on this CPU
The model is loaded once and cached for the process lifetime.
"""
import logging
import os

logger = logging.getLogger(__name__)

_model_cache = {}


def _get_model(model_size: str):
    if model_size in _model_cache:
        return _model_cache[model_size]
    from faster_whisper import WhisperModel
    threads = int(os.getenv("WHISPER_THREADS", "0")) or os.cpu_count() or 2
    logger.info(f"Loading faster-whisper model '{model_size}' (int8, {threads} threads)...")
    model = WhisperModel(
        model_size,
        device="cpu",
        compute_type="int8",
        cpu_threads=threads,
        download_root=os.getenv("WHISPER_CACHE", "/app/data/whisper_models"),
    )
    _model_cache[model_size] = model
    return model


def transcribe(audio_path, model_size: str = "base") -> dict:
    model = _get_model(model_size)
    logger.info(f"Whisper transcribing {audio_path}...")
    segments_iter, info = model.transcribe(
        str(audio_path),
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    segments = []
    for seg in segments_iter:
        segments.append({
            "time": _fmt_time(seg.start),
            "speaker": "",  # whisper has no diarization
            "text": seg.text.strip(),
        })

    return {
        "language": info.language or "",
        "speakers": [],
        "segments": segments,
    }


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"
