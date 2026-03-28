"""notifications.py — Telegram push notifications (optional)."""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)


def send_telegram(message: str) -> bool:
    """Send message via Telegram Bot API. Returns True on success."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        return resp.status_code == 200
    except Exception:
        logger.exception("Telegram send failed")
        return False
