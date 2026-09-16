"""Failure notifications: one direct HTTP POST to an ntfy-compatible URL. No `apprise`
abstraction — a single channel doesn't need one (WORK_FOUNDATION.md §2).
"""

from __future__ import annotations

import httpx
from loguru import logger

from app.config import get_settings


def alert(message: str) -> None:
    """Best-effort: a notification failure must never mask the original error."""
    url = get_settings().alert_url
    if not url:
        logger.warning("ALERT_URL not set, dropping alert: {}", message)
        return
    try:
        httpx.post(url, content=message.encode("utf-8"), timeout=10.0)
    except httpx.HTTPError:
        logger.exception("failed to send alert: {}", message)
