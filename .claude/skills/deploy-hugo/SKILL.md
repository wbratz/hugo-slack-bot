---
name: deploy-hugo
description: Deploy local Hugo code changes to the NAS — push source, rebuild the Docker image, restart every instance, and verify via the bot log. Use when the user says "deploy hugo", "update hugo", "push hugo", "ship it", or has just edited Hugo's source and wants it live.
---

# Deploy Hugo

Ships the current local Hugo code to the NAS where it runs.

## Steps

1. **Sanity-check what changed.** Run `git status` / `git diff --stat`. If there are
   uncommitted changes, ask whether to commit first — the deploy works from the working
   tree (not from git), but committing keeps history clean and lets you roll back.

2. **Run the deploy script** from the repo root:
   ```powershell
   .\deploy.ps1
   ```
   It pushes `*.py`, `Dockerfile`, and `requirements.txt` to the primary NAS folder,
   rebuilds the shared `hugo:latest` image, restarts the `bot` container in every
   configured instance, and tails the bot log. The user may be prompted for their SSH
   and sudo passwords (that's expected — the terminal handles it, not you).

3. **Verify from the tailed log** the script prints at the end. Confirm a fresh
   `Hugo bot starting (Socket Mode)` + `⚡️ Bolt app is running!` with a current
   timestamp, and no tracebacks. If you see a rate-limit warning on
   `announce_new_features`, that's non-fatal — the bot still starts.

4. **If this deploy added a feature** (a new `Feature(...)` entry in `hugo_features.py`),
   tell the user Hugo will auto-announce it in the announce channel on restart.

## Notes

- Connection details (NAS host, user, port, instance paths) live in `deploy.local.ps1`,
  which is gitignored. If it's missing, `deploy.ps1` prints how to create it from
  `deploy.local.ps1.example`.
- Only the **primary** folder is built; other instances share that image and just get
  their bot restarted.
- The routine deploy only ships image source. If `docker-compose.yml` structure changed
  (mounts, services), push it manually — it's not part of the automatic push.
- The curator and digest run on their own DSM Task Scheduler cron, using the same rebuilt
  image, so they pick up code changes automatically on their next scheduled run. No extra
  step needed for them.
