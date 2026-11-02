# Autonomous Daily Reel & YouTube Shorts Bot — Antigravity Workspace Rules

Always follow these architectural invariants when modifying, testing, or running this project:

1. **Zero-Cost API Constraint**: Use only free-tier or keyless libraries and endpoints:
   - Trend discovery: Google Trends RSS (`https://trends.google.com/trending/rss`), Google News RSS, and Hacker News Firebase API.
   - Script generation: Google Gen AI SDK (`google-genai` with `gemini-2.5-flash`).
   - Voiceover synthesis: `edge-tts` neural voices (no API key required).
   - Visual assets: Pexels Portrait Video API (`PEXELS_API_KEY`) with automatic fallback to Pollinations.ai (`https://image.pollinations.ai`) and `Pillow` procedural backgrounds.
2. **2+ Minute Video Duration Floor (`125s` to `175s`)**:
   - Every generated script must contain **12 to 15 scenes** (`320–380` total spoken words, `25–30` words per scene).
   - Rendered videos must strictly enforce `MIN_VIDEO_SECONDS=125` (2 minutes 5 seconds) and `MAX_VIDEO_SECONDS=175` (under the 3-minute YouTube Shorts limit).
3. **Vertical 9:16 Video Spec**:
   - Output resolution must be `1080x1920` at `30fps`, `libx264` (`yuv420p`), and `aac` audio (`192k`) for compatibility with both YouTube Shorts and the Instagram Graph API (`media_type=REELS`).
4. **Verification**:
   - Always test pipeline changes locally with `python daily_reel_bot.py --dry-run` and verify the output `.mp4` duration via `ffprobe` before pushing to GitHub Actions.
