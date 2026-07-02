"""Hugo's feature changelog.

This list is the source of truth for what Hugo will announce in the configured
announce channel (HUGO_ANNOUNCE_CHANNEL, defaults to #general). On bot startup,
hugo_bot diffs this list against `~/.hugo/announced_features.json` and posts
any new entries.

To announce a new feature: append a Feature(...) entry at the bottom of FEATURES
and restart the bot. The bot will post exactly once and persist the ID so it
won't re-announce on the next restart.

Keep entries chronological (oldest at top). Keep descriptions to one sentence;
they get rendered as a bullet in multi-feature announcements.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    description: str


FEATURES: list[Feature] = [
    Feature(
        id="digest",
        title="Daily reading digest",
        description=(
            "Every morning at 8am I drop summaries of new articles from the reading "
            "queue in the digest channel."
        ),
    ),
    Feature(
        id="welcome",
        title="Workspace welcome",
        description=(
            "I DM new workspace members with an intro and post a public hello so "
            "they don't show up to silence."
        ),
    ),
    Feature(
        id="url_summary",
        title="On-demand URL summary",
        description=(
            "Mention me with a URL (`@Hugo https://...`) and I'll reply in thread "
            "with a TL;DR. Up to 5 URLs at a time."
        ),
    ),
    Feature(
        id="reaction_triage",
        title="Reaction triage",
        description=(
            "React :books: on any message with a link and I'll summarize it in "
            "thread — no @-mention needed."
        ),
    ),
    Feature(
        id="help_command",
        title="Help on demand",
        description="`@Hugo help` lists everything I can do, in case you forget.",
    ),
    Feature(
        id="status_emojis",
        title="Status reactions",
        description=(
            "I react :eyes: while I'm working and :white_check_mark: when I'm done, "
            "so you can tell I haven't ghosted you."
        ),
    ),
    Feature(
        id="thread_tldr",
        title="Thread TL;DR",
        description=(
            "Inside any long thread, `@Hugo tldr` (also `recap` / `catchup`) and "
            "I'll summarize who said what, decisions made, and questions left "
            "hanging."
        ),
    ),
    Feature(
        id="manual_queue",
        title="Save links to the morning digest",
        description=(
            "React :bookmark: on any message with a URL, or say `@Hugo queue <url>`. "
            "It lands in tomorrow morning's digest. The Chrome reading list pipeline "
            "is retired."
        ),
    ),
    Feature(
        id="auto_curator",
        title="Auto-curated AI/tech reading",
        description=(
            "Every morning before the digest I scan AI/tech RSS feeds, score each "
            "candidate with Claude, and add the top picks to the digest queue. "
            "You wake up to interesting reading without having to find it."
        ),
    ),
    Feature(
        id="dm_support",
        title="DM me directly",
        description=(
            "Skip the channel noise — DM me anything you'd normally `@Hugo` me "
            "with. URLs to summarize, `queue <url>` to save, `tldr` (won't work in "
            "DMs since there's no thread, obviously), or just `help`. Same Hugo, "
            "private."
        ),
    ),
    Feature(
        id="web_research",
        title="Ask me anything (web research)",
        description=(
            "@-mention me with a question — no URL needed — and I'll research it "
            "live on the web and reply in thread with a concise, sourced answer. "
            "Ask from inside a thread and I'll read the thread for context first, "
            "so \"why is this happening?\" actually resolves."
        ),
    ),
]
