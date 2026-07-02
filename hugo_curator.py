"""Hugo's auto-curator.

Pulls AI/tech RSS feeds, dedupes against state, asks Claude to score each
candidate's relevance, and adds the top N to the digest queue.

Run via DSM Task Scheduler at ~6am, before the 8am digest.

To change Hugo's reading universe, edit the FEEDS list below. Aim for
high-signal, low-volume sources. Restart isn't needed — the curator
re-reads the file on each run.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
from anthropic import Anthropic
from slack_sdk import WebClient

from hugo_common import STATE_DIR, load_env, notify_admin, setup_logging
from hugo_queue import add_to_queue, is_known

load_env()
log = setup_logging("hugo.curator")

CURATOR_STATE_FILE = STATE_DIR / "curator_state.json"
DAILY_CAP = int(os.environ.get("HUGO_CURATOR_DAILY_CAP", "7"))
RANKING_THRESHOLD = int(os.environ.get("HUGO_CURATOR_THRESHOLD", "6"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ENTRIES_PER_FEED = 25
RANK_BATCH_SIZE = 40  # candidates per LLM call; keeps output under max_tokens

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")

# Hugo's reading universe. Edit to taste — add, comment out, reorder.
# Aim for high-signal, low-volume. ~10 feeds works well at 5/day.
FEEDS: list[dict] = [
    # Hacker News — broad tech + tuned AI/agent search
    {"name": "Hacker News (front page)", "url": "https://hnrss.org/frontpage"},
    {"name": "Hacker News (AI/dev keywords)", "url": "https://hnrss.org/newest?q=AI+OR+LLM+OR+Claude+OR+Anthropic+OR+OpenAI+OR+Cursor+OR+Copilot+OR+Codex+OR+%22coding+agent%22+OR+%22AI+coding%22+OR+RAG+OR+agent"},

    # AI/ML practitioner content — heavy weight on actually-shipping content
    {"name": "Towards Data Science", "url": "https://towardsdatascience.com/feed"},
    {"name": "KDnuggets", "url": "https://www.kdnuggets.com/feed"},
    {"name": "MachineLearningMastery", "url": "https://machinelearningmastery.com/feed"},
    {"name": "InfoQ AI/ML/Data Eng", "url": "https://feed.infoq.com/ai-ml-data-eng/"},
    {"name": "O'Reilly Radar", "url": "https://www.oreilly.com/radar/feed/index.xml"},
    {"name": "LangChain blog", "url": "https://blog.langchain.com/rss/"},

    # Industry analysis + news
    {"name": "Latent Space", "url": "https://www.latent.space/feed"},
    {"name": "Import AI", "url": "https://importai.substack.com/feed"},
    {"name": "Stratechery", "url": "https://stratechery.com/feed/"},
    {"name": "The New Stack", "url": "https://thenewstack.io/feed/"},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/"},

    # Consumer / journalist AI coverage + dev tooling
    {"name": "The Verge AI", "url": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"},
    {"name": "Ars Technica AI tag", "url": "https://arstechnica.com/tag/ai/feed/"},
    {"name": "XDA Developers", "url": "https://www.xda-developers.com/feed/"},

    # Reddit feeds disabled — noisy + scrape-hostile.
    # Manual `:bookmark:` on a Reddit link still works (hugo_summarize handles
    # Reddit via its .json endpoint). Re-enable here for auto-curation:
    # {"name": "/r/LocalLLaMA top week", "url": "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=week"},
    # {"name": "/r/MachineLearning top week", "url": "https://www.reddit.com/r/MachineLearning/top/.rss?t=week"},
]


@dataclass
class Candidate:
    url: str
    title: str
    source: str
    blurb: str


def load_curator_state() -> dict:
    if CURATOR_STATE_FILE.exists():
        return json.loads(CURATOR_STATE_FILE.read_text(encoding="utf-8"))
    return {"evaluated": {}}


def save_curator_state(state: dict) -> None:
    CURATOR_STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    if "<" in text:
        text = _HTML_TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def fetch_feed_entries(feed_meta: dict) -> list[Candidate]:
    name = feed_meta["name"]
    url = feed_meta["url"]
    try:
        parsed = feedparser.parse(url, agent="hugo-bot/1.0 (+slack)")
    except Exception as exc:
        log.warning(f"  feedparser error for {name}: {exc}")
        return []
    raw_entries = parsed.get("entries", []) or []
    entries: list[Candidate] = []
    for raw in raw_entries[:ENTRIES_PER_FEED]:
        link = (raw.get("link") or "").strip()
        title = (raw.get("title") or "").strip()
        if not link or not title:
            continue
        blurb = _strip_html(raw.get("summary") or raw.get("description") or "")
        entries.append(Candidate(url=link, title=title, source=name, blurb=blurb[:500]))
    log.info(f"  {name}: {len(entries)} entries")
    return entries


def fetch_all_feeds() -> list[Candidate]:
    log.info(f"fetching {len(FEEDS)} feeds")
    all_entries: list[Candidate] = []
    for feed in FEEDS:
        all_entries.extend(fetch_feed_entries(feed))
    log.info(f"total entries fetched: {len(all_entries)}")
    return all_entries


RANK_PROMPT = """You are curating articles for a personal reading list owned by a software engineer who is deep into AI tooling, agent engineering, and the practice of actually shipping reliable AI-powered software. Score each article 1-10.

