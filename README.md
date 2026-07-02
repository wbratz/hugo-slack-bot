# Hugo

A self-hosted Slack bot that reads the internet so your team doesn't have to.

Hugo scans AI/tech RSS feeds every morning, uses Claude to score each article
for relevance, and posts a summarized digest to a Slack channel. Beyond the
daily digest he's a general-purpose reading assistant: summarize any URL on
demand, TL;DR a noisy thread, save links for tomorrow's digest, and greet new
members — all from Slack mentions, DMs, or emoji reactions.

## Features

- **Auto-curated digest** — pulls a configurable set of RSS feeds, ranks every
  candidate with Claude against a tunable relevance prompt, and posts the top
  picks as a summarized digest each morning.
- **On-demand summaries** — `@Hugo <url>` or react `:books:` on any message with
  a link; Hugo replies in thread with a tight TL;DR.
- **Thread TL;DR** — `@Hugo tldr` inside a thread → who said what, decisions
  made, questions left open.
- **Save for later** — react `:bookmark:` or `@Hugo queue <url>` to add a link to
  the next digest.
- **DMs** — everything works in a direct message too, to keep channels quiet.
- **New-member greeter** — optional welcome DM + public hello on `team_join`.
- **Self-announcing** — Hugo posts an announcement when a new feature ships.
- **Crash alerts** — unhandled exceptions get DM'd to an admin.

## How it works

```
RSS feeds ──► curator (Claude ranks) ──┐
                                        ├──► queue.json ──► digest (Claude summarizes) ──► Slack
manual saves (:bookmark: / queue) ──────┘
```

Three Python entrypoints share a small set of modules:

- `hugo_bot.py` — always-on Slack Socket Mode listener (mentions, reactions, DMs)
- `hugo_curator.py` — scheduled: fetch feeds, rank, enqueue
- `daily_digest.py` — scheduled: drain queue, summarize, post

Shared helpers: `hugo_common.py` (env/logging/Slack utils), `hugo_summarize.py`
(fetch + summarize), `hugo_queue.py` (the queue), `hugo_features.py` (changelog
for the self-announcer).

## Stack

- Python 3.12
- [slack-bolt](https://slack.dev/bolt-python/) over Socket Mode (no public URL needed)
- [Anthropic API](https://docs.anthropic.com/) (Claude) for ranking + summarization
- [trafilatura](https://trafilatura.readthedocs.io/) for article extraction
- [feedparser](https://feedparser.readthedocs.io/) for RSS
- Docker + Docker Compose for deployment

## Running it

1. Create a Slack app with Socket Mode enabled and the scopes listed in
   `CLAUDE.md` (chat, reactions, users, channels, im, app_mentions).
2. Copy `.env.example` to `.env` and fill in your Anthropic key, Slack tokens,
   and channel preferences.
3. Build and run:
   ```bash
   docker compose up -d bot                    # always-on listener
   docker compose run --rm curator             # fetch + rank (schedule at ~6am)
   docker compose run --rm digest              # summarize + post (schedule at ~8am)
   ```
4. Schedule the curator and digest with cron or your platform's task scheduler.

Full operational docs — architecture, workflows, Slack setup, multi-workspace
deployment, tuning the curator — are in [`CLAUDE.md`](./CLAUDE.md).

## Configuration

All configuration is via environment variables (see `.env.example` and the
`.env` section of `CLAUDE.md`). Highlights:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API access |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Bot + Socket Mode tokens |
| `HUGO_DIGEST_CHANNEL` | Where the digest posts |
| `HUGO_CURATOR_DAILY_CAP` | Max articles per digest |
| `HUGO_CURATOR_THRESHOLD` | Minimum relevance score (1-10) to qualify |
| `HUGO_WELCOME_ENABLED` | Toggle the new-member greeter |

Tune Hugo's taste by editing the `FEEDS` list and `RANK_PROMPT` in
`hugo_curator.py`.

## License

MIT — see [`LICENSE`](./LICENSE).
