# Autonomous Daily Trend-to-Instagram Reels Bot (2+ Minute Kinetic Edition)

A zero-cost, cloud-automated Python pipeline that discovers daily trending topics, writes a **2-to-2.5-minute (125–155s)** deep-dive script using Google Gemini, synthesizes neural voiceovers with word-level timestamps via Microsoft Edge TTS, renders a 30fps 1080x1920 vertical video with **Ken Burns camera motion, word-by-word karaoke pop captions, and an animated audio equalizer waveform** via FFmpeg, and publishes directly to **Instagram Reels** on a daily GitHub Actions schedule.

## Architecture & Free Stack

| Pipeline Stage | Free Tool / API Used | Cost / Key Requirement |
| :--- | :--- | :--- |
| **1. Trend Analysis** | [Google Trends RSS](https://trends.google.com/trending), Google News RSS, and Hacker News API | Free, no API key required |
| **2. Script & Metadata** | [Google Gen AI SDK (`google-genai`)](https://pypi.org/project/google-genai/) (`gemini-2.5-flash`) | Free tier via Google AI Studio (`GEMINI_API_KEY`) |
| **3. Neural Voiceover** | [`edge-tts`](https://pypi.org/project/edge-tts/) (`WordBoundary` streaming) | Free, no API key required |
| **4. Kinetic Renderer** | [Pexels API](https://www.pexels.com/api/documentation/) + [Pollinations.ai](https://gen.pollinations.ai/docs) + `FFmpeg` & `Pillow` | Free (`PEXELS_API_KEY` optional; Pollinations is keyless) |
| **5. Instagram Upload** | [Instagram Graph API Reels Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing) (Resumable Upload) or `instagrapi` | Free Meta Developer App token or account session |
| **6. Cloud Scheduler** | GitHub Actions (`.github/workflows/daily_reel.yml`) | Free (2,000 mins/month on private repos, unlimited on public repos) |
