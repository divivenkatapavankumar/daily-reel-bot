# Autonomous Daily Instagram Reel Bot — Antigravity Workspace Rules

Always follow these architectural invariants when modifying, testing, or running this project:

1. **Instagram-Only Zero-Cost API Constraint**:
   - Trend discovery: Google Trends RSS (`https://trends.google.com/trending/rss`), Google News RSS, and Hacker News Firebase API.
   - Script generation: Google Gen AI SDK (`google-genai` with `gemini-2.5-flash`).
   - Voiceover synthesis & word timings: `edge-tts` neural voices (`WordBoundary` streaming, no API key required).
   - Visual assets: Pexels Portrait Video API (`PEXELS_API_KEY`) with automatic fallback to Pollinations.ai (`https://image.pollinations.ai`) and `Pillow` cyber-grid backgrounds.
   - Publishing: Instagram Reels ONLY via official Meta Graph API Resumable Upload (`IG_USER_ID` + `IG_ACCESS_TOKEN`) or `instagrapi` (`IG_USERNAME` + `IG_PASSWORD` + `IG_SESSION_JSON`).
2. **2+ Minute Video Duration Floor (`125s` to `175s`)**:
   - Every generated script must contain **12 to 15 scenes** (`320–380` total spoken words, `25–30` words per scene).
   - Rendered videos must strictly enforce `MIN_VIDEO_SECONDS=125` (2 minutes 5 seconds) and `MAX_VIDEO_SECONDS=175`.
3. **Kinetic Animation & Motion Graphics Spec (`1080x1920`, `30fps`)**:
   - Every scene must apply continuous 30fps Ken Burns camera motion (alternating diagonal push-in, pull-out, horizontal pan, and vertical rise on a `1350x2400` overscaled canvas) with scene fade-in/out transitions.
   - Captions must use word-by-word karaoke highlighting (4 words per phrase, with the active spoken word popped at 118% scale inside a neon pill badge), accompanied by a 21-bar animated audio equalizer waveform, pulsing `LIVE TREND` HUD badge, scene counter (`SCENE 01/14`), and continuous bottom timeline progress bar.
4. **Verification**:
   - Always test pipeline changes locally with `python daily_reel_bot.py --dry-run` and verify the output `.mp4` duration via `ffprobe` before pushing to GitHub Actions.