Top priority — boost (8-10):
- *Claude Code specifically* — features, dashboards, workflow tips, "how I improved my Claude Code", agent view, prompt patterns
- AI coding tools and agents broadly: Cursor, Copilot, Aider, Codex, Windsurf, LangChain, AutoGen, CrewAI, MCP servers, SPEC.md / orchestration formats
- AI engineering ops — evaluation harnesses, guardrails, hallucination measurement, LLM observability, monitoring; RAG patterns including graph-RAG and temporal-RAG; multi-metric eval frameworks; tracing / cost / latency tooling
- Local LLM workflows — running models locally, persistent context, hybrid pipelines, escaping cloud message limits, context engineering
- Practitioner experience reports — "I built X to solve Y", "How I do Z", "Why I stopped using A and switched to B"; first-person accounts from working engineers
- VS Code, JetBrains, and IDE productivity — even when the article isn't strictly AI (hidden task runners, agent windows, useful extensions). A pure dev-productivity gem still counts
- New foundation models with strong coding capabilities, especially with SWE-bench / HumanEval numbers
- Content-rich "small-N" pieces ("5 small LLMs for tool calling", "12-metric eval framework from 100+ deployments") that name specific tools/patterns and explain them — these LOOK like listicles but have actual substance, NOT generic top-10 fluff

Mid-tier — include if notable (6-7):
- AI industry news that affects builders — model releases, product launches, acquisitions, lab moves
- Industry drama and named-person stories — trial coverage (e.g. Sam Altman testimony), founder/exec moves, big interviews; even gossipy if it tells you something about where the field is going
- Major frontier-lab announcements (Anthropic, OpenAI, DeepMind, Meta AI, xAI)
- One-off fascinating tech or science stories — breakthroughs, even if not AI
- Research papers with practical implications for engineers
- Security incidents, vulnerabilities, or post-mortems relevant to engineers
- Credible analysis from named voices (Stratechery, Latent Space, Import AI, etc.)

Skip (1-5):
- Pure marketing or PR fluff
- Generic surface-level listicles ("10 productivity tips", "5 best Notion templates") with no specific technical depth
- Paywall teasers with no actionable substance
- Crypto-only news, NFT hype, ad-tech-only content
- Off-topic content (sports, politics outside tech, lifestyle, gaming reviews, deal alerts, e-commerce)
- "AI ethics" philosophy without concrete substance or specific incidents
- Vague enterprise AI thought-leadership ("How AI will transform [industry]")
- Beginner-level intros to topics the reader has long mastered (what is an LLM, intro to Python, hello-world tutorials)

How to distinguish a content-rich list (boost) from a fluff listicle (skip):
- Content-rich: names specific products/papers/patterns, makes claims you can act on, came from someone who clearly used the tools
- Fluff: vague tips, no specific recommendations, reads like SEO content

