"""Outbound email via SMTP (digest delivery).

Sends generated digests to a configured recipient. Uses stdlib smtplib +
email.message — blocking calls run in a thread executor, mirroring the rest of
the codebase. No new dependency.
"""
import asyncio
import logging
import smtplib
from email.message import EmailMessage

from .database import get_setting

logger = logging.getLogger(__name__)


def _send_sync(host: str, port: int, user: str, password: str,
               to_addr: str, subject: str, html: str, text: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(text or "Diese Nachricht benötigt einen HTML-fähigen Client.")
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


async def _smtp_settings() -> dict:
    return {
        "host": (await get_setting("smtp_host")) or "smtp.hosting.de",
        "port": int((await get_setting("smtp_port")) or "465"),
        "user": await get_setting("smtp_user"),
        "password": await get_setting("smtp_password"),
        "to": await get_setting("digest_email_to"),
        "enabled": (await get_setting("digest_email_enabled")) == "1",
    }


async def send_email(to_addr: str, subject: str, html: str, text: str = "") -> dict:
    cfg = await _smtp_settings()
    if not cfg["user"] or not cfg["password"]:
        raise RuntimeError("SMTP nicht konfiguriert (Benutzer/Passwort fehlt).")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _send_sync, cfg["host"], cfg["port"], cfg["user"],
        cfg["password"], to_addr or cfg["to"], subject, html, text)
    return {"ok": True}


async def test_connection(host: str, port: int, user: str, password: str) -> dict:
    loop = asyncio.get_event_loop()

    def _login():
        with smtplib.SMTP_SSL(host, int(port or 465), timeout=20) as smtp:
            smtp.login(user, password)

    await loop.run_in_executor(None, _login)
    return {"ok": True}


async def maybe_email_digest(digest_id: int, title: str, html: str, tldr: str = "",
                             to: str = ""):
    """Send a finished digest if email delivery is enabled. Best-effort.

    `to` overrides the configured recipient (used by the Tageszeitung, which may
    have its own address); falls back to the digest email recipient otherwise."""
    cfg = await _smtp_settings()
    recipient = (to or cfg["to"]).strip()
    if not cfg["enabled"] or not recipient:
        return
    try:
        await send_email(recipient, title or "PodScribe Redaktion", html, tldr)
        logger.info(f"Digest {digest_id} emailed to {recipient}")
    except Exception as e:
        logger.error(f"Digest {digest_id} email failed: {e}")
