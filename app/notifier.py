import logging

import httpx

from .database import get_setting

logger = logging.getLogger(__name__)


async def send_notification(title: str, message: str, priority: str = "default",
                            click_path: str = ""):
    """Send an ntfy push. When click_path is given and a public_base_url is
    configured, the notification becomes tappable (ntfy 'Click' header), opening
    the app at that path (e.g. '/episode/42' or '/digest/7')."""
    ntfy_topic = await get_setting("ntfy_topic")
    if not ntfy_topic:
        return
    ntfy_url = (await get_setting("ntfy_url")) or "https://ntfy.sh"
    url = f"{ntfy_url.rstrip('/')}/{ntfy_topic}"
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", errors="replace"),
        "Priority": priority,
        "Tags": "headphones",
    }
    if click_path:
        base = (await get_setting("public_base_url")).rstrip("/")
        if base:
            headers["Click"] = f"{base}/{click_path.lstrip('/')}"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                url,
                content=message.encode("utf-8"),
                headers=headers,
                timeout=8,
            )
    except Exception as e:
        logger.debug(f"ntfy notification failed: {e}")
