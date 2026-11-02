# Autonomous Daily Trend-to-Reel & YouTube Shorts Bot (2+ Minute Edition)

A zero-cost, cloud-automated Python pipeline that discovers daily trending topics, writes a **2-to-2.5-minute (125–155s)** deep-dive script using Google Gemini, synthesizes neural voiceovers with Microsoft Edge TTS, renders a 1080x1920 vertical video with dynamic captions via FFmpeg, and publishes directly to **YouTube Shorts** and **Instagram Reels** on a daily GitHub Actions schedule.

## Duration & Platform Compatibility (2 to 3 Minutes)

* **Target Length (`125s` to `155s`):** Gemini is prompted for **12 to 15 scenes** (`320–380` spoken words, with an automatic expansion pass if the first draft is under 290 words), and `assemble_full_reel()` enforces a floor of `MIN_VIDEO_SECONDS=125` (2 minutes 5 seconds) and a ceiling of `MAX_VIDEO_SECONDS=175` (2 minutes 55 seconds).
* **YouTube Shorts & Instagram Reels Support:** Both YouTube Shorts and Instagram Reels support vertical 9:16 videos up to **3 minutes (180 seconds)**, so a 2-to-2.5-minute video qualifies natively on both platforms.

## Architecture & Free Stack

| Pipeline Stage | Free Tool / API Used | Cost / Key Requirement |
| :--- | :--- | :--- |
| **1. Trend Analysis** | [Google Trends RSS](https://trends.google.com/trending), Google News RSS, and Hacker News API | Free, no API key required |
| **2. Script & Metadata** | [Google Gen AI SDK (`google-genai`)](https://pypi.org/project/google-genai/) (`gemini-2.5-flash`) | Free tier via Google AI Studio (`GEMINI_API_KEY`) |
| **3. Neural Voiceover** | [`edge-tts`](https://pypi.org/project/edge-tts/) | Free, no API key required |
| **4. Visuals & Rendering** | [Pexels API](https://www.pexels.com/api/documentation/) (portrait stock video) + [Pollinations.ai](https://gen.pollinations.ai/docs) (1080x1920 AI images) + `FFmpeg` & `Pillow` | Free (`PEXELS_API_KEY` optional; Pollinations is keyless) |
| **5. YouTube Upload** | [YouTube Data API v3](https://developers.google.com/youtube/data/guides/uploading_a_video) | Free Google Cloud OAuth 2.0 Refresh Token |
| **6. Instagram Upload** | [Instagram Graph API Reels Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing) (Resumable Upload) or `instagrapi` | Free Meta Developer App token or account session |
| **7. Cloud Scheduler** | GitHub Actions (`.github/workflows/daily_reel.yml`) | Free (2,000 mins/month on private repos, unlimited on public repos) |
