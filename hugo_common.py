"""Shared helpers for Hugo features.

Both `daily_digest.py` (scheduled) and `hugo_bot.py` (long-running event
listener) import from here so they agree on env loading, log destination,
and how to look up Slack channels by name.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".hugo"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = STATE_DIR / "hugo.log"


def load_env() -> None:
    load_dotenv(PROJECT_DIR / ".env", override=True)


def setup_logging(logger_name: str) -> logging.Logger:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[
                logging.FileHandler(LOG_FILE, encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )
    return logging.getLogger(logger_name)


def resolve_channel_id(client, channel_ref: str) -> str | None:
    """Resolve a channel reference to its Slack channel ID.

    `#name` triggers a lookup via conversations.list (requires `channels:read`
    and `groups:read` scopes for public + private channels). Anything not
    starting with `#` is treated as a raw ID and returned unchanged.
    """
    if not channel_ref:
        return None
    if not channel_ref.startswith("#"):
        return channel_ref
    name = channel_ref[1:]
    cursor: str | None = None
    while True:
        resp = client.conversations_list(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
        )
        for ch in resp.get("channels", []):
            if ch.get("name") == name:
                return ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            return None


def notify_admin(client, message: str) -> None:
    """DM the configured admin user with `message`. Silent no-op if unset or DM fails.

    Used for crash alerts and ops messages. Failures are swallowed so an alert
    failure can't cascade into a second exception.
    """
    admin = os.environ.get("HUGO_ADMIN_USER_ID")
    if not admin:
        return
    try:
        resp = client.conversations_open(users=admin)
        dm = resp["channel"]["id"]
        if len(message) > 3500:
            message = message[:3500] + "\n... (truncated)"
        client.chat_postMessage(channel=dm, text=message)
    except Exception:
        pass
