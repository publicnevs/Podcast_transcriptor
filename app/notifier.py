import logging

import httpx

from .database import get_setting

logger = logging.getLogger(__name__)


async def send_notification(title: str, message: str, priority: str = "default"):
    ntfy_topic = await get_setting("ntfy_topic")
    if not ntfy_topic:
        return
    ntfy_url = (await get_setting("ntfy_url")) or "https://ntfy.sh"
    url = f"{ntfy_url.rstrip('/')}/{ntfy_topic}"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                url,
                content=message.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8").decode("latin-1", errors="replace"),
                    "Priority": priority,
                    "Tags": "headphones",
                },
                timeout=8,
            )
    except Exception as e:
        logger.debug(f"ntfy notification failed: {e}")
