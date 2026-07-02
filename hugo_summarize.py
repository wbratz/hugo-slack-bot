"""Article fetching + summarization.

Shared by daily_digest (scheduled batch) and hugo_bot (on-demand from mentions
and reactions). Keep this module side-effect-light: no logging setup, no env
loading. Callers handle that.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import requests
import trafilatura
from anthropic import Anthropic

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_ARTICLE_CHARS = 40_000


@dataclass
class Article:
    text: str
    title: Optional[str] = None


_REDDIT_URL_RE = re.compile(
    r"https?://(?:www\.|old\.)?reddit\.com/r/[^/]+/comments/", re.IGNORECASE
)
_USER_AGENT = "hugo-bot/1.0 (Slack reading digest)"


def _fetch_with_trafilatura(url: str) -> Optional[Article]:
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        title: Optional[str] = None
        text: Optional[str] = None
        try:
            doc = trafilatura.bare_extraction(
                downloaded,
                include_comments=False,
                include_tables=False,
                favor_recall=True,
            )
        except Exception:
            doc = None
        if doc is not None:
            text = getattr(doc, "text", None)
            title = getattr(doc, "title", None)
        if not text:
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                favor_recall=True,
            )
        if not text:
            return None
        if len(text) > MAX_ARTICLE_CHARS:
            text = text[:MAX_ARTICLE_CHARS] + "\n\n[... truncated ...]"
        return Article(text=text, title=title)
    except Exception:
        return None


def _fetch_reddit(url: str) -> Optional[Article]:
    """Resolve a Reddit thread via its .json endpoint.

    For link posts: fetch the external URL it links to and return that content.
    For self posts: return the post body + top comments as the article text.
    """
    json_url = url.split("?", 1)[0].rstrip("/") + ".json"
    try:
        resp = requests.get(
            json_url,
            headers={"User-Agent": _USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None

    try:
        post = payload[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return None

    title = post.get("title")

    # Link post — fetch and summarize the destination URL instead
    if not post.get("is_self"):
        external = post.get("url_overridden_by_dest") or post.get("url")
        if external and not _REDDIT_URL_RE.search(external) and external != url:
            article = _fetch_with_trafilatura(external)
            if article:
                # Prefer the post's title if the destination didn't provide one
                if not article.title:
                    article = Article(text=article.text, title=title)
                return article
        # If we can't fetch the destination, fall through to use whatever the post contains

    # Self post (or fallback for link posts whose destination wouldn't load) —
    # use the post body plus top-level comments for context.
    body = (post.get("selftext") or "").strip()
    parts: list[str] = []
    if body:
        parts.append(body)

    try:
        comment_listing = payload[1]["data"]["children"]
    except (KeyError, IndexError, TypeError):
        comment_listing = []

    top_comments: list[str] = []
    for c in comment_listing[:6]:
        cdata = c.get("data") or {}
        body_text = (cdata.get("body") or "").strip()
        if not body_text or body_text in ("[deleted]", "[removed]"):
            continue
        author = cdata.get("author", "unknown")
        snippet = body_text[:600]
        if len(body_text) > 600:
            snippet += "..."
        top_comments.append(f"@{author}: {snippet}")

    if top_comments:
        parts.append("Top comments:\n" + "\n\n".join(top_comments))

    if not parts:
        return None

    text = "\n\n".join(parts)
    if len(text) > MAX_ARTICLE_CHARS:
        text = text[:MAX_ARTICLE_CHARS] + "\n\n[... truncated ...]"
    return Article(text=text, title=title)


def fetch_article(url: str) -> Optional[Article]:
    """Download URL and extract main article text + title. Returns None on any failure."""
    if _REDDIT_URL_RE.search(url):
        article = _fetch_reddit(url)
        if article:
            return article
        # If Reddit pathway fails, fall through to trafilatura as a last resort
    return _fetch_with_trafilatura(url)


SUMMARY_PROMPT = """You are summarizing an article for a personal reading digest.

Title: {title}
URL: {url}

Article text:
---
{text}
---

Write a summary as 3-5 short bullet points capturing the key takeaways. Each bullet should be one sentence, concrete, and actually informative — no fluff like "the article discusses". Lead with the most important insight first.

Output ONLY the bullets, one per line, each starting with "- ". No preamble, no headers."""


def summarize_article(anthropic_client: Anthropic, title: str, url: str, text: str) -> str:
    msg = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": SUMMARY_PROMPT.format(title=title, url=url, text=text),
            }
        ],
    )
    return msg.content[0].text.strip()


MAX_THREAD_CHARS = 40_000

THREAD_SUMMARY_PROMPT = """You are summarizing a Slack thread for someone who didn't have time to read it. Be concrete and useful — capture what was actually discussed, decided, or asked. Skip generic framing like "the participants discussed X".

Thread (chronological, one line per message, format `Name: text`):
---
{thread}
---

Output (use Slack mrkdwn — `*bold*`, `_italic_`):
- Open with a one-line topic summary in *bold*.
- Then 3-7 bullet points (one sentence each) capturing what was said. Reference contributors by name when relevant: "*Alice* flagged X; *Bob* countered with Y".
- If clear decisions were made, list them under a final "*Decisions:*" section.
- If real questions went unanswered, list them under "*Open questions:*".

Skip pleasantries, emoji-only reactions, and noise. Lead with the most important info. Be brief but useful. No preamble — output only the summary."""


def summarize_thread(anthropic_client: Anthropic, thread_text: str) -> str:
    if len(thread_text) > MAX_THREAD_CHARS:
        thread_text = thread_text[:MAX_THREAD_CHARS] + "\n\n[... older messages truncated ...]"
    msg = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": THREAD_SUMMARY_PROMPT.format(thread=thread_text),
            }
        ],
    )
    return msg.content[0].text.strip()
