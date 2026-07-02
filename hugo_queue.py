"""Hugo's reading queue.

A single queue, two writers:
- The auto-curator (RSS feeds → Claude relevance ranking → top N/day)
- Manual adds from Slack (:bookmark: reaction or `@Hugo queue <url>`)

The digest reads pending entries, summarizes them, posts, then moves them
to `posted` so they're not re-summarized.

Queue file: ~/.hugo/queue.json
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from hugo_common import STATE_DIR

QUEUE_FILE = STATE_DIR / "queue.json"


def url_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue() -> dict:
    if QUEUE_FILE.exists():
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    return {"pending": [], "posted": {}}


def save_queue(queue: dict) -> None:
    QUEUE_FILE.write_text(
        json.dumps(queue, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def is_known(url: str, queue: Optional[dict] = None) -> bool:
    """True if the URL is already pending OR already posted."""
    if queue is None:
        queue = load_queue()
    if url_id(url) in queue.get("posted", {}):
        return True
    return any(p.get("url") == url for p in queue.get("pending", []))


def add_to_queue(
    url: str,
    title: Optional[str] = None,
    source: str = "manual",
) -> bool:
    """Add a URL to the pending queue. Returns True if newly added, False if already known."""
    queue = load_queue()
    if is_known(url, queue):
        return False
    queue.setdefault("pending", []).append(
        {
            "url": url,
            "title": title or url,
            "source": source,
            "added_at": _now_iso(),
        }
    )
    save_queue(queue)
    return True


def get_pending() -> list[dict]:
    queue = load_queue()
    return list(queue.get("pending", []))


def mark_posted(urls: list[str]) -> None:
    """Move the given URLs from pending → posted."""
    queue = load_queue()
    pending = queue.get("pending", [])
    posted = queue.setdefault("posted", {})
    posted_set = set(urls)
    timestamp = _now_iso()

    new_pending = []
    for entry in pending:
        if entry["url"] in posted_set:
            posted[url_id(entry["url"])] = {
                "url": entry["url"],
                "title": entry.get("title", entry["url"]),
                "source": entry.get("source", "manual"),
                "posted_at": timestamp,
            }
        else:
            new_pending.append(entry)

    queue["pending"] = new_pending
    queue["posted"] = posted
    save_queue(queue)