Output ONLY a JSON array, no preamble, no code fence. One object per article in the input order:
[{{"index": 0, "score": 8, "reason": "brief reasoning"}}, ...]

Articles:
{items}"""


def _rank_batch(
    client: Anthropic, batch: list[Candidate]
) -> list[tuple[int, Candidate, str]]:
    """Rank a single batch. Returns [(score, candidate, reason), ...]."""
    items_json = json.dumps(
        [
            {"index": i, "source": c.source, "title": c.title, "blurb": c.blurb[:300]}
            for i, c in enumerate(batch)
        ],
        indent=2,
    )
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": RANK_PROMPT.format(items=items_json)}],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1])
        else:
            text = "\n".join(lines[1:])
    rankings = json.loads(text)

    out: list[tuple[int, Candidate, str]] = []
    for r in rankings:
        try:
            idx = int(r["index"])
            score = int(r["score"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= idx < len(batch):
            out.append((score, batch[idx], (r.get("reason") or "")[:200]))
    return out


def rank_with_claude(
    client: Anthropic, candidates: list[Candidate]
) -> tuple[list[tuple[int, Candidate, str]], set[str]]:
    """Rank all candidates in batches.

    Returns (scored_results, ranked_urls) — `ranked_urls` is the set of URLs
    that were actually ranked in a successful batch. Used so state-write only
    marks evaluated those that completed; failed batches retry next run.
    """
    if not candidates:
        return [], set()
    all_scored: list[tuple[int, Candidate, str]] = []
    ranked_urls: set[str] = set()
    for batch_start in range(0, len(candidates), RANK_BATCH_SIZE):
        batch = candidates[batch_start : batch_start + RANK_BATCH_SIZE]
        log.info(f"  ranking batch {batch_start}-{batch_start + len(batch)} ({len(batch)} items)")
        try:
            scored = _rank_batch(client, batch)
        except Exception as exc:
            log.warning(f"  batch failed (will retry next run): {exc}")
            continue
        all_scored.extend(scored)
        for c in batch:
            ranked_urls.add(c.url)
    all_scored.sort(key=lambda x: x[0], reverse=True)
    return all_scored, ranked_urls


def _run() -> int:
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return 1

    state = load_curator_state()
    evaluated = state.setdefault("evaluated", {})

    candidates = fetch_all_feeds()
    fresh = [c for c in candidates if c.url not in evaluated and not is_known(c.url)]
    log.info(f"fresh after dedupe: {len(fresh)}")
    if not fresh:
        log.info("nothing new to consider")
        return 0

    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    scored, ranked_urls = rank_with_claude(anthropic_client, fresh)
    if not scored:
        log.warning("ranking produced no usable results — leaving curator state untouched so we retry next run")
        return 1

    # Only mark as evaluated those URLs whose batch actually succeeded.
    timestamp = datetime.now(timezone.utc).isoformat()
    for c in fresh:
        if c.url in ranked_urls:
            evaluated[c.url] = {"source": c.source, "evaluated_at": timestamp}
    save_curator_state(state)

    qualifying = [(s, c, r) for s, c, r in scored if s >= RANKING_THRESHOLD]
    selected = qualifying[:DAILY_CAP]
    log.info(f"qualifying ≥{RANKING_THRESHOLD}: {len(qualifying)}; selecting top {len(selected)}")

    added = 0
    for score, c, reason in selected:
        if add_to_queue(c.url, title=c.title, source=f"curator/{c.source}"):
            log.info(f"  +{score} [{c.source}] {c.title[:80]} — {reason[:80]}")
            added += 1

    log.info(f"added {added} to queue (cap {DAILY_CAP}, threshold {RANKING_THRESHOLD})")
    return 0


def main() -> int:
    try:
        return _run()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        log.exception("curator crashed")
        if SLACK_BOT_TOKEN:
            try:
                slack = WebClient(token=SLACK_BOT_TOKEN)
                notify_admin(slack, f":rotating_light: Hugo curator crashed\n```\n{tb}\n```")
            except Exception:
                pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
