"""Newsletter inbox via IMAP.

Fetches messages from a dedicated mailbox (e.g. a hosting.de account that
subscribes to various newsletters), groups them per sender into pseudo-podcasts
(feed_type='newsletter'), and hands each mail to the standard episode pipeline
so it gets summarised + tagged exactly like a newsfeed article.

Blocking IMAP/email work (stdlib imaplib + email) runs in a thread executor,
mirroring feed_parser/transcriber. Dedup relies on the Message-ID stored in
episodes.episode_url (unique index ux_ep_url) — the IMAP SINCE search only
bounds the scan window.
"""
import asyncio
import email
import hashlib
import imaplib
import logging
import re
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

from .database import DB_PATH, get_setting, set_setting

logger = logging.getLogger(__name__)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _imap_date(d: datetime) -> str:
    """IMAP SEARCH SINCE wants 'DD-Mon-YYYY' with an English month abbrev."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _decode(value: str) -> str:
    """Decode a possibly RFC2047-encoded header into a plain string."""
    if not value:
        return ""
    parts = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, ValueError):
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def _html_to_text(html: str) -> str:
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html)
        for tag in tree.iter("script", "style"):
            tag.clear()
        text = tree.text_content()
        return re.sub(r"\s+", " ", text).strip()[:8000]
    except Exception:
        # Fallback (no lxml): drop script/style blocks, strip tags, unescape.
        import html as html_mod
        stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                          flags=re.IGNORECASE | re.DOTALL)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        return re.sub(r"\s+", " ", html_mod.unescape(stripped)).strip()[:8000]


def _extract_body(msg) -> str:
    """Prefer text/plain; fall back to stripped text/html."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, ValueError):
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, ValueError):
            text = payload.decode("utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            html = text
        else:
            plain = text
    if plain.strip():
        return plain.strip()[:8000]
    if html.strip():
        return _html_to_text(html)
    return ""


def _fetch_emails_sync(host: str, port: int, user: str, password: str,
                       since: datetime) -> list:
    conn = imaplib.IMAP4_SSL(host, port)
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("SEARCH", None, "SINCE", _imap_date(since))
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        out = []
        for uid in uids:
            typ, msg_data = conn.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = _decode(msg.get("Subject", "")) or "(ohne Betreff)"
            sender_name, sender_email = parseaddr(_decode(msg.get("From", "")))
            sender_email = (sender_email or "unknown@unknown").lower()
            sender_name = sender_name or sender_email.split("@")[0]
            try:
                pub_date = parsedate_to_datetime(
                    msg.get("Date", "")).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pub_date = ""
            message_id = (msg.get("Message-ID", "") or "").strip()
            if not message_id:
                message_id = "newsletter-mid:" + hashlib.sha1(
                    f"{sender_email}|{subject}|{pub_date}".encode()).hexdigest()
            body = _extract_body(msg)
            out.append({
                "sender_email": sender_email,
                "sender_name": sender_name,
                "subject": subject,
                "message_id": message_id,
                "pub_date": pub_date,
                "body": body,
            })
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _test_connection_sync(host: str, port: int, user: str, password: str):
    conn = imaplib.IMAP4_SSL(host, port)
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


async def test_connection(host: str, port: int, user: str, password: str) -> dict:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _test_connection_sync, host, int(port or 993), user, password)
    return {"ok": True}


async def _settings() -> dict:
    return {
        "enabled": (await get_setting("newsletter_enabled")) == "1",
        "host": (await get_setting("newsletter_imap_host")) or "imap.hosting.de",
        "port": int((await get_setting("newsletter_imap_port")) or "993"),
        "user": await get_setting("newsletter_imap_user"),
        "password": await get_setting("newsletter_imap_password"),
        "last_checked": await get_setting("newsletter_last_checked"),
    }


async def check_inbox() -> int:
    """Fetch new newsletter mails, file them per sender, and queue processing.
    Returns the number of newly inserted episodes."""
    import aiosqlite
    from . import processor
    from .notifier import send_notification

    cfg = await _settings()
    if not cfg["enabled"] or not cfg["user"] or not cfg["password"]:
        return 0

    # Scan window: from last check minus a safety margin; first run = 30 days.
    if cfg["last_checked"]:
        try:
            base = datetime.strptime(cfg["last_checked"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            base = datetime.now()
        since = base - timedelta(days=3)
    else:
        since = datetime.now() - timedelta(days=30)

    loop = asyncio.get_event_loop()
    try:
        mails = await loop.run_in_executor(
            None, _fetch_emails_sync, cfg["host"], cfg["port"],
            cfg["user"], cfg["password"], since)
    except Exception as e:
        logger.error(f"Newsletter inbox check failed: {e}")
        raise

    # Group by sender so each newsletter becomes its own pseudo-podcast.
    by_sender: dict[str, list] = {}
    for m in mails:
        by_sender.setdefault(m["sender_email"], []).append(m)

    inserted_total = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for sender_email, items in by_sender.items():
            rss_url = f"newsletter:{sender_email}"
            title = items[0]["sender_name"] or sender_email
            await db.execute(
                """INSERT OR IGNORE INTO podcasts (title, rss_url, feed_type,
                       auto_transcribe, check_interval_hours)
                   VALUES (?, ?, 'newsletter', 1, 24)""",
                (title, rss_url),
            )
            async with db.execute(
                "SELECT id FROM podcasts WHERE rss_url=?", (rss_url,)) as cur:
                row = await cur.fetchone()
            if not row:
                continue
            podcast_id = row["id"]
            eps = [{
                "title": m["subject"],
                "audio_url": "",
                "episode_url": m["message_id"],
                "pub_date": m["pub_date"],
                "duration_sec": 0,
                "description": m["body"],
            } for m in items]
            inserted = await processor.insert_new_episodes(
                db, podcast_id, eps,
                feed_type="newsletter", full_text_extraction=False,
                auto_transcribe=True,
            )
            if inserted:
                await db.execute(
                    "UPDATE podcasts SET last_checked=CURRENT_TIMESTAMP WHERE id=?",
                    (podcast_id,))
            inserted_total += inserted
        await db.commit()

    await set_setting("newsletter_last_checked",
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    if inserted_total > 0:
        await send_notification(
            "PodScribe — Neue Newsletter",
            f"{inserted_total} neue Newsletter-Ausgabe(n) wurden hinzugefügt.",
            click_path="/",
        )
        await processor.process_queued()

    return inserted_total
