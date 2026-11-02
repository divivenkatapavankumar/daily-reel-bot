---
name: daily-reel-pipeline
description: Scaffolds, tests, runs, and deploys the autonomous 2+ minute daily trend-to-Reel and YouTube Shorts pipeline. Use when setting up API credentials, running a dry-run video build, scheduling daily runs, or deploying to GitHub Actions.
---

# Daily Reel & YouTube Shorts Pipeline Skill

Follow these steps when asked to set up, test, or deploy the daily Reel/Short bot in Google Antigravity:

## 1. Environment & Dependency Setup
1. Verify `ffmpeg` and `ffprobe` are installed in the terminal (`ffmpeg -version`). If missing on Linux, run `sudo apt-get update && sudo apt-get install -y ffmpeg fonts-liberation` (or `brew install ffmpeg` on macOS).
2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` if `.env` does not exist, and prompt the user to populate `GEMINI_API_KEY` (and optional `PEXELS_API_KEY`).

## 2. One-Time YouTube OAuth Refresh Token Setup
1. If `YOUTUBE_REFRESH_TOKEN` is not set, instruct the user to place their Desktop `client_secret.json` from Google Cloud Console into the workspace root.
2. Run `python daily_reel_bot.py --setup-youtube` to launch the local OAuth flow and capture `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, and `YOUTUBE_REFRESH_TOKEN`.

## 3. Dry-Run Verification (Artifact Check)
1. Execute a local dry run before enabling live uploads:
   ```bash
   set -a && source .env && set +a
   python daily_reel_bot.py --dry-run
   ```
2. Inspect the generated `output/reel_*.mp4` and `output/reel_*.json` sidecar file.
3. Run `ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 output/reel_*.mp4` to confirm the duration is between `125` and `175` seconds.

## 4. Deployment & Scheduling
- **Cloud (GitHub Actions)**: Use the GitHub CLI (`gh secret set`) or GitHub MCP server to upload the secrets from `.env` to the repository, then trigger `.github/workflows/daily_reel.yml` via `gh workflow run daily_reel.yml -f dry_run=true`.
- **Local Antigravity Schedule**: Alternatively, use the `/schedule` slash command in the Antigravity App to run `python daily_reel_bot.py` daily on the local workstation.
