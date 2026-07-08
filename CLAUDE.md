# Hugo

Personal Slack bot for a Slack workspace. Started as a daily reading digest; grew into a multi-purpose chat assistant.

> **Real deployment values (NAS host, SSH user/port, concrete paths) live in `DEPLOYMENT.local.md`, which is gitignored.** This file uses placeholders like `<nas-user>@<nas-host>` and `<ssh-port>`. If you're operating a live instance, read `DEPLOYMENT.local.md` for the actual copy-paste commands.

## What Hugo does today

- **Auto-curator** — at ~6am daily, scans AI/tech RSS feeds (`hugo_curator.py`'s `FEEDS` list), asks Claude to score each candidate for relevance, adds the top picks to the digest queue.
- **Reading digest** — on weekday mornings at 8am, posts the best `HUGO_DIGEST_MAX_POSTS` (default 3) curator picks by score, plus any manual saves, to `HUGO_DIGEST_CHANNEL`. Skips Sat/Sun (the curator keeps running, so Monday picks the best of the weekend pool). Non-selected curator picks are discarded so the pool stays fresh.
- **Manual save** — `:bookmark:` reaction on any message with a URL, or `@Hugo queue <url>`, adds it to the digest queue.
- **Workspace greeter** — DMs new members with the intro; posts a public hello in `HUGO_WELCOME_CHANNEL`. Toggle with `HUGO_WELCOME_ENABLED`.
- **On-demand URL summary** — `@Hugo <url>` or `:books:` reaction → summary in thread (right now, not next morning).
- **Thread TL;DR** — `@Hugo tldr` / `recap` / `catchup` inside a thread → summary of who said what, decisions, open questions.
- **Help & status reactions** — `@Hugo help` lists features; Hugo reacts `:eyes:` while working, `:white_check_mark:` when done, `:bookmark_tabs:` after queuing.
- **Feature announcer** — on bot startup, announces new entries from `hugo_features.py` in `HUGO_ANNOUNCE_CHANNEL`.
- **Crash alerts** — any unhandled exception in any container DMs `HUGO_ADMIN_USER_ID` with the traceback.

## Architecture

| File | Purpose |
|------|---------|
| `daily_digest.py` | Scheduled job. Reads `~/.hugo/queue.json` pending entries, summarizes each, posts the digest, moves them to posted. |
| `hugo_curator.py` | Scheduled job. Pulls RSS feeds, dedupes against state, scores with Claude, adds top picks to the queue. |
| `hugo_queue.py` | Shared queue API: `add_to_queue`, `get_pending`, `mark_posted`, `is_known`. State at `~/.hugo/queue.json`. |
| `hugo_bot.py` | Persistent Socket Mode listener. All Slack event handlers live here. |
| `hugo_common.py` | Shared: env loading, logging setup, channel name → ID resolution, `notify_admin`. |
| `hugo_summarize.py` | Shared: `fetch_article` (trafilatura), `summarize_article`, `summarize_thread`. Side-effect-light. |
| `hugo_features.py` | Source of truth for the feature changelog used by the announce-on-startup flow. |
| `extension/` | **DEPRECATED.** Old Chrome MV3 extension that exported the reading list. Replaced by the auto-curator + `:bookmark:` reaction. Kept for reference. |
| `Dockerfile` / `docker-compose.yml` | Container build + orchestration. Three services: `bot` (always-on), `digest` (profile-gated daily), `curator` (profile-gated daily). |

## Deployment

Hugo runs entirely in Docker on a Synology NAS under `/volume1/docker/hugo/`. No Windows dependency remains at runtime. (Actual host/user/port in `DEPLOYMENT.local.md`.)

```
                          Synology NAS
                          ────────────
                          /volume1/docker/hugo/
                          ├─ Dockerfile
                          ├─ docker-compose.yml
                          ├─ .env                    (secrets — never committed)
                          ├─ *.py
                          ├─ requirements.txt
                          └─ state/                  (mounted → /root/.hugo)
                              ├─ hugo.log
                              ├─ queue.json
                              ├─ curator_state.json
                              ├─ announced_features.json
                              └─ digest_cron.log
```

Volume mounts (in `docker-compose.yml`):

- `./.env` → `/app/.env` (read-only)
- `./state/` → `/root/.hugo/`

Containers:

- `hugo-bot` — `restart: unless-stopped`, runs `python hugo_bot.py` continuously. Auto-starts on Container Manager / NAS boot.
- `hugo-digest` — profile `digest`. DSM Task Scheduler fires `docker compose run --rm digest` daily at 8am.
- `hugo-curator` — profile `curator`. DSM Task Scheduler fires `docker compose run --rm curator` daily at 6am.

## Workflows

All commands assume you're SSH'd into the NAS at `/volume1/docker/hugo/`. Substitute the placeholders (`<nas-user>`, `<nas-host>`, `<ssh-port>`) with the real values from `DEPLOYMENT.local.md`.

### Adding a new feature

1. Implement it locally. Commit.
2. Append a `Feature(id=..., title=..., description=...)` entry to the bottom of `FEATURES` in `hugo_features.py`. ID is a snake_case slug, unique.
3. If user-facing, update `HELP_TEXT` and (if appropriate) `INTRO_DM` in `hugo_bot.py`.
4. Deploy — one command from the repo root:
   ```powershell
   .\deploy.ps1
   ```
   This pushes source, rebuilds the shared image, restarts every instance's bot, and tails the log. (In Claude Code, the `/deploy-hugo` skill runs this and verifies the result.) It reads NAS connection details from `deploy.local.ps1` (gitignored — copy from `deploy.local.ps1.example`).
5. Hugo auto-announces the new feature in `HUGO_ANNOUNCE_CHANNEL` on restart.

The manual equivalent, if you ever need it: `scp -O -P <ssh-port> <files> <nas-user>@<nas-host>:/volume1/docker/hugo/`, then SSH in and `sudo docker compose build && sudo docker compose up -d bot` in each instance folder.

### Editing the curator's feed list

Edit `FEEDS` in `hugo_curator.py` (add/remove/reorder URLs). Then push the file and rebuild — same flow as adding a feature. No state migration needed; the curator just picks up the new list on its next run.

### Force-announcing a feature (testing / backfill)

```bash
sudo docker compose run --rm bot python hugo_bot.py --announce-feature <feature_id>
```

### Testing the greeting flow

```bash
sudo docker compose run --rm bot python hugo_bot.py --test-greet <SLACK_USER_ID>
```

### Running the digest manually

```bash
sudo docker compose run --rm digest
```
If the queue is empty, exits cleanly with "nothing pending".

### Running the curator manually

```bash
sudo docker compose run --rm curator
```
Useful to seed the queue right after deploying changes. Fetches all feeds, ranks, adds top picks to queue. Updates `~/.hugo/curator_state.json` so the same entries aren't re-ranked tomorrow.

### Tailing the bot log live

```bash
sudo docker compose logs -f bot
```

Or read the persistent log file:
```bash
tail -f /volume1/docker/hugo/state/hugo.log
```

### Inspecting the queue

```bash
cat /volume1/docker/hugo/state/queue.json | jq '.pending | length, .posted | length'
```
(`jq` is preinstalled on Synology.) `.pending` is what's waiting for the next digest; `.posted` is what's been summarized historically.

### Adding a new Slack scope

1. Slack app management → OAuth & Permissions → Bot Token Scopes → add scope.
2. Slack prompts to reinstall (yellow banner) → click → Allow.
3. Bot token may rotate — re-copy and update `SLACK_BOT_TOKEN` in the NAS `.env`.
4. Restart the bot: `sudo docker compose restart bot`.

## `.env` (on the NAS, never committed)

Required:

- `ANTHROPIC_API_KEY` — Anthropic API key
- `SLACK_BOT_TOKEN` — bot user OAuth token (`xoxb-...`)
- `SLACK_APP_TOKEN` — app-level token for Socket Mode (`xapp-...`)
- `HUGO_ADMIN_USER_ID` — Slack user ID to DM on crashes

Optional (defaults shown):

- `HUGO_WORKSPACE_NAME="the workspace"` — used in INTRO_DM + PUBLIC_GREETING. Set per-instance to your workspace's name.
- `HUGO_WELCOME_ENABLED=true` — master switch for the team_join greeting. Set to `false` to disable greeting entirely on a workspace (digest + bot remain on).
- `HUGO_DIGEST_CHANNEL=#ai-summaries` — where the daily digest posts
- `HUGO_WELCOME_CHANNEL` — where the public part of the greeting posts (the DM still goes if welcome is enabled, even without this set; public greeting requires both)
- `HUGO_ANNOUNCE_CHANNEL=#general` — where feature announcements post
- `CLAUDE_MODEL=claude-sonnet-4-6` — model used for summaries + curator ranking
- `HUGO_CURATOR_DAILY_CAP=3` — max curator picks added to the queue per run
- `HUGO_CURATOR_THRESHOLD=6` — minimum Claude relevance score (1-10) to qualify
- `HUGO_DIGEST_MAX_POSTS=3` — max curator picks the digest posts per run (best-by-score). Manual `:bookmark:` saves always post on top of this and are never culled.
- `HUGO_TZ=UTC` — timezone for the weekend check and digest date header. **Set this to your local zone** (e.g. `America/Phoenix`) so the Sat/Sun skip lands on your weekend, not UTC's.

See `.env.example` for a starting template. When editing `.env` on the NAS, **always end the file with a newline** — `echo "X=Y" >> .env` will concatenate onto the previous line if the file lacked a trailing newline. Verify with `cat .env` after any append.

## Slack scopes (current)

Bot token scopes in use:

- `chat:write`, `chat:write.customize` — post messages, customize sender
- `users:read` — resolve user IDs → display names (used by thread TL;DR)
- `im:write`, `im:read`, `im:history` — DMs
- `channels:read`, `channels:history`, `channels:join` — public channels
- `groups:read` — private channel discovery (history not yet requested)
- `reactions:read`, `reactions:write` — receive + add reactions
- `app_mentions:read` — `@-mention` events
- `links:read` — URL unfurling (passive)
- `incoming-webhook` — **legacy**, no longer used; safe to revoke

Event subscriptions:

- `team_join` — workspace join
- `app_mention` — `@-mentions`
- `reaction_added` — emoji reactions
- `message.im` — direct messages

## Scheduled jobs (DSM Task Scheduler)

| Task name | Runs | Schedule | User |
|-----------|------|----------|------|
| `Hugo Curator` | `cd /volume1/docker/hugo && <docker-path> compose run --rm curator >> state/curator_cron.log 2>&1` | Daily 06:00 | root |
| `Hugo Daily Digest` | `cd /volume1/docker/hugo && <docker-path> compose run --rm digest >> state/digest_cron.log 2>&1` | Daily 08:00 | root |

Both cron entries stay **daily** — the curator runs every day (so weekend finds accumulate), and the digest self-skips Sat/Sun in code based on `HUGO_TZ`. No need to configure a weekday-only schedule in DSM. Run `digest --force` to post on a weekend manually: `docker compose run --rm digest python daily_digest.py --force`.

`<docker-path>` is the absolute path to the docker binary on the NAS (find with `which docker`; recorded in `DEPLOYMENT.local.md`).

The bot is *not* in Task Scheduler — `restart: unless-stopped` keeps the container alive.

Run manually: DSM → Control Panel → Task Scheduler → right-click task → Run.

Logs:
```bash
tail -50 /volume1/docker/hugo/state/curator_cron.log
tail -50 /volume1/docker/hugo/state/digest_cron.log
```

## Running Hugo in multiple Slack workspaces

Hugo is single-tenant per Slack app token — for a second workspace, run a second instance side-by-side on the NAS.

Layout:

```
/volume1/docker/
├── hugo/                   ← primary instance
└── hugo-<workspace>/       ← additional instance
    ├── docker-compose.yml  ← references same hugo:latest image
    ├── .env                ← second workspace's tokens + HUGO_WORKSPACE_NAME
    └── state/              ← separate queue / posted / announced state
```

Both instances **share the same `hugo:latest` image** (no `build:` in the secondary's compose) — rebuild via the primary's compose file and both pick up the new image on next restart. Container names are namespaced via the compose `name:` field so they don't collide.

To stand up a new workspace instance:

1. In Slack: create a new Slack app named Hugo in the second workspace, repeat the original setup (Socket Mode, all the same Bot Token Scopes and event subscriptions, install, enable Messages tab). Grab the new `xoxb-` and `xapp-` tokens.
2. On the NAS:
   ```bash
   sudo mkdir -p /volume1/docker/hugo-<workspace>/state
   sudo chown -R $USER:users /volume1/docker/hugo-<workspace>
   ```
3. Copy `docker-compose.instance.yml.example` from the primary folder to `/volume1/docker/hugo-<workspace>/docker-compose.yml`. Replace every occurrence of `CHANGEME` with a unique slug (e.g. `acme`).
4. Create the `.env` (same required vars as the primary, plus a `HUGO_WORKSPACE_NAME=<Workspace Name>` line so the intro / greeting copy matches). For a digest-only instance, also set `HUGO_WELCOME_ENABLED=false`.
5. `cd /volume1/docker/hugo-<workspace> && sudo docker compose up -d bot`
6. Add two more DSM Task Scheduler entries pointing at the new folder (curator + digest), same shape as the primary's, with the new path.

Updating both instances after a code change: rebuild the image in the primary folder (`cd /volume1/docker/hugo && sudo docker compose build`), then restart each instance's bot (`sudo docker compose up -d bot` from each folder). State stays separate; behavior is identical.

## Personality / brand guidance

Hugo is "the workspace's resident AI bouncer, librarian, and chaos translator." Built like a final boss, tuned like an intern with unlimited caffeine. Turns rabbit holes into useful signal. (The workspace name is injected at runtime from `HUGO_WORKSPACE_NAME`.)

Hugo's messages should feel:

- Concise. Don't waste people's time.
- Slightly dry / wry. Not corporate. Not bot-speak.
- Honest when things break — say what failed, don't fake-cheerful past it.

Avoid: emoji-heavy fluff, "I'm just a bot but..." disclaimers, "Sure thing!" openers.

## Known limitations

- Reactions on thread *replies* (not top-level messages) can't be summarized or queued via the emoji flows — `conversations.history` doesn't return replies. Reactions on top-level messages work fine.
- The auto-curator's quality is bounded by the feeds in `FEEDS` and Claude's scoring. If too many low-signal items get through, raise `HUGO_CURATOR_THRESHOLD` (default 6). If too few, lower it or add feeds.
- Both bot and curator use the same bot token. If you ever split them, each piece would need its own Slack client identity.
- Resolving `#channel` names to IDs calls `conversations.list`, which is rate-limited on fresh Slack apps. If you hit `ratelimited` errors on startup, put channel **IDs** (e.g. `C0…`) in the `*_CHANNEL` env vars instead of names — `resolve_channel_id` passes non-`#` values straight through with no API call.

## Retired / legacy

- **Chrome reading list pipeline** — a browser extension exported the reading list to a synced folder that the digest read. Retired in favor of the auto-curator + `:bookmark:` reaction. The `extension/` directory is kept for reference but is unused.
- **Windows Task Scheduler tasks** — an earlier deployment ran the bot + digest on Windows. Superseded by the NAS Docker deployment.
