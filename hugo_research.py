"""Freeform Q&A with live web research.

Used by hugo_bot when someone @-mentions Hugo (or DMs him) with a real question
that isn't a URL, a `tldr`, a `queue`, or a `help` request. Hugo researches it
with the Anthropic server-side web search tool and returns a concise, sourced
answer.

Side-effect-light like hugo_summarize: no logging setup, no env loading. The
caller owns the Anthropic client and the environment. Slack rendering (turning
the returned sources into `<url|title>` links) is the caller's job too — this
module stays Slack-agnostic and just returns text + (title, url) tuples.
"""
from __future__ import annotations

import os

from anthropic import Anthropic

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Cap searches per question so one query can't quietly run up the bill.
WEB_SEARCH_MAX_USES = 5
# How many source links to hand back to the caller.
MAX_SOURCES = 4
# Keep the answer tight — this is Slack, not an essay.
MAX_ANSWER_TOKENS = 1024
# Guard against dumping a giant thread into the prompt.
MAX_THREAD_CONTEXT_CHARS = 12_000

# `web_search_20250305` is the basic web-search variant. It works on every
# web-search-capable model, including whatever CLAUDE_MODEL is set to, so the
# feature doesn't break if the configured model changes. (The newer
# `web_search_20260209` adds dynamic filtering but is gated to Sonnet 4.6+ /
# Opus 4.6+ — not worth the compatibility risk here.)
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": WEB_SEARCH_MAX_USES,
}

RESEARCH_SYSTEM_PROMPT = (
    "You are Hugo, a Slack workspace's resident AI bouncer, librarian, and chaos "
    "translator. Someone has asked you a question. Research it with the web_search "
    "tool when the answer depends on current or factual information, then answer.\n\n"
    "Voice: concise, slightly dry, a little wry. Not corporate, not bubbly. No "
    "\"Sure thing!\" openers, no \"I'm just a bot\" disclaimers, no emoji spam. Be "
    "honest when something is uncertain or you couldn't find it — say so plainly "
    "instead of guessing.\n\n"
    "Format for Slack:\n"
    "- Use Slack mrkdwn: *bold* (single asterisks), _italic_, `code`. Never use "
    "**double-asterisk** markdown — it renders literally in Slack.\n"
    "- Keep it tight. Lead with the answer. A few short sentences, or 3-5 bullets "
    "for anything with parts. Don't pad.\n"
    "- Do NOT append your own \"Sources:\" section or a list of links — sources are "
    "attached separately.\n"
    "- If a Slack thread is included for context, use it to resolve what \"this\", "
    "\"that\", or \"it\" refer to before answering."
)


def _build_user_content(question: str, thread_context: str | None) -> str:
    question = question.strip()
    if not thread_context:
        return question
    context = thread_context.strip()
    if len(context) > MAX_THREAD_CONTEXT_CHARS:
        context = context[:MAX_THREAD_CONTEXT_CHARS] + "\n\n[... earlier messages truncated ...]"
    return (
        "Here's the Slack thread I'm being asked about (oldest message first):\n"
        f"---\n{context}\n---\n\n"
        f"The question: {question}"
    )


def answer_question(
    anthropic_client: Anthropic,
    question: str,
    thread_context: str | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Research `question` on the web and return (answer_text, sources).

    `thread_context` is the surrounding Slack thread (formatted `Name: text`,
    oldest first) when the question was asked inside a thread, so pronouns like
    "this" resolve. None for DMs / top-level mentions.

    `sources` is a list of (title, url) tuples — the sources Hugo actually cited,
    falling back to raw search hits — capped at MAX_SOURCES. Raises on API/tool
    failure; the caller handles that gracefully.
    """
    msg = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_ANSWER_TOKENS,
        system=RESEARCH_SYSTEM_PROMPT,
        tools=[WEB_SEARCH_TOOL],
        messages=[{"role": "user", "content": _build_user_content(question, thread_context)}],
    )

    text_parts: list[str] = []
    cited: list[tuple[str, str]] = []       # sources Hugo referenced in the answer
    searched: list[tuple[str, str]] = []    # raw search hits, as a fallback

    for block in msg.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            # Consecutive text blocks are one continuous stream split at citation
            # boundaries — concatenate directly so prose isn't mangled.
            text_parts.append(getattr(block, "text", "") or "")
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if url:
                    cited.append((getattr(citation, "title", None) or url, url))
        elif btype == "web_search_tool_result":
            # On success `.content` is a list of results; on error it's a single
            # error object — branch before iterating.
            content = getattr(block, "content", None)
            if isinstance(content, list):
                for result in content:
                    url = getattr(result, "url", None)
                    if url:
                        searched.append((getattr(result, "title", None) or url, url))

    answer = "".join(text_parts).strip()

    # Prefer the sources Hugo actually cited; fall back to raw search hits.
    # Dedupe by URL, preserve order.
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    for title, url in cited + searched:
        if url not in seen:
            seen.add(url)
            sources.append((title, url))

    return answer, sources[:MAX_SOURCES]
