---
name: daily-reel-pipeline
description: Scaffolds, tests, runs, and deploys the autonomous 2+ minute daily trend-to-Instagram Reels pipeline. Use when setting up Instagram API credentials, running a dry-run kinetic video build, scheduling daily runs, or deploying to GitHub Actions.
---

# Daily Instagram Reel Pipeline Skill

Follow these steps when asked to set up, test, or deploy the daily Instagram Reel bot in Google Antigravity:

## 1. Environment & Dependency Setup
1. Verify `ffmpeg` and `ffprobe` are installed in the terminal (`ffmpeg -version`). If missing on Linux, run `sudo apt-get update && sudo apt-get install -y ffmpeg fonts-liberation` (or `brew install ffmpeg` on macOS).
2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` if `.env` does not exist, and prompt the user to populate `GEMINI_API_KEY` (and optional `PEXELS_API_KEY`).

## 2. Dry-Run Verification (Artifact Check)
1. Execute a local dry run before enabling live Instagram uploads:
   ```bash
   set -a && source .env && set +a
   python daily_reel_bot.py --dry-run
   ```
2. Inspect the generated `output/reel_*.mp4` and `output/reel_*.json` sidecar file.
3. Run `ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 output/reel_*.mp4` to confirm the duration is between `125` and `175` seconds.

## 3. Instagram Credentials Setup
- **Option A (Official Meta Graph API — Recommended)**: Set `IG_USER_ID` and `IG_ACCESS_TOKEN` in `.env`.
- **Option B (`instagrapi` Direct Login)**: Set `IG_USERNAME` and `IG_PASSWORD` in `.env`, then run `python daily_reel_bot.py --setup-ig-session` once locally to generate `IG_SESSION_JSON` so GitHub Actions reuses your trusted device session without login challenges.

## 4. Deployment & Scheduling
- **Cloud (GitHub Actions)**: Use the GitHub CLI (`gh secret set < .env`) or GitHub MCP server to upload the secrets from `.env` to the repository, then trigger `.github/workflows/daily_reel.yml` via `gh workflow run daily_reel.yml -f dry_run=true`.
- **Local Antigravity Schedule**: Alternatively, use the `/schedule` slash command in the Antigravity App to run `python daily_reel_bot.py` daily on the local workstation.
