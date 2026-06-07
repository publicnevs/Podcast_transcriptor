"""Canonical topic tagging with alias-based de-duplication.

Auto-tags reduce a vocabulary explosion ("Prompting", "Prompts",
"Prompt-Engineering") down to one canonical tag via:
  1. slug normalization
  2. alias table lookup
  3. exact slug match
  4. fuzzy merge (difflib) -> records an alias
  5. otherwise create a new canonical tag
"""
import re
import unicodedata
from difflib import SequenceMatcher

import aiosqlite

from .database import DB_PATH

_FUZZY_THRESHOLD = 0.82
# Too-generic labels we never want as tags
_STOPWORDS = {"ki", "ai", "technologie", "technology", "podcast", "news",
              "update", "thema", "themen", "folge", "episode"}


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")  # strip diacritics
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


async def _find_canonical(db, slug: str, label: str):
    """Return existing tag_id for this slug/label or None."""
    # 2. alias lookup
    async with db.execute("SELECT tag_id FROM tag_aliases WHERE alias_slug=?", (slug,)) as c:
        row = await c.fetchone()
        if row:
            return row[0]
    # 3. exact slug
    async with db.execute("SELECT id FROM tags WHERE slug=?", (slug,)) as c:
        row = await c.fetchone()
        if row:
            return row[0]
    # 4. fuzzy merge against existing tags
    async with db.execute("SELECT id, slug FROM tags") as c:
        existing = await c.fetchall()
    for tid, tslug in existing:
        if slug == tslug:
            return tid
        if slug in tslug or tslug in slug:
            await db.execute("INSERT OR IGNORE INTO tag_aliases (alias_slug, tag_id) VALUES (?, ?)",
                             (slug, tid))
            return tid
        if SequenceMatcher(None, slug, tslug).ratio() >= _FUZZY_THRESHOLD:
            await db.execute("INSERT OR IGNORE INTO tag_aliases (alias_slug, tag_id) VALUES (?, ?)",
                             (slug, tid))
            return tid
    return None


async def upsert_tags(episode_id: int, raw_tags: list) -> int:
    """raw_tags: [{"label": str, "kind": str}]. Returns number of tags linked."""
    if not raw_tags:
        return 0
    linked = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for t in raw_tags:
            label = (t.get("label") or "").strip()
            if not label:
                continue
            slug = slugify(label)
            if not slug or slug in _STOPWORDS:
                continue
            kind = (t.get("kind") or "topic").strip() or "topic"

            tag_id = await _find_canonical(db, slug, label)
            if tag_id is None:
                cur = await db.execute(
                    "INSERT INTO tags (slug, label, kind) VALUES (?, ?, ?)",
                    (slug, label, kind),
                )
                tag_id = cur.lastrowid

            await db.execute(
                "INSERT OR IGNORE INTO episode_tags (episode_id, tag_id, source) VALUES (?, ?, 'auto')",
                (episode_id, tag_id),
            )
            linked += 1
        # refresh denormalized counts
        await db.execute("""
            UPDATE tags SET episode_count =
                (SELECT COUNT(*) FROM episode_tags WHERE episode_tags.tag_id = tags.id)
        """)
        await db.commit()
    return linked


async def add_manual_tag(episode_id: int, label: str) -> int:
    return await upsert_tags(episode_id, [{"label": label, "kind": "topic"}])


async def remove_tag(episode_id: int, tag_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM episode_tags WHERE episode_id=? AND tag_id=?",
                         (episode_id, tag_id))
        await db.execute(
            "UPDATE tags SET episode_count=(SELECT COUNT(*) FROM episode_tags WHERE tag_id=tags.id) WHERE id=?",
            (tag_id,))
        await db.commit()


async def rename_tag(tag_id: int, new_label: str):
    """Rename a tag; if the new slug collides with another tag, merge into it."""
    new_slug = slugify(new_label)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM tags WHERE slug=? AND id!=?", (new_slug, tag_id)) as c:
            other = await c.fetchone()
        if other:
            target = other[0]
            # move episode links, then alias + delete old
            await db.execute(
                "INSERT OR IGNORE INTO episode_tags (episode_id, tag_id, source) "
                "SELECT episode_id, ?, source FROM episode_tags WHERE tag_id=?", (target, tag_id))
            await db.execute("DELETE FROM episode_tags WHERE tag_id=?", (tag_id,))
            await db.execute("UPDATE tag_aliases SET tag_id=? WHERE tag_id=?", (target, tag_id))
            async with db.execute("SELECT slug FROM tags WHERE id=?", (tag_id,)) as c:
                old = await c.fetchone()
            if old:
                await db.execute("INSERT OR IGNORE INTO tag_aliases (alias_slug, tag_id) VALUES (?, ?)",
                                 (old[0], target))
            await db.execute("DELETE FROM tags WHERE id=?", (tag_id,))
            await db.execute(
                "UPDATE tags SET episode_count=(SELECT COUNT(*) FROM episode_tags WHERE tag_id=tags.id) WHERE id=?",
                (target,))
        else:
            await db.execute("UPDATE tags SET label=?, slug=? WHERE id=?", (new_label, new_slug, tag_id))
        await db.commit()
