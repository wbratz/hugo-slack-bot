"""Hugo bot — persistent Slack event listener (Socket Mode).

Reacts to:
- team_join: DMs the new member with the full intro, posts a public hello in
  the welcome channel.
- app_mention: `@Hugo <url> [...]` → fetches each URL, summarizes, replies in
  thread.
- reaction_added with `:books:`: fetches the URL(s) in the reacted message,
  summarizes, replies in thread.

Unhandled exceptions in any handler are reported to HUGO_ADMIN_USER_ID via DM
so silent breakage doesn't pile up.

Run with no args to start the listener. Run with --test-greet <USER_ID> to
fire the greeting flow against a specific user (for smoke testing).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path

from anthropic import Anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from hugo_common import load_env, notify_admin, resolve_channel_id, setup_logging
from hugo_features import FEATURES, Feature
from hugo_queue import add_to_queue
from hugo_summarize import fetch_article, summarize_article, summarize_thread

load_env()
log = setup_logging("hugo.bot")

WELCOME_CHANNEL = os.environ.get("HUGO_WELCOME_CHANNEL")
DIGEST_CHANNEL = os.environ.get("HUGO_DIGEST_CHANNEL", "#ai-summaries")
ANNOUNCE_CHANNEL = os.environ.get("HUGO_ANNOUNCE_CHANNEL", "#general")
WORKSPACE_NAME = os.environ.get("HUGO_WORKSPACE_NAME", "the workspace")
WELCOME_ENABLED = os.environ.get("HUGO_WELCOME_ENABLED", "true").lower() in ("true", "1", "yes", "on")
SUMMARY_TRIGGER_EMOJI = "books"
QUEUE_TRIGGER_EMOJI = "bookmark"
WORKING_EMOJI = "eyes"
DONE_EMOJI = "white_check_mark"
QUEUED_EMOJI = "bookmark_tabs"
MAX_URLS_PER_REQUEST = 5

ANNOUNCED_FEATURES_FILE = Path.home() / ".hugo" / "announced_features.json"

HELP_TEXT = (
    ":wave: Here's everything I can do right now:\n\n"
    ":books: *Daily reading digest* — every morning at 8am I post summaries of "
    "everything in my queue to {digest_channel}.\n\n"
    ":robot_face: *Auto-curator* — every morning before the digest I scan AI/tech "
    "RSS feeds, score them with Claude, and add the top picks to the queue. You "
    "wake up to interesting reading without having to find it.\n\n"
    ":bookmark: *Queue a link for the digest* — react :bookmark: on any message "
    "with a URL, or say `@Hugo queue <url>`. It lands in tomorrow's morning digest.\n\n"
    ":link: *Summarize a URL right now* — `@Hugo https://example.com` and I'll reply "
    "in thread with a TL;DR. Up to 5 URLs at a time.\n\n"
    ":books: *React :books: on a link* and I'll summarize it in thread immediately — "
    "no @-mention needed.\n\n"
    ":scroll: *Thread TL;DR* — inside any long thread, `@Hugo tldr` (or `recap` / "
    "`catchup`) and I'll summarize who said what + decisions + open questions.\n\n"
    "_While I'm working you'll see me react :eyes: on your request. When I'm done, "
    "you'll see :white_check_mark:._\n\n"
    ":speech_balloon: *Prefer to DM me?* All of the above works in a direct message to "
    "me too — drop a URL, say `queue <url>`, ask `help`, or just `@Hugo` me in a "
    "channel. Same Hugo, less channel noise.\n\n"
    "Lost? Say `help` any time."
)

HELP_KEYWORD_RE = re.compile(
    r"\b(help|commands?|usage|what can you do|how do (i|you))\b",
    re.IGNORECASE,
)

TLDR_KEYWORD_RE = re.compile(
    r"\b(tl;?dr|recap|catch[\s\-]?up)\b",
    re.IGNORECASE,
)

QUEUE_KEYWORD_RE = re.compile(
    r"\b(queue|save|read[\s\-]?later|bookmark)\b",
    re.IGNORECASE,
)

MAX_THREAD_MESSAGES = 500

INTRO_DM = (
    "Hey <@{user}> :wave:\n\n"
    "I'm *Hugo* — {workspace_name}'s resident AI bouncer, librarian, and chaos translator. "
    "I grab links, read the stuff nobody has time to read, and drop clean summaries into the "
    "channel before the group chat spirals into a 47-message debate. "
    "Built like a final boss, tuned like an intern with unlimited caffeine, "
    "and here to turn rabbit holes into useful signal.\n\n"
    "*What I can do right now:*\n"
    ":books: *Daily reading digest* — every morning at 8am I post summaries to "
    "{digest_channel}. The queue gets fed by my auto-curator (AI/tech feeds I scan "
    "and score) plus anything you save manually.\n"
    ":bookmark: *Save links to the digest queue* — react :bookmark: on any link, or "
    "say `@Hugo queue <url>`.\n"
    ":link: *Summarize on demand* — `@Hugo https://...` and I'll reply in thread "
    "with a TL;DR right now.\n"
    ":books: *React :books: on a link* and I'll summarize it in thread immediately.\n"
    ":scroll: *Thread TL;DR* — `@Hugo tldr` inside any thread.\n\n"
    "_I'm constantly being enhanced — more tricks land regularly. Mentions, smarter replies, "
    "richer summaries, and probably some chaos nobody asked for. Stay tuned._"
)

PUBLIC_GREETING = (
    "Everyone, please welcome <@{user}> to {workspace_name} :wave:\n"
    "I'm *Hugo* — I just slid into their DMs with the full tour. Be nice."
)


_digest_mention_cache: str | None = None


def get_digest_mention(client) -> str:
    global _digest_mention_cache
    if _digest_mention_cache is None:
        channel_id = resolve_channel_id(client, DIGEST_CHANNEL)
        if channel_id:
            _digest_mention_cache = f"<#{channel_id}>"
        else:
            log.warning(
                f"could not resolve digest channel {DIGEST_CHANNEL} — "
                "falling back to plain text"
            )
            _digest_mention_cache = DIGEST_CHANNEL
    return _digest_mention_cache


def greet_user(client, user_id: str) -> None:
    digest_mention = get_digest_mention(client)
    try:
        resp = client.conversations_open(users=user_id)
        dm_channel = resp["channel"]["id"]
        client.chat_postMessage(
            channel=dm_channel,
            text=INTRO_DM.format(
                user=user_id,
                digest_channel=digest_mention,
                workspace_name=WORKSPACE_NAME,
            ),
        )
        log.info(f"  DM sent to {user_id}")
    except Exception as exc:
        log.error(f"  DM to {user_id} failed: {exc}")

    if WELCOME_CHANNEL:
        try:
            client.chat_postMessage(
                channel=WELCOME_CHANNEL,
                text=PUBLIC_GREETING.format(
                    user=user_id,
                    workspace_name=WORKSPACE_NAME,
                ),
            )
            log.info(f"  public greeting posted in {WELCOME_CHANNEL}")
        except Exception as exc:
            log.error(f"  public greeting in {WELCOME_CHANNEL} failed: {exc}")
    else:
        log.warning("  HUGO_WELCOME_CHANNEL not set — skipping public greeting")


# Slack-formatted: <https://...> or <https://...|display>
_SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|[^>]*)?>")
# Plain URLs that weren't wrapped
_PLAIN_URL_RE = re.compile(r"(?<![<\w])https?://[^\s<>\"\)\]]+")


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    urls = list(_SLACK_LINK_RE.findall(text))
    cleaned = _SLACK_LINK_RE.sub(" ", text)
    urls.extend(_PLAIN_URL_RE.findall(cleaned))
    seen: set[str] = set()
    ordered: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def queue_urls(urls: list[str], source: str) -> tuple[int, int]:
    """Add URLs to the queue. Returns (newly_added, already_known)."""
    added = 0
    skipped = 0
    for url in urls[:MAX_URLS_PER_REQUEST]:
        if add_to_queue(url, source=source):
            added += 1
        else:
            skipped += 1
    return added, skipped


def react_status(client, channel: str, ts: str, emoji: str) -> None:
    """Add a reaction. Silent on already_reacted; warns on other failures."""
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=emoji)
    except SlackApiError as exc:
        err = exc.response.get("error")
        if err != "already_reacted":
            log.warning(f"reactions.add({emoji}) on {channel}/{ts} failed: {err}")


def unreact_status(client, channel: str, ts: str, emoji: str) -> None:
    """Remove a reaction. Silent on no_reaction; warns on other failures."""
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name=emoji)
    except SlackApiError as exc:
        err = exc.response.get("error")
        if err != "no_reaction":
            log.warning(f"reactions.remove({emoji}) on {channel}/{ts} failed: {err}")


_user_name_cache: dict[str, str] = {}
_bot_user_id_cache: str | None = None


def get_bot_user_id(client) -> str | None:
    global _bot_user_id_cache
    if _bot_user_id_cache is None:
        try:
            _bot_user_id_cache = client.auth_test()["user_id"]
        except Exception as exc:
            log.warning(f"auth.test failed: {exc}")
    return _bot_user_id_cache


def get_user_display_name(client, user_id: str) -> str:
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    name = user_id
    try:
        resp = client.users_info(user=user_id)
        user = resp.get("user", {})
        profile = user.get("profile", {})
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("name")
            or user_id
        )
    except Exception as exc:
        log.warning(f"users.info({user_id}) failed: {exc}")
    _user_name_cache[user_id] = name
    return name


def clean_slack_text(client, text: str) -> str:
    """Convert Slack-encoded mentions and links to human-readable form."""
    if not text:
        return ""

    def user_repl(m):
        return f"@{get_user_display_name(client, m.group(1))}"

    text = re.sub(r"<@(U\w+)(?:\|[^>]+)?>", user_repl, text)
    text = re.sub(r"<#C\w+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<#C\w+>", "#channel", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text


def fetch_thread_messages(client, channel: str, thread_ts: str) -> list[dict] | None:
    """Fetch all messages in a thread. Returns None if the API call fails."""
    messages: list[dict] = []
    cursor: str | None = None
    try:
        while True:
            resp = client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=200,
                cursor=cursor,
            )
            messages.extend(resp.get("messages", []))
            if len(messages) >= MAX_THREAD_MESSAGES:
                messages = messages[:MAX_THREAD_MESSAGES]
                break
            cursor = resp.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break
    except SlackApiError as exc:
        log.warning(f"conversations.replies failed: {exc.response.get('error')}")
        return None
    return messages


def format_thread_for_summary(
    client, messages: list[dict], bot_user_id: str | None, exclude_ts: str | None
) -> tuple[str, int]:
    """Return (formatted_thread, count_of_real_messages)."""
    lines: list[str] = []
    real_count = 0
    for msg in messages:
        if msg.get("subtype") in ("bot_message", "channel_join", "channel_leave"):
            continue
        if exclude_ts and msg.get("ts") == exclude_ts:
            continue
        user_id = msg.get("user")
        if not user_id:
            continue
        if bot_user_id and user_id == bot_user_id:
            continue
        text = clean_slack_text(client, msg.get("text", "")).strip()
        if not text:
            continue
        name = get_user_display_name(client, user_id)
        lines.append(f"{name}: {text}")
        real_count += 1
    return "\n".join(lines), real_count


def summarize_thread_and_reply(
    client, channel: str, thread_ts: str, exclude_ts: str | None = None
) -> None:
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set — can't summarize")
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":warning: I'm not set up to summarize right now (missing API key).",
        )
        return

    messages = fetch_thread_messages(client, channel, thread_ts)
    if messages is None:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                ":lock: I couldn't read this thread — am I in the channel? "
                "Try `/invite @Hugo` and ask again."
            ),
        )
        return

    bot_user_id = get_bot_user_id(client)
    thread_text, real_count = format_thread_for_summary(
        client, messages, bot_user_id, exclude_ts
    )

    if real_count < 2:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":shrug: This thread is too short to bother summarizing — just read it.",
        )
        return

    anthropic_client = Anthropic(api_key=anthropic_api_key)
    try:
        summary = summarize_thread(anthropic_client, thread_text)
    except Exception as exc:
        log.warning(f"  thread summarize failed: {exc}")
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":warning: TL;DR failed: `{exc}`",
        )
        return

    header = f":scroll: *TL;DR* — {real_count} messages\n\n"
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=header + summary,
    )
    log.info(f"  thread tldr posted ({real_count} messages)")


def fetch_message(client, channel: str, ts: str) -> dict | None:
    """Look up a single message by (channel, ts). Returns None if not found.

    conversations.history doesn't return thread replies, so reactions on
    thread replies will silently no-op here. Acceptable for v1.
    """
    try:
        resp = client.conversations_history(
            channel=channel,
            latest=ts,
            oldest=ts,
            inclusive=True,
            limit=1,
        )
        messages = resp.get("messages", [])
        if messages:
            return messages[0]
    except Exception as exc:
        log.warning(f"fetch_message failed for {channel}/{ts}: {exc}")
    return None


def summarize_and_reply(client, channel: str, thread_ts: str, urls: list[str]) -> None:
    """Summarize up to MAX_URLS_PER_REQUEST and reply in the thread."""
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set — can't summarize")
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":warning: I'm not set up to summarize right now (missing API key).",
        )
        return

    anthropic_client = Anthropic(api_key=anthropic_api_key)
    capped = urls[:MAX_URLS_PER_REQUEST]
    if len(urls) > MAX_URLS_PER_REQUEST:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":hugo: That's {len(urls)} links — I'll do the first {MAX_URLS_PER_REQUEST}.",
        )

    for url in capped:
        log.info(f"  summarizing on-demand: {url}")
        article = fetch_article(url)
        if not article:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":disappointed: Couldn't fetch or extract content from <{url}>",
            )
            continue
        title = article.title or url
        try:
            summary = summarize_article(anthropic_client, title, url, article.text)
        except Exception as exc:
            log.warning(f"  summarize failed for {url}: {exc}")
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":warning: Summary failed for <{url}>: `{exc}`",
            )
            continue
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"*<{url}|{title}>*\n{summary}",
        )


def load_announced_features() -> set[str]:
    if ANNOUNCED_FEATURES_FILE.exists():
        try:
            data = json.loads(ANNOUNCED_FEATURES_FILE.read_text(encoding="utf-8"))
            return set(data.get("announced", []))
        except Exception as exc:
            log.warning(f"could not parse {ANNOUNCED_FEATURES_FILE}: {exc}")
    return set()


def save_announced_features(announced: set[str]) -> None:
    ANNOUNCED_FEATURES_FILE.write_text(
        json.dumps({"announced": sorted(announced)}, indent=2),
        encoding="utf-8",
    )


def _format_single_feature_announcement(feature: Feature) -> str:
    return (
        f":sparkles: *New trick unlocked: {feature.title}*\n\n"
        f"{feature.description}\n\n"
        f"_Say `@Hugo help` for the full list of what I can do._"
    )


def _format_multi_feature_announcement(features: list[Feature]) -> str:
    bullets = "\n".join(f"• *{f.title}* — {f.description}" for f in features)
    return (
        f":sparkles: *Hugo just leveled up — {len(features)} new tricks*\n\n"
        f"{bullets}\n\n"
        f"_Say `@Hugo help` any time to see the full list._"
    )


def announce_new_features(client) -> None:
    """Post any features not yet in the announced state file. Run once per startup."""
    announced = load_announced_features()
    new_features = [f for f in FEATURES if f.id not in announced]
    if not new_features:
        log.info("no new features to announce")
        return

    channel_id = resolve_channel_id(client, ANNOUNCE_CHANNEL)
    if not channel_id:
        log.warning(
            f"could not resolve announce channel '{ANNOUNCE_CHANNEL}' — skipping "
            "feature announcement"
        )
        return

    if len(new_features) == 1:
        text = _format_single_feature_announcement(new_features[0])
    else:
        text = _format_multi_feature_announcement(new_features)

    try:
        client.chat_postMessage(channel=channel_id, text=text)
        log.info(f"announced {len(new_features)} feature(s) to {channel_id}")
        announced.update(f.id for f in new_features)
        save_announced_features(announced)
    except Exception as exc:
        log.error(f"feature announcement failed: {exc}")


def force_announce_feature(client, feature_id: str) -> int:
    """Force-announce a feature regardless of state. Returns process exit code."""
    feature = next((f for f in FEATURES if f.id == feature_id), None)
    if not feature:
        known = ", ".join(f.id for f in FEATURES)
        log.error(f"unknown feature id '{feature_id}' — known: {known}")
        return 1
    channel_id = resolve_channel_id(client, ANNOUNCE_CHANNEL)
    if not channel_id:
        log.error(f"could not resolve announce channel '{ANNOUNCE_CHANNEL}'")
        return 1
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=_format_single_feature_announcement(feature),
        )
        log.info(f"force-announced feature '{feature_id}' to {channel_id}")
        return 0
    except Exception as exc:
        log.error(f"force announcement failed: {exc}")
        return 2


def handle_user_request(
    client,
    *,
    channel: str,
    request_ts: str,
    in_thread_ts: str | None,
    text: str,
    user: str,
    source_label: str,
) -> None:
    """Common routing for @-mentions in channels and direct messages.

    request_ts: the user's message timestamp (used for status reactions and
        for the thread under which Hugo's reply sits).
    in_thread_ts: the parent thread the user's message lives in, if any.
        None for top-level channel mentions and for DMs.
    source_label: short label used in queue source attribution
        (e.g. "mention", "DM").
    """
    thread_ts_for_reply = in_thread_ts or request_ts
    urls = extract_urls(text)

    # Queue intent
    if urls and QUEUE_KEYWORD_RE.search(text):
        react_status(client, channel, request_ts, WORKING_EMOJI)
        try:
            added, skipped = queue_urls(urls, source=f"manual/{source_label} by {user}")
            if added and skipped:
                msg = f":books: Stashed {added}, already had {skipped}. Tomorrow's digest will eat the new ones."
            elif added:
                plural = "" if added == 1 else "s"
                msg = f":books: Stashed {added} link{plural}. Tomorrow's digest will eat {'it' if added == 1 else 'them'}."
            else:
                msg = ":hugo: Already had those. Move along."
            client.chat_postMessage(channel=channel, thread_ts=thread_ts_for_reply, text=msg)
            log.info(f"  queued {added}, skipped {skipped}")
        finally:
            unreact_status(client, channel, request_ts, WORKING_EMOJI)
            react_status(client, channel, request_ts, DONE_EMOJI)
        return

    # URL summary
    if urls:
        react_status(client, channel, request_ts, WORKING_EMOJI)
        try:
            summarize_and_reply(
                client, channel=channel, thread_ts=thread_ts_for_reply, urls=urls
            )
        finally:
            unreact_status(client, channel, request_ts, WORKING_EMOJI)
            react_status(client, channel, request_ts, DONE_EMOJI)
        return

    # Thread TL;DR
    if TLDR_KEYWORD_RE.search(text):
        if not in_thread_ts:
            client.chat_postMessage(
                channel=channel,
                thread_ts=request_ts,
                text=(
                    ":thinking_face: I can only TL;DR a thread. Mention me from "
                    "*inside* the thread you want summarized."
                ),
            )
            return
        react_status(client, channel, request_ts, WORKING_EMOJI)
        try:
            summarize_thread_and_reply(
                client, channel=channel, thread_ts=in_thread_ts, exclude_ts=request_ts
            )
        finally:
            unreact_status(client, channel, request_ts, WORKING_EMOJI)
            react_status(client, channel, request_ts, DONE_EMOJI)
        return

    # Help
    if HELP_KEYWORD_RE.search(text):
        digest_mention = get_digest_mention(client)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts_for_reply,
            text=HELP_TEXT.format(digest_channel=digest_mention),
        )
        return

    # Default — friendly nudge
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts_for_reply,
        text=(
            ":wave: Hi! Say `help` to see what I can do, drop a URL to summarize, "
            "or say `queue <url>` to save it for tomorrow's digest."
        ),
    )


def build_app() -> App:
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("team_join")
    def on_team_join(event, client):
        if not WELCOME_ENABLED:
            log.info("team_join received but HUGO_WELCOME_ENABLED is false — skipping")
            return
        user_id = event["user"]["id"]
        log.info(f"team_join: {user_id}")
        greet_user(client, user_id)

    @app.event("app_mention")
    def on_app_mention(event, client):
        text = event.get("text", "")
        channel = event["channel"]
        request_ts = event["ts"]
        in_thread_ts = event.get("thread_ts")
        user = event.get("user", "unknown")
        log.info(f"app_mention in {channel} — text len {len(text)}")
        handle_user_request(
            client,
            channel=channel,
            request_ts=request_ts,
            in_thread_ts=in_thread_ts,
            text=text,
            user=user,
            source_label="mention",
        )

    @app.event("reaction_added")
    def on_reaction_added(event, client):
        reaction = event.get("reaction")
        if reaction not in (SUMMARY_TRIGGER_EMOJI, QUEUE_TRIGGER_EMOJI):
            return
        item = event.get("item", {})
        if item.get("type") != "message":
            return
        channel = item["channel"]
        ts = item["ts"]
        user = event.get("user", "unknown")
        log.info(f"reaction :{reaction}: in {channel} on {ts}")
        msg = fetch_message(client, channel, ts)
        if not msg:
            log.info("  message not retrievable (likely a thread reply) — skipping")
            return
        urls = extract_urls(msg.get("text", ""))
        if not urls:
            log.info("  no URLs in the reacted message — skipping silently")
            return

        if reaction == QUEUE_TRIGGER_EMOJI:
            added, skipped = queue_urls(urls, source=f"manual/reaction by {user}")
            log.info(f"  queued {added}, skipped {skipped}")
            if added > 0:
                react_status(client, channel, ts, QUEUED_EMOJI)
            return

        # SUMMARY_TRIGGER_EMOJI (:books:) flow
        react_status(client, channel, ts, WORKING_EMOJI)
        try:
            summarize_and_reply(client, channel=channel, thread_ts=ts, urls=urls)
        finally:
            unreact_status(client, channel, ts, WORKING_EMOJI)
            react_status(client, channel, ts, DONE_EMOJI)

    @app.event("message")
    def on_message(event, client):
        # Only act on direct messages to Hugo. Channel messages are handled
        # via app_mention; reactions handle their own thing.
        if event.get("channel_type") != "im":
            return
        # Ignore Hugo's own DMs and any message-edited / file-shared subevents
        if event.get("subtype") or event.get("bot_id"):
            return
        text = event.get("text", "")
        channel = event["channel"]
        request_ts = event["ts"]
        in_thread_ts = event.get("thread_ts")
        user = event.get("user", "unknown")
        log.info(f"DM from {user} — text len {len(text)}")
        handle_user_request(
            client,
            channel=channel,
            request_ts=request_ts,
            in_thread_ts=in_thread_ts,
            text=text,
            user=user,
            source_label="DM",
        )

    @app.error
    def on_error(error, body, client):
        log.exception(f"unhandled bot error: {error}")
        tb = traceback.format_exc()
        notify_admin(
            client,
            f":rotating_light: Hugo bot handler crashed\n```\n{tb}\n```",
        )

    return app


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--test-greet",
        metavar="USER_ID",
        help="Run the greeting flow against the given Slack user ID and exit.",
    )
    p.add_argument(
        "--announce-feature",
        metavar="FEATURE_ID",
        help=(
            "Force-announce a feature by ID (e.g. 'thread_tldr') regardless of state. "
            "For testing or backfill. Does not update announced-features state."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not os.environ.get("SLACK_BOT_TOKEN"):
        log.error("SLACK_BOT_TOKEN not set (check .env)")
        return 1
    if not os.environ.get("SLACK_APP_TOKEN"):
        log.error("SLACK_APP_TOKEN not set (check .env)")
        return 1

    app = build_app()

    if args.test_greet:
        log.info(f"test-greet: {args.test_greet}")
        greet_user(app.client, args.test_greet)
        return 0

    if args.announce_feature:
        return force_announce_feature(app.client, args.announce_feature)

    try:
        announce_new_features(app.client)
    except Exception as exc:
        log.error(f"announce_new_features crashed (continuing anyway): {exc}")

    log.info("Hugo bot starting (Socket Mode)")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
