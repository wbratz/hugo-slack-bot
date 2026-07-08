"""Hugo — daily reading digest.

Posts a curated digest to HUGO_DIGEST_CHANNEL. Reads pending entries from the
queue (`~/.hugo/queue.json`), summarizes them, posts, and clears them.

Selection:
- Manual saves (`:bookmark:` / `@Hugo queue`) always post — you chose them.
- Curator picks are ranked by the score the curator stored; the best
  HUGO_DIGEST_MAX_POSTS (default 3) post, the rest are discarded.

Cadence:
- Skips Saturday and Sunday (pass --force to override). The curator keeps
  running daily, so weekend finds accumulate and Monday's digest picks the
  best few from the whole weekend pool.

Designed to be invoked once per day by DSM Task Scheduler.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import Anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from hugo_common import load_env, notify_admin, resolve_channel_id, setup_logging
from hugo_queue import discard_pending, get_pending, mark_posted
from hugo_summarize import fetch_article, summarize_article

load_env()
log = setup_logging("hugo.digest")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
HUGO_DIGEST_CHANNEL = os.environ.get("HUGO_DIGEST_CHANNEL", "#ai-summaries")
HUGO_DIGEST_MAX_POSTS = int(os.environ.get("HUGO_DIGEST_MAX_POSTS", "3"))
HUGO_TZ = os.environ.get("HUGO_TZ", "UTC")


def _now() -> datetime:
    try:
        return datetime.now(ZoneInfo(HUGO_TZ))
    except Exception:
        log.warning(f"invalid HUGO_TZ={HUGO_TZ!r}; falling back to system local time")
        return datetime.now()


def _is_manual(entry: dict) -> bool:
    return str(entry.get("source", "")).startswith("manual")


def _score(entry: dict) -> int:
    s = entry.get("score")
    return s if isinstance(s, int) else -1


def slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def post_to_slack(slack: WebClient, channel_id: str, summaries: list[dict]) -> None:
    now = _now()
    today = now.strftime(f"%A, %B {now.day}, %Y")
    header = f"*:books: Hugo's Reading Digest — {today}*"

    lines = [header, ""]
    for item in summaries:
        title = slack_escape(item["title"] or item["url"])
        url = item["url"]
        source = item.get("source") or ""
        source_tag = f"  _{slack_escape(source)}_" if source else ""
        lines.append(f"*<{url}|{title}>*{source_tag}")
        lines.append(item["summary"])
        lines.append("")

    text = "\n".join(lines).rstrip()
    try:
        slack.chat_postMessage(channel=channel_id, text=text, mrkdwn=True)
        log.info(f"posted digest with {len(summaries)} article(s) to {channel_id}")
    except SlackApiError as exc:
        log.error(f"slack post failed: {exc.response.get('error')}")
        sys.exit(2)


def _select(pending: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split pending into (to_post, to_drop).

    All manual saves post. Curator picks post the best HUGO_DIGEST_MAX_POSTS by
    stored score; the rest drop. Curator picks lead (highest score first),
    followed by manual saves.
    """
    manual = [e for e in pending if _is_manual(e)]
    curator = sorted(
        (e for e in pending if not _is_manual(e)),
        key=_score,
        reverse=True,
    )
    selected_curator = curator[:HUGO_DIGEST_MAX_POSTS]
    dropped = curator[HUGO_DIGEST_MAX_POSTS:]
    to_post = selected_curator + manual
    return to_post, dropped


def _run(force: bool) -> int:
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return 1
    if not SLACK_BOT_TOKEN:
        log.error("SLACK_BOT_TOKEN not set")
        return 1

    now = _now()
    if now.weekday() >= 5 and not force:  # 5 = Sat, 6 = Sun
        log.info(
            f"{now:%A} — weekend, not posting. Curator still accumulates; "
            "Monday's digest picks the best of the weekend pool. (--force to override.)"
        )
        return 0

    slack = WebClient(token=SLACK_BOT_TOKEN)
    channel_id = resolve_channel_id(slack, HUGO_DIGEST_CHANNEL)
    if not channel_id:
        log.error(
            f"could not resolve channel '{HUGO_DIGEST_CHANNEL}' — "
            "does the bot have channels:read scope and access to the channel?"
        )
        return 1

    pending = get_pending()
    if not pending:
        log.info("nothing pending — skipping post")
        return 0

    to_post, dropped = _select(pending)
    manual_n = sum(1 for e in to_post if _is_manual(e))
    log.info(
        f"pending {len(pending)}: posting {len(to_post)} "
        f"({len(to_post) - manual_n} curator + {manual_n} manual), dropping {len(dropped)}"
    )

    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    summaries: list[dict] = []
    posted_urls: list[str] = []

    for entry in to_post:
        url = entry["url"]
        title = entry.get("title") or url
        source = entry.get("source", "")
        log.info(f"processing: {title}")
        article = fetch_article(url)
        if not article:
            log.warning(f"  fetch/extract failed for {url}")
            summary = "- _(could not fetch or extract article content)_"
            display_title = title
        else:
            display_title = title if (title and title != url) else (article.title or url)
            try:
                summary = summarize_article(anthropic_client, display_title, url, article.text)
            except Exception as exc:
                log.warning(f"  summarize failed: {exc}")
                summary = f"- _(summary failed: {exc})_"
        summaries.append(
            {
                "url": url,
                "title": display_title,
                "summary": summary,
                "source": source,
            }
        )
        posted_urls.append(url)

    post_to_slack(slack, channel_id, summaries)
    mark_posted(posted_urls)
    discard_pending([e["url"] for e in dropped])
    if dropped:
        log.info(f"dropped {len(dropped)} lower-ranked curator item(s) from the pool")
    return 0


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--force",
        action="store_true",
        help="post even on weekends (normally Sat/Sun are skipped)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return _run(force=args.force)
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        log.exception("digest crashed")
        if SLACK_BOT_TOKEN:
            try:
                slack = WebClient(token=SLACK_BOT_TOKEN)
                notify_admin(slack, f":rotating_light: Hugo digest crashed\n```\n{tb}\n```")
            except Exception:
                pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
