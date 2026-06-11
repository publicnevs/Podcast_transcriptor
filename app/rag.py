"""Semantic search over the whole library ("Frag deine Bibliothek").

Every processed episode (podcast, newsfeed article, or newsletter) is chunked
and embedded once; a question is embedded at query time and matched against the
stored vectors with cosine similarity (pure Python — personal-scale corpora of a
few thousand chunks rank in well under a second). The top chunks are handed to
Gemini Flash to compose a cited answer.

Vectors are stored as packed float32 bytes in episode_chunks.vector to stay
dependency-free (no numpy, no vector extension).
"""
import json
import logging
import math
from array import array

import aiosqlite

from .database import DB_PATH
from .transcriber import embed_texts, answer_from_context

logger = logging.getLogger(__name__)

_CHUNK_CHARS = 1500


def _pack(vec: list) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


def _chunk_segments(segments: list) -> list:
    """Group transcript segments into ~_CHUNK_CHARS pieces, keeping the start
    time of each piece for citation deep-links."""
    chunks, buf, start = [], [], None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if start is None:
            start = seg.get("time", "00:00:00")
        buf.append(text)
        if sum(len(t) for t in buf) >= _CHUNK_CHARS:
            chunks.append({"start_time": start, "text": " ".join(buf)})
            buf, start = [], None
    if buf:
        chunks.append({"start_time": start or "00:00:00", "text": " ".join(buf)})
    return chunks


async def index_episode(episode_id: int, segments: list):
    """(Re)build embeddings for one episode. Idempotent: clears prior chunks."""
    chunks = _chunk_segments(segments)
    if not chunks:
        return
    vectors = await embed_texts([c["text"] for c in chunks])
    if not vectors:  # no API key → indexing silently disabled
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM episode_chunks WHERE episode_id=?", (episode_id,))
        for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            await db.execute(
                """INSERT INTO episode_chunks (episode_id, chunk_idx, start_time, text, vector)
                   VALUES (?, ?, ?, ?, ?)""",
                (episode_id, idx, chunk["start_time"], chunk["text"], _pack(vec)),
            )
        await db.commit()


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def answer(question: str, k: int = 8) -> dict:
    """Embed the question, rank all chunks, and let Gemini answer with sources."""
    q_vecs = await embed_texts([question])
    if not q_vecs:
        return {"answer": "", "sources": [],
                "error": "Kein Gemini API Key konfiguriert."}
    qv = q_vecs[0]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT c.id, c.episode_id, c.start_time, c.text, c.vector,
                   e.title AS episode_title, p.title AS podcast_title
            FROM episode_chunks c
            JOIN episodes e ON e.id = c.episode_id
            LEFT JOIN podcasts p ON p.id = e.podcast_id
        """) as cur:
            rows = await cur.fetchall()

    if not rows:
        return {"answer": "", "sources": [],
                "error": "Noch keine durchsuchbaren Inhalte. Bitte zuerst Index aufbauen."}

    scored = []
    for r in rows:
        score = _cosine(qv, _unpack(r["vector"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:k]

    context_parts, sources = [], []
    for i, (score, r) in enumerate(top, start=1):
        context_parts.append(f"[{i}] ({r['podcast_title']} — {r['episode_title']}) "
                             f"{r['text']}")
        sources.append({
            "ref": i,
            "episode_id": r["episode_id"],
            "episode_title": r["episode_title"],
            "podcast_title": r["podcast_title"],
            "start_time": r["start_time"],
            "snippet": (r["text"][:240] + "…") if len(r["text"]) > 240 else r["text"],
            "score": round(float(score), 3),
        })
    context = "\n\n".join(context_parts)
    text = await answer_from_context(question, context)
    return {"answer": text, "sources": sources}


async def reindex_all() -> dict:
    """Embed all done episodes that have no chunks yet. Returns counts."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT e.id, t.segments_json
            FROM episodes e
            JOIN transcripts t ON t.episode_id = e.id
            WHERE e.status = 'done'
              AND e.id NOT IN (SELECT DISTINCT episode_id FROM episode_chunks)
        """) as cur:
            rows = await cur.fetchall()

    indexed = 0
    for r in rows:
        try:
            segments = json.loads(r["segments_json"] or "[]")
            await index_episode(r["id"], segments)
            indexed += 1
        except Exception as e:
            logger.warning(f"Reindex episode {r['id']} failed: {e}")
    return {"indexed": indexed, "pending_before": len(rows)}


async def stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM episode_chunks") as cur:
            chunks = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(DISTINCT episode_id) FROM episode_chunks") as cur:
            episodes = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM episodes WHERE status='done'") as cur:
            done = (await cur.fetchone())[0]
    return {"chunks": chunks, "indexed_episodes": episodes, "done_episodes": done}
