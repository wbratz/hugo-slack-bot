"""Hugo — daily reading digest.

Reads pending entries from the queue (`~/.hugo/queue.json`), summarizes each
via the Anthropic API, posts a single digest to HUGO_DIGEST_CHANNEL, and
moves them to `posted` so they're not re-summarized.

Designed to be invoked once per day by DSM Task Scheduler.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

from anthropic import Anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from hugo_common import load_env, notify_admin, resolve_channel_id, setup_logging
from hugo_queue import get_pending, mark_posted
from hugo_summarize import fetch_article, summarize_article

load_env()
log = setup_logging("hugo.digest")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
HUGO_DIGEST_CHANNEL = os.environ.get("HUGO_DIGEST_CHANNEL", "#ai-summaries")


def slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def post_to_slack(slack: WebClient, channel_id: str, summaries: list[dict]) -> None:
    now = datetime.now()
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


def _run() -> int:
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return 1
    if not SLACK_BOT_TOKEN:
        log.error("SLACK_BOT_TOKEN not set")
        return 1

    slack = WebClient(token=SLACK_BOT_TOKEN)
    channel_id = resolve_channel_id(slack, HUGO_DIGEST_CHANNEL)
    if not channel_id:
        log.error(
            f"could not resolve channel '{HUGO_DIGEST_CHANNEL}' — "
            "does the bot have channels:read scope and access to the channel?"
        )
        return 1

    pending = get_pending()
    log.info(f"queue has {len(pending)} pending entries")
    if not pending:
        log.info("nothing pending — skipping post")
        return 0

    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    summaries: list[dict] = []
    posted_urls: list[str] = []

    for entry in pending:
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
    return 0


def main() -> int:
    try:
        return _run()
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
