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
from .transcriber import embed_texts, answer_from_context, answer_chat_from_context

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


async def _retrieve(query: str, k: int):
    """Embed the query and rank all chunks. Returns (context, sources, error)."""
    q_vecs = await embed_texts([query])
    if not q_vecs:
        return None, None, "Kein Gemini API Key konfiguriert."
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
        return None, None, "Noch keine durchsuchbaren Inhalte. Bitte zuerst Index aufbauen."

    scored = [(_cosine(qv, _unpack(r["vector"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:k]

    context_parts, sources = [], []
    for i, (score, r) in enumerate(top, start=1):
        context_parts.append(f"[{i}] ({r['podcast_title']} — {r['episode_title']}) {r['text']}")
        sources.append({
            "ref": i,
            "episode_id": r["episode_id"],
            "episode_title": r["episode_title"],
            "podcast_title": r["podcast_title"],
            "start_time": r["start_time"],
            "snippet": (r["text"][:240] + "…") if len(r["text"]) > 240 else r["text"],
            "score": round(float(score), 3),
        })
    return "\n\n".join(context_parts), sources, None


async def answer(question: str, k: int = 8) -> dict:
    """Embed the question, rank all chunks, and let Gemini answer with sources."""
    context, sources, error = await _retrieve(question, k)
    if error:
        return {"answer": "", "sources": [], "error": error}
    text = await answer_from_context(question, context)
    return {"answer": text, "sources": sources}


async def chat(messages: list, k: int = 8) -> dict:
    """Multi-turn variant of answer(). `messages` is a list of
    {role:'user'|'assistant', content:str}; retrieval uses the latest question
    (with the previous user turn for context), the answer is history-aware."""
    user_turns = [m for m in messages if m.get("role") == "user" and m.get("content")]
    if not user_turns:
        return {"answer": "", "sources": []}
    query = user_turns[-1]["content"]
    if len(user_turns) >= 2:  # carry a little prior context into retrieval
        query = user_turns[-2]["content"] + " " + query

    context, sources, error = await _retrieve(query, k)
    if error:
        return {"answer": "", "sources": [], "error": error}

    history = "\n".join(
        f"{'Nutzer' if m.get('role') == 'user' else 'Assistent'}: {m.get('content', '')}"
        for m in messages[-8:]
    )
    text = await answer_chat_from_context(history, context)
    return {"answer": text, "sources": sources}


async def related(episode_id: int, limit: int = 6) -> list:
    """Episodes related to `episode_id` by shared tags + embedding similarity.
    Falls back gracefully to tags-only when the embedding index is empty."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT tag_id FROM episode_tags WHERE episode_id=?", (episode_id,)) as cur:
            my_tags = [r["tag_id"] for r in await cur.fetchall()]

        tag_overlap: dict = {}
        if my_tags:
            qmarks = ",".join("?" * len(my_tags))
            async with db.execute(f"""
                SELECT et.episode_id AS eid, COUNT(*) AS shared
                FROM episode_tags et
                JOIN episodes e ON e.id = et.episode_id
                WHERE et.tag_id IN ({qmarks}) AND et.episode_id != ? AND e.status='done'
                GROUP BY et.episode_id
            """, (*my_tags, episode_id)) as cur:
                for r in await cur.fetchall():
                    tag_overlap[r["eid"]] = r["shared"]

        async with db.execute("SELECT episode_id, vector FROM episode_chunks") as cur:
            chunk_rows = await cur.fetchall()

    # Mean chunk vector per episode (componentwise average).
    sums: dict = {}
    counts: dict = {}
    for r in chunk_rows:
        eid, v = r["episode_id"], _unpack(r["vector"])
        if eid not in sums:
            sums[eid] = array("f", v)
        else:
            s = sums[eid]
            for i in range(len(s)):
                s[i] += v[i]
        counts[eid] = counts.get(eid, 0) + 1
    means = {eid: array("f", [x / counts[eid] for x in s]) for eid, s in sums.items()}

    sim: dict = {}
    if episode_id in means:
        mv = means[episode_id]
        for eid, vec in means.items():
            if eid != episode_id:
                sim[eid] = _cosine(mv, vec)

    cand = set(tag_overlap) | set(sim)
    if not cand:
        return []
    scored = sorted(
        ((tag_overlap.get(eid, 0) + 2.0 * sim.get(eid, 0.0), eid) for eid in cand),
        reverse=True,
    )[:limit]
    ids = [eid for _, eid in scored]
    if not ids:
        return []

    qmarks = ",".join("?" * len(ids))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"""
            SELECT e.id, e.title, p.id AS podcast_id, p.title AS podcast_title,
                   p.artwork_url, p.feed_type
            FROM episodes e LEFT JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.id IN ({qmarks})
        """, ids) as cur:
            meta = {r["id"]: r for r in await cur.fetchall()}

    out = []
    for score, eid in scored:
        r = meta.get(eid)
        if not r:
            continue
        out.append({
            "episode_id": eid,
            "title": r["title"],
            "podcast_id": r["podcast_id"],
            "podcast_title": r["podcast_title"],
            "artwork_url": r["artwork_url"],
            "feed_type": r["feed_type"],
            "shared_tags": tag_overlap.get(eid, 0),
            "score": round(float(score), 3),
        })
    return out


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
