#!/usr/bin/env python3
"""
Autonomous Daily Trend-to-Instagram Reels Bot (2+ Minute Kinetic Animation Edition)
===================================================================================
End-to-end Instagram Reels pipeline designed for daily execution via GitHub Actions (or cron):
1. Trend Discovery: Pulls daily trending topics from Google Trends RSS, Google News RSS, and Hacker News API.
2. AI Scriptwriter: Uses Google Gemini (`google-genai` free tier) to select the top trend and write a
   12-to-15-scene deep-dive vertical Reel script (320-380 spoken words, targeting 125-155 seconds / 2+ minutes).
3. Neural Voiceover + Word Timestamps: Generates spoken audio per scene using `edge-tts` (`WordBoundary`
   streaming) to extract exact word-by-word timing for karaoke captions.
4. Kinetic 9:16 Video Engine (FFmpeg + Pillow):
   - 4-mode 30fps Ken Burns camera motion (Push-In Zoom, Pull-Out Reveal, Pan Left-to-Right, Tilt Upward)
   - Hormozi/CapCut-style word-by-word active highlight captions with pop-scale neon word pills
   - Animated audio waveform visualizer bars, pulsing LIVE TREND HUD badge, scene counter (`03/14`),
     slide-in headline banner, and a continuous timeline progress bar
   - Crisp scene-to-scene fade transitions and color-grade punch (`1080x1920`, `30fps`, `H.264`)
5. Instagram Reels Uploader: Publishes automatically to Instagram Reels via Official Meta Graph API
   Resumable Upload (`IG_USER_ID` + `IG_ACCESS_TOKEN`) or `instagrapi` (`IG_USERNAME` + `IG_PASSWORD` / `IG_SESSION_JSON`).

Usage:
  python daily_reel_bot.py                      # Run full pipeline (generate 2+ min animated Reel + upload to Instagram)
  python daily_reel_bot.py --dry-run            # Generate 2+ min animated Reel locally without uploading
  python daily_reel_bot.py --setup-ig-session   # Optional: Log in once locally via instagrapi to export IG_SESSION_JSON
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import textwrap
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("daily_reel_bot")


def _load_env_file(dotenv_path: Path | None = None) -> None:
    if dotenv_path is None:
        dotenv_path = Path(__file__).resolve().parent / ".env"
    if dotenv_path.is_file():
        for line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v


_load_env_file()


def _ensure_ffmpeg_in_path() -> None:
    root = Path(__file__).resolve().parent
    candidates = [
        root / ".venv" / "Scripts",
        root / ".venv" / "bin",
    ]
    winget_pkg = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget_pkg.is_dir():
        for p in winget_pkg.glob("**/ffmpeg.exe"):
            candidates.append(p.parent)
            break
    for c in candidates:
        if c.is_dir() and str(c) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(c) + os.pathsep + os.environ.get("PATH", "")


_ensure_ffmpeg_in_path()


# ---------------------------------------------------------------------------
# Configuration & Data Models
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip())
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip())
    trend_geo: str = field(default_factory=lambda: os.getenv("TREND_GEO", "IN").strip())
    niche_filter: str = field(
        default_factory=lambda: os.getenv(
            "NICHE_FILTER",
            "technology, AI, science, business, finance, or fascinating world news",
        ).strip()
    )
    tts_voice: str = field(
        default_factory=lambda: os.getenv("TTS_VOICE", "en-US-ChristopherNeural").strip()
    )
    tts_rate: str = field(
        default_factory=lambda: os.getenv("TTS_RATE", "+0%").strip()
    )
    min_video_seconds: int = field(
        default_factory=lambda: int(os.getenv("MIN_VIDEO_SECONDS", "125"))
    )
    max_video_seconds: int = field(
        default_factory=lambda: int(os.getenv("MAX_VIDEO_SECONDS", "175"))
    )
    visual_source: str = field(
        default_factory=lambda: os.getenv("VISUAL_SOURCE", "auto").strip().lower()
    )  # "auto" | "pexels" | "pollinations"
    pexels_api_key: str = field(default_factory=lambda: os.getenv("PEXELS_API_KEY", "").strip())

    # Instagram credentials (Official Meta Graph API or instagrapi fallback)
    ig_user_id: str = field(default_factory=lambda: os.getenv("IG_USER_ID", "").strip())
    ig_access_token: str = field(default_factory=lambda: os.getenv("IG_ACCESS_TOKEN", "").strip())
    ig_username: str = field(default_factory=lambda: os.getenv("IG_USERNAME", "").strip())
    ig_password: str = field(default_factory=lambda: os.getenv("IG_PASSWORD", "").strip())
    ig_session_json: str = field(
        default_factory=lambda: (
            os.getenv("IG_SESSION_JSON", "").strip()
            or (Path("ig_session.json").read_text(encoding="utf-8").strip() if Path("ig_session.json").is_file() else "")
        )
    )

    output_dir: Path = field(default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "output")))
    history_file: Path = field(default_factory=lambda: Path(os.getenv("HISTORY_FILE", "topic_history.json")))


@dataclass
class WordTiming:
    word: str
    start_sec: float
    end_sec: float


@dataclass
class SceneSpec:
    narration: str
    overlay_text: str
    search_query: str
    image_prompt: str
    search_queries: List[str] = field(default_factory=list)


@dataclass
class ReelScript:
    trend_topic: str
    title: str
    caption: str
    tags: List[str]
    scenes: List[SceneSpec]


# ---------------------------------------------------------------------------
# Stage 1: Trend Discovery (Google Trends RSS + Google News RSS + Hacker News)
# ---------------------------------------------------------------------------

def load_topic_history(history_file: Path) -> List[str]:
    if history_file.exists():
        try:
            data = json.loads(history_file.read_text(encoding="utf-8"))
            return data.get("topics", [])[-50:]
        except Exception as exc:
            logger.warning("Could not parse history file %s: %s", history_file, exc)
    return []


def save_topic_history(history_file: Path, topic: str) -> None:
    topics = load_topic_history(history_file)
    if topic not in topics:
        topics.append(topic)
    history_file.write_text(
        json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "topics": topics[-50:]}, indent=2),
        encoding="utf-8",
    )


def fetch_trending_topics(geo: str = "IN", max_items: int = 20) -> List[dict]:
    """Fetches real-time trending topics from multiple free public feeds."""
    trends: List[dict] = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DailyReelBot/2.0)"}

    gtrends_urls = [
        f"https://trends.google.com/trending/rss?geo={geo}",
        f"https://trends.google.com/trends/trendingsearches/daily/rss?geo={geo}",
    ]
    for url in gtrends_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code == 200 and "<item>" in resp.text:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:max_items]:
                    title = (item.findtext("title") or "").strip()
                    desc = (item.findtext("description") or "").strip()
                    news_titles = [
                        elem.text.strip()
                        for elem in item.iter()
                        if elem.tag.endswith("news_item_title") and elem.text
                    ]
                    context = " | ".join(news_titles[:2]) if news_titles else desc
                    if title:
                        trends.append({"source": "Google Trends", "topic": title, "context": context})
                if trends:
                    logger.info("Fetched %d topics from Google Trends (%s).", len(trends), geo)
                    break
        except Exception as exc:
            logger.debug("Google Trends RSS attempt failed (%s): %s", url, exc)

    gnews_url = f"https://news.google.com/rss?hl=en-{geo}&gl={geo}&ceid={geo}:en"
    try:
        resp = requests.get(gnews_url, headers=headers, timeout=12)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:10]:
                title = (item.findtext("title") or "").strip()
                if title:
                    trends.append({"source": "Google News", "topic": title, "context": ""})
            logger.info("Added headlines from Google News RSS (%s).", geo)
    except Exception as exc:
        logger.debug("Google News RSS fetch failed: %s", exc)

    try:
        top_ids = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10
        ).json()[:8]
        for item_id in top_ids:
            story = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json", timeout=8
            ).json()
            if story and story.get("title"):
                trends.append(
                    {
                        "source": "Hacker News",
                        "topic": story["title"],
                        "context": f"Score: {story.get('score', 0)}",
                    }
                )
        logger.info("Added top tech stories from Hacker News API.")
    except Exception as exc:
        logger.debug("Hacker News fetch failed: %s", exc)

    if not trends:
        raise RuntimeError("Could not fetch trending topics from any public source.")
    return trends[:max_items]


# ---------------------------------------------------------------------------
# Stage 2: Gemini AI Trend Selector & 2+ Minute Deep-Dive Scriptwriter
# ---------------------------------------------------------------------------

def generate_reel_script(config: BotConfig, trends: List[dict], past_topics: List[str]) -> ReelScript:
    """
    Uses Google Gemini API (google-genai SDK) to pick the best trend and write a 12-to-15 scene
    deep-dive Instagram Reel script totaling 320-380 spoken words (~125-155 seconds / 2+ minutes).
    """
    if not config.gemini_api_key:
        raise ValueError("GEMINI_API_KEY environment variable is required. Get a free key at https://aistudio.google.com/apikey")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.gemini_api_key)

    trends_formatted = "\n".join(
        f"- [{t['source']}] {t['topic']}" + (f" ({t['context']})" if t.get("context") else "")
        for t in trends
    )
    avoid_str = ", ".join(past_topics[-15:]) if past_topics else "None"

    prompt = f"""You are an expert vertical documentary & deep-dive producer for Instagram Reels.

Here are today's live trending topics:
{trends_formatted}

Previously covered topics to AVOID repeating:
{avoid_str}

Preferred niche focus: {config.niche_filter}

Task:
1. Select the single most compelling, Advertiser-Friendly / Brand-Safe trending topic from the list above.
2. Write a comprehensive, high-retention 2-to-2.5-minute (125 to 150 seconds) vertical Instagram Reel script broken into **12 to 15 scenes** (total spoken word count MUST be between **320 and 380 words**, with **25 to 30 spoken words per scene**):
   - Scene 1: Scroll-stopping hook in the first 5 seconds introducing the breaking trend and why everyone is talking about it.
   - Scenes 2-4: Background context—how we got here and the core problem or catalyst.
   - Scenes 5-9: Deep-dive breakdown—key facts, how it works, major players involved, and surprising numbers or details.
   - Scenes 10-12: Real-world impact—what this changes for everyday people, the industry, or the economy.
   - Final Scene (Scene 13-15): Crisp future outlook and an engaging question to drive Instagram comments and shares.
3. Return STRICT JSON matching this exact schema:
{{
  "trend_topic": "Exact name of the chosen trend",
  "title": "Catchy Reel headline under 65 chars",
  "caption": "Engaging Instagram Reel caption with a 2-sentence hook, key takeaways, a question for comments, and 8-10 relevant hashtags (#Reels #Trending #ExplorePage)",
  "tags": ["reels", "trending", "explorepage", "tech", "news"],
  "scenes": [
    {{
      "narration": "2 to 3 full, engaging spoken sentences (MUST be 25-30 words per scene, natural spoken English, no emojis or hashtags).",
      "overlay_text": "3-5 WORD PUNCHY HEADLINE",
      "search_queries": [
        "2-3 word keyword for primary visual photograph (e.g. 'stock exchange trading')",
        "2-3 word keyword for secondary cutaway photo (e.g. 'vault cash money')"
      ],
      "search_query": "2-3 word stock video keyword for Pexels (e.g. 'server room technology')",
      "image_prompt": "Vertical 9:16 cinematic illustration/photo prompt describing the visual scene, clean composition, no text, no watermarks"
    }}
  ]
}}"""

    candidate_models = [config.gemini_model]
    for alt in ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"]:
        if alt not in candidate_models:
            candidate_models.append(alt)

    def _call_gemini_safe(prompt_text: str, temp: float = 0.7) -> Any:
        last_exc: Exception | None = None
        for m in candidate_models:
            for attempt in range(3):
                try:
                    return client.models.generate_content(
                        model=m,
                        contents=prompt_text,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=temp,
                        ),
                    )
                except Exception as exc:
                    last_exc = exc
                    err = str(exc)
                    logger.warning("Gemini attempt %d on %s failed: %s", attempt + 1, m, exc)
                    if "503" in err or "429" in err or "UNAVAILABLE" in err:
                        time.sleep(2 * (attempt + 1))
                        continue
                    break
        raise RuntimeError(f"All Gemini model attempts failed: {last_exc}")

    response = _call_gemini_safe(prompt, temp=0.7)

    raw_text = (response.text or "").strip()
    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.DOTALL)
    payload: dict[str, Any] = json.loads(raw_text)

    total_words = sum(len(s.get("narration", "").split()) for s in payload.get("scenes", []))
    if total_words < 290:
        logger.info("Initial script was %d words (<290); requesting scene expansion from Gemini for 2+ min length...", total_words)
        expand_prompt = f"""The JSON script below currently has {total_words} total spoken words across {len(payload.get('scenes', []))} scenes, which is too short for our 2-minute (125+ second) minimum Instagram Reel length.

Expand this script to **13 to 15 scenes** with **26 to 32 spoken words per scene** (at least **340 spoken words total**) by adding deeper context, concrete examples, and implications, while keeping the exact same JSON schema:

{json.dumps(payload)}"""
        exp_resp = _call_gemini_safe(expand_prompt, temp=0.6)
        exp_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (exp_resp.text or "").strip(), flags=re.DOTALL)
        payload = json.loads(exp_text)

    scenes = [
        SceneSpec(
            narration=s["narration"].strip(),
            overlay_text=s["overlay_text"].strip(),
            search_query=s.get("search_query", "").strip(),
            image_prompt=s.get("image_prompt", "").strip(),
            search_queries=[str(q).strip() for q in s.get("search_queries", []) if str(q).strip()]
            or [s.get("search_query", "").strip()],
        )
        for s in payload["scenes"]
    ]

    final_word_count = sum(len(s.narration.split()) for s in scenes)
    script = ReelScript(
        trend_topic=payload["trend_topic"].strip(),
        title=payload["title"].strip(),
        caption=payload["caption"].strip(),
        tags=[str(t).strip() for t in payload.get("tags", ["reels", "trending", "explorepage"])],
        scenes=scenes,
    )
    logger.info(
        "Selected trend: '%s' | Title: '%s' (%d scenes, %d spoken words)",
        script.trend_topic,
        script.title,
        len(script.scenes),
        final_word_count,
    )
    return script


# ---------------------------------------------------------------------------
# Stage 3: Free Neural Voiceover + Word-Level Timestamp Extraction (edge-tts)
# ---------------------------------------------------------------------------

async def _synthesize_scene_audio_and_words(
    text: str,
    voice: str,
    rate: str,
    out_mp3: Path,
) -> List[WordTiming]:
    """Streams audio and captures exact WordBoundary timestamps from edge-tts."""
    import edge_tts

    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    word_timings: List[WordTiming] = []
    audio_bytes = bytearray()

    async for chunk in communicate.stream():
        ctype = chunk.get("type")
        if ctype == "audio":
            audio_bytes.extend(chunk["data"])
        elif ctype == "WordBoundary":
            start_s = chunk.get("offset", 0) / 10_000_000.0
            dur_s = max(chunk.get("duration", 0) / 10_000_000.0, 0.12)
            w_text = str(chunk.get("text", "")).strip()
            if w_text:
                word_timings.append(WordTiming(word=w_text, start_sec=start_s, end_sec=start_s + dur_s))

    out_mp3.write_bytes(bytes(audio_bytes))
    return word_timings


def get_media_duration(media_path: Path) -> float:
    """Returns exact duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return max(float(out), 1.5)


def build_fallback_word_timings(narration: str, spoken_duration: float) -> List[WordTiming]:
    """Proportional character-weighted word timings if TTS stream omits WordBoundary events."""
    words = [w for w in narration.split() if w.strip()]
    if not words:
        return [WordTiming(word="", start_sec=0.0, end_sec=spoken_duration)]
    weights = [max(len(w), 2) for w in words]
    total_w = sum(weights)
    cursor = 0.0
    timings: List[WordTiming] = []
    for w, weight in zip(words, weights):
        dur = spoken_duration * (weight / total_w)
        timings.append(WordTiming(word=w, start_sec=cursor, end_sec=cursor + dur))
        cursor += dur
    return timings


def generate_scene_voiceovers(
    scenes: List[SceneSpec],
    voice: str,
    rate: str,
    work_dir: Path,
) -> Tuple[List[Path], List[List[WordTiming]]]:
    """Generates MP3 voiceovers and word-level timing arrays for every scene."""
    audio_paths: List[Path] = []
    all_word_timings: List[List[WordTiming]] = []

    for idx, scene in enumerate(scenes):
        out_mp3 = work_dir / f"scene_{idx:02d}_audio.mp3"
        logger.info("Synthesizing TTS voiceover + word timings for scene %d/%d...", idx + 1, len(scenes))
        timings = asyncio.run(_synthesize_scene_audio_and_words(scene.narration, voice, rate, out_mp3))
        spoken_dur = get_media_duration(out_mp3)
        if not timings:
            timings = build_fallback_word_timings(scene.narration, spoken_dur)
        audio_paths.append(out_mp3)
        all_word_timings.append(timings)

    return audio_paths, all_word_timings


def pad_audio_with_silence(in_mp3: Path, target_duration: float, out_mp3: Path) -> Path:
    """Pads an audio file with trailing silence so the scene hits exact target_duration."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(in_mp3),
        "-af",
        f"apad=whole_dur={target_duration:.3f}",
        "-t",
        f"{target_duration:.3f}",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(out_mp3),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return out_mp3


# ---------------------------------------------------------------------------
# Stage 4A: Multi-Provider Visual Pipeline (Pexels, Wikimedia, Pollinations, Picsum)
# ---------------------------------------------------------------------------

def fetch_pexels_vertical_video(query: str, api_key: str, out_path: Path) -> bool:
    """Downloads a portrait 9:16 HD stock video from Pexels API if available."""
    if not api_key:
        return False
    url = "https://api.pexels.com/videos/search"
    params = {"query": query, "orientation": "portrait", "size": "medium", "per_page": 5}
    headers = {"Authorization": api_key}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        videos = resp.json().get("videos", [])
        for video in videos:
            files = video.get("video_files", [])
            portrait_files = [
                f for f in files if f.get("file_type") == "video/mp4" and (f.get("height", 0) > f.get("width", 0))
            ]
            chosen = portrait_files[0] if portrait_files else (files[0] if files else None)
            if chosen and chosen.get("link"):
                vid_resp = requests.get(chosen["link"], stream=True, timeout=30)
                vid_resp.raise_for_status()
                with open(out_path, "wb") as fh:
                    for chunk in vid_resp.iter_content(chunk_size=65536):
                        fh.write(chunk)
                logger.info("Downloaded Pexels portrait video for '%s'", query)
                return True
    except Exception as exc:
        logger.warning("Pexels lookup failed for '%s': %s", query, exc)
    return False


def fetch_pexels_vertical_photo(query: str, api_key: str, out_path: Path) -> bool:
    """Downloads a portrait 9:16 HD stock photo from Pexels API if available."""
    if not api_key:
        return False
    url = "https://api.pexels.com/v1/search"
    params = {"query": query, "orientation": "portrait", "per_page": 5}
    headers = {"Authorization": api_key}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=12)
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if photos:
            src = photos[0].get("src", {})
            img_url = src.get("large2x") or src.get("large") or src.get("original")
            if img_url:
                img_resp = requests.get(img_url, timeout=20)
                if img_resp.status_code == 200 and len(img_resp.content) > 15_000:
                    with Image.open(io.BytesIO(img_resp.content)) as im:
                        im.convert("RGB").save(out_path, "PNG")
                    logger.info("Downloaded Pexels portrait photo for '%s'", query)
                    return True
    except Exception as exc:
        logger.debug("Pexels photo lookup failed for '%s': %s", query, exc)
    return False


def fetch_wikimedia_image(query: str, out_path: Path) -> bool:
    """Downloads a free high-resolution photograph from Wikimedia Commons (zero API key needed)."""
    clean_q = re.sub(r"[^\w\s-]", " ", query).strip()
    if not clean_q:
        return False
    url = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{clean_q} filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": 8,
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": 1280,
        "format": "json",
    }
    headers = {"User-Agent": "DailyReelBot/2.0 (contact@dailyreelbot.local)"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code != 200:
            return False
        pages = resp.json().get("query", {}).get("pages", {})
        for pid, pdata in pages.items():
            ii = pdata.get("imageinfo", [{}])[0]
            thumburl = ii.get("thumburl") or ii.get("url")
            mime = ii.get("mime", "")
            if thumburl and mime in ("image/jpeg", "image/png", "image/webp"):
                img_resp = requests.get(thumburl, headers=headers, timeout=8)
                if img_resp.status_code == 200 and len(img_resp.content) > 15_000:
                    with Image.open(io.BytesIO(img_resp.content)) as im:
                        if im.width >= 350 and im.height >= 350:
                            im.convert("RGB").save(out_path, "PNG")
                            logger.info("Downloaded Wikimedia photo for '%s' (%dx%d)", clean_q, im.width, im.height)
                            return True
    except Exception as exc:
        logger.debug("Wikimedia search failed for '%s': %s", clean_q, exc)
    return False


def fetch_pollinations_vertical_image(prompt: str, seed: int, out_path: Path) -> bool:
    """Downloads a free 1080x1920 AI-generated vertical image from Pollinations.ai (no API key needed)."""
    clean_p = re.sub(r"[^\w\s-]", " ", prompt)[:85].strip()
    encoded = urllib.parse.quote(f"{clean_p}, vertical 9:16 portrait, cinematic lighting, photorealistic, no text")
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1920&seed={seed}&nologo=true"
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200 and len(resp.content) > 10_000:
            with Image.open(io.BytesIO(resp.content)) as im:
                im.convert("RGB").save(out_path, "PNG")
            logger.info("Generated 1080x1920 AI image via Pollinations.ai (seed=%d)", seed)
            return True
    except Exception as exc:
        logger.debug("Pollinations.ai image generation failed: %s", exc)
    return False


def fetch_picsum_vertical_image(seed: int, out_path: Path) -> bool:
    """Downloads a guaranteed high-res 1080x1920 aesthetic photo from Picsum (zero key needed, 100% reliable)."""
    url = f"https://picsum.photos/1080/1920?random={seed}"
    try:
        resp = requests.get(url, timeout=12)
        if resp.status_code == 200 and len(resp.content) > 15_000:
            with Image.open(io.BytesIO(resp.content)) as im:
                im.convert("RGB").save(out_path, "PNG")
            logger.info("Fetched Picsum 1080x1920 aesthetic photography (seed=%d)", seed)
            return True
    except Exception as exc:
        logger.debug("Picsum fetch failed: %s", exc)
    return False


def acquire_shot_visual(
    query: str,
    prompt: str,
    seed: int,
    config: BotConfig,
    out_img: Path,
    out_vid: Path,
    scene_idx: int,
    shot_idx: int,
) -> Tuple[Path, bool]:
    """
    Acquires a real vertical photograph or video clip using the multi-provider priority chain:
    1. Pexels Stock Video (if PEXELS_API_KEY provided)
    2. Pexels HD Portrait Photo (if PEXELS_API_KEY provided)
    3. Wikimedia Commons real topical photograph (free, keyless)
    4. Pollinations.ai AI vertical art (free, keyless)
    5. Picsum HD 1080x1920 aesthetic photography (free, 100% reliable fallback)
    6. Pillow procedural cyber-grid (offline safety net)
    """
    if config.visual_source in ("auto", "pexels") and config.pexels_api_key:
        if fetch_pexels_vertical_video(query, config.pexels_api_key, out_vid):
            return out_vid, True
        if fetch_pexels_vertical_photo(query, config.pexels_api_key, out_img):
            return out_img, False

    if config.visual_source in ("auto", "wikimedia", "photo"):
        if fetch_wikimedia_image(query, out_img):
            return out_img, False

    if config.visual_source in ("auto", "pollinations"):
        if fetch_pollinations_vertical_image(prompt or query, seed, out_img):
            return out_img, False

    # Guaranteed aesthetic real photography fallback
    if fetch_picsum_vertical_image(seed, out_img):
        return out_img, False

    # Offline safety net
    create_fallback_background(out_img, scene_idx + shot_idx)
    return out_img, False


def create_fallback_background(out_path: Path, scene_idx: int) -> None:
    """Generates a rich cyber-grid & gradient 1080x1920 background using Pillow if offline."""
    palettes = [
        ((10, 15, 35), (30, 64, 175), (56, 189, 248)),
        ((17, 12, 38), (107, 33, 168), (244, 114, 182)),
        ((8, 28, 36), (15, 118, 110), (45, 212, 191)),
        ((28, 14, 24), (153, 27, 27), (251, 146, 60)),
        ((14, 18, 42), (67, 56, 202), (167, 139, 250)),
    ]
    top_rgb, bot_rgb, glow_rgb = palettes[scene_idx % len(palettes)]
    width, height = 1080, 1920
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / height
        r = int(top_rgb[0] * (1 - ratio) + bot_rgb[0] * ratio)
        g = int(top_rgb[1] * (1 - ratio) + bot_rgb[1] * ratio)
        b = int(top_rgb[2] * (1 - ratio) + bot_rgb[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    for gx in range(0, width, 90):
        draw.line([(gx, 0), (gx, height)], fill=(glow_rgb[0], glow_rgb[1], glow_rgb[2]), width=1)
    for gy in range(0, height, 90):
        draw.line([(0, gy), (width, gy)], fill=(glow_rgb[0], glow_rgb[1], glow_rgb[2]), width=1)

    img.save(out_path, "PNG")


# ---------------------------------------------------------------------------
# Stage 4B: Kinetic Word-by-Word Caption, Waveform & HUD Overlay Engine
# ---------------------------------------------------------------------------

def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_candidates = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    for path in font_candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


@dataclass
class CaptionEvent:
    phrase_words: List[str]
    active_index: int
    start_sec: float
    duration_sec: float
    step_seed: int


def build_word_by_word_events(
    word_timings: List[WordTiming],
    target_scene_duration: float,
    words_per_phrase: int = 4,
) -> List[CaptionEvent]:
    """
    Groups words into 4-word phrases and emits one CaptionEvent per spoken word
    so the active word highlight & waveform animate continuously every ~0.25s.
    """
    if not word_timings:
        return [
            CaptionEvent(
                phrase_words=[""],
                active_index=0,
                start_sec=0.0,
                duration_sec=target_scene_duration,
                step_seed=0,
            )
        ]

    events: List[CaptionEvent] = []
    n_words = len(word_timings)

    for idx, wt in enumerate(word_timings):
        group_start = (idx // words_per_phrase) * words_per_phrase
        group_slice = word_timings[group_start : group_start + words_per_phrase]
        phrase_words = [w.word for w in group_slice]
        active_idx = idx - group_start

        start_s = 0.0 if idx == 0 else wt.start_sec
        if idx + 1 < n_words:
            end_s = max(word_timings[idx + 1].start_sec, start_s + 0.08)
        else:
            end_s = max(target_scene_duration, start_s + 0.15)

        dur_s = max(end_s - start_s, 0.08)
        events.append(
            CaptionEvent(
                phrase_words=phrase_words,
                active_index=active_idx,
                start_sec=start_s,
                duration_sec=dur_s,
                step_seed=idx,
            )
        )

    total_ev = sum(e.duration_sec for e in events)
    if total_ev > 0:
        scale = target_scene_duration / total_ev
        for e in events:
            e.duration_sec = max(e.duration_sec * scale, 0.06)

    return events


def render_kinetic_overlay_png(
    headline: str,
    event: CaptionEvent,
    trend_badge: str,
    scene_idx: int,
    total_scenes: int,
    global_progress: float,
    is_scene_intro: bool,
    out_png: Path,
) -> None:
    """Renders a 1080x1920 RGBA overlay frame with karaoke word pills, waveform bars, HUD, and progress bar."""
    width, height = 1080, 1920
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Subtle top gradient to ensure headline readability without obscuring visuals
    for y in range(0, 320):
        alpha = int(120 * (1 - y / 320))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
    for y in range(height - 720, height):
        alpha = int(210 * ((y - (height - 720)) / 720))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

    font_headline = _load_font(50)
    font_word_idle = _load_font(54)
    font_word_active = _load_font(62)

    # --- Animated Headline Banner (Top HUD Trend Pill & Scene Counter removed per user request) ---
    slide_offset_y = -18 if is_scene_intro else 0
    wrapped_head = textwrap.wrap(headline.upper(), width=20)[:2]
    hy = 185 + slide_offset_y

    card_h = len(wrapped_head) * 68 + 32
    draw.rounded_rectangle(
        [65, hy - 18, width - 65, hy + card_h - 18],
        radius=22,
        fill=(15, 23, 42, 190),
        outline=(250, 204, 21, 210),
        width=3,
    )
    for line in wrapped_head:
        lb = draw.textbbox((0, 0), line, font=font_headline)
        lw, lh = lb[2] - lb[0], lb[3] - lb[1]
        lx = (width - lw) // 2
        draw.text((lx + 3, hy + 3), line, font=font_headline, fill=(0, 0, 0, 230))
        draw.text((lx, hy), line, font=font_headline, fill=(250, 204, 21, 255))
        hy += lh + 18

    # --- 3. Dynamic Audio Equalizer Bars ---
    num_bars = 21
    bar_w = 12
    bar_gap = 10
    total_eq_w = num_bars * bar_w + (num_bars - 1) * bar_gap
    eq_start_x = (width - total_eq_w) // 2
    eq_base_y = height - 540

    for b_idx in range(num_bars):
        wave = abs(math.sin((b_idx * 0.65) + (event.step_seed * 1.35)))
        center_boost = 1.0 - abs(b_idx - num_bars // 2) / (num_bars / 1.5)
        bar_h = int(14 + 54 * wave * max(center_boost, 0.35))
        bx0 = eq_start_x + b_idx * (bar_w + bar_gap)
        by0 = eq_base_y - bar_h // 2
        by1 = eq_base_y + bar_h // 2
        bar_color = (56, 189, 248, 235) if (b_idx % 2 == 0) else (250, 204, 21, 235)
        draw.rounded_rectangle([bx0, by0, bx0 + bar_w, by1], radius=6, fill=bar_color)

    # --- 4. Word-by-Word Karaoke Captions (Active Word Pop-Scale Pill) ---
    words_upper = [w.upper() for w in event.phrase_words if w]
    if not words_upper:
        words_upper = [""]

    lines_indices: List[List[int]] = []
    if len(words_upper) <= 2:
        lines_indices.append(list(range(len(words_upper))))
    else:
        mid = (len(words_upper) + 1) // 2
        lines_indices.append(list(range(0, mid)))
        lines_indices.append(list(range(mid, len(words_upper))))

    cap_y = height - 455
    for line_word_idxs in lines_indices:
        word_boxes = []
        total_line_w = 0
        for w_i in line_word_idxs:
            w_str = words_upper[w_i]
            is_active = w_i == event.active_index
            fnt = font_word_active if is_active else font_word_idle
            wb = draw.textbbox((0, 0), w_str, font=fnt)
            ww, wh = wb[2] - wb[0], wb[3] - wb[1]
            word_boxes.append((w_i, w_str, is_active, fnt, ww, wh))
            total_line_w += ww
        total_line_w += max(len(word_boxes) - 1, 0) * 28

        cursor_x = (width - total_line_w) // 2
        for w_i, w_str, is_active, fnt, ww, wh in word_boxes:
            if is_active and w_str:
                pill_color = (250, 204, 21, 255) if (scene_idx % 2 == 0) else (56, 189, 248, 255)
                text_color = (10, 15, 30, 255)
                draw.rounded_rectangle(
                    [cursor_x - 16, cap_y - 10, cursor_x + ww + 16, cap_y + wh + 16],
                    radius=16,
                    fill=pill_color,
                    outline=(255, 255, 255, 255),
                    width=3,
                )
                draw.text((cursor_x, cap_y - 2), w_str, font=fnt, fill=text_color)
            else:
                for dx, dy in [(-3, -3), (3, -3), (-3, 3), (3, 3), (0, 4)]:
                    draw.text((cursor_x + dx, cap_y + dy), w_str, font=fnt, fill=(0, 0, 0, 240))
                draw.text((cursor_x, cap_y), w_str, font=fnt, fill=(255, 255, 255, 255))
            cursor_x += ww + 28
        cap_y += 92

    # --- 5. Continuous Bottom Timeline Progress Bar ---
    bar_margin = 70
    bar_y = height - 135
    bar_total_w = width - 2 * bar_margin
    draw.rounded_rectangle(
        [bar_margin, bar_y, bar_margin + bar_total_w, bar_y + 14],
        radius=7,
        fill=(255, 255, 255, 65),
    )
    fill_w = max(int(bar_total_w * min(max(global_progress, 0.02), 1.0)), 16)
    draw.rounded_rectangle(
        [bar_margin, bar_y, bar_margin + fill_w, bar_y + 14],
        radius=7,
        fill=(250, 204, 21, 255),
    )

    overlay.save(out_png, "PNG")


# ---------------------------------------------------------------------------
# Stage 4C: 30fps Ken Burns Camera Motion + Scene Transition Compositor
# ---------------------------------------------------------------------------

def get_camera_motion_filter(scene_idx: int, duration: float, is_video: bool) -> str:
    """
    Returns an FFmpeg video filter chain that applies continuous 30fps Ken Burns
    pan/zoom motion, color-contrast punch, and smooth scene fade-in/out transitions.
    """
    fade_out_st = max(duration - 0.16, 0.1)
    dur_safe = max(duration, 1.0)
    if is_video:
        return (
            f"scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,"
            f"fps=30,"
            f"eq=contrast=1.06:saturation=1.12,"
            f"fade=t=in:st=0:d=0.15,"
            f"fade=t=out:st={fade_out_st:.2f}:d=0.15,"
            f"format=yuv420p"
        )

    mode = scene_idx % 4
    if mode == 0:
        x_expr = f"(iw-1080)*min(t/{dur_safe:.2f},1.0)"
        y_expr = f"(ih-1920)*min(t/{dur_safe:.2f},1.0)"
    elif mode == 1:
        x_expr = f"(iw-1080)*(1.0-min(t/{dur_safe:.2f},1.0))"
        y_expr = f"(ih-1920)*(0.8-0.6*min(t/{dur_safe:.2f},1.0))"
    elif mode == 2:
        x_expr = f"(iw-1080)*(0.5+0.45*sin(t*1.2))"
        y_expr = f"(ih-1920)*min(t/{dur_safe:.2f},1.0)"
    else:
        x_expr = f"(iw-1080)/2"
        y_expr = f"(ih-1920)*(1.0-min(t/{dur_safe:.2f},1.0))"

    return (
        f"scale=1350:2400:force_original_aspect_ratio=increase,"
        f"crop=1350:2400,"
        f"fps=30,"
        f"crop=1080:1920:x='{x_expr}':y='{y_expr}',"
        f"eq=contrast=1.06:saturation=1.12,"
        f"fade=t=in:st=0:d=0.15,"
        f"fade=t=out:st={fade_out_st:.2f}:d=0.15,"
        f"format=yuv420p"
    )


def render_scene_clip(
    scene_idx: int,
    total_scenes: int,
    scene: SceneSpec,
    trend_topic: str,
    audio_path: Path,
    word_timings: List[WordTiming],
    target_scene_duration: float,
    elapsed_before_scene: float,
    total_video_duration: float,
    config: BotConfig,
    work_dir: Path,
) -> Path:
    """Renders a single scene into a 1080x1920 30fps H.264 MP4 with multi-picture dynamic camera cuts and karaoke captions."""
    padded_audio = work_dir / f"scene_{scene_idx:02d}_padded.mp3"
    pad_audio_with_silence(audio_path, target_scene_duration, padded_audio)

    # Multi-picture shot division: 2 to 3 dynamic visual cuts per scene (every 3.5 - 5.5 seconds)
    num_shots = 3 if target_scene_duration >= 13.0 else 2
    dur_base = round(target_scene_duration / num_shots, 3)
    shot_durations = [dur_base] * num_shots
    shot_durations[-1] = round(target_scene_duration - sum(shot_durations[:-1]), 3)

    shot_mp4s: List[Path] = []
    queries = scene.search_queries if scene.search_queries else [scene.search_query]

    for shot_idx in range(num_shots):
        if shot_idx < len(queries) and queries[shot_idx].strip():
            q = queries[shot_idx].strip()
        elif shot_idx == 0:
            q = scene.search_query or scene.overlay_text
        else:
            q = f"{scene.search_query} {scene.overlay_text}".strip()

        shot_dur = shot_durations[shot_idx]
        shot_img_path = work_dir / f"scene_{scene_idx:02d}_shot_{shot_idx:02d}.png"
        shot_vid_path = work_dir / f"scene_{scene_idx:02d}_shot_{shot_idx:02d}_raw.mp4"
        shot_mp4 = work_dir / f"scene_{scene_idx:02d}_shot_{shot_idx:02d}.mp4"

        shot_asset, is_video = acquire_shot_visual(
            query=q,
            prompt=scene.image_prompt,
            seed=int(time.time()) + scene_idx * 97 + shot_idx * 23,
            config=config,
            out_img=shot_img_path,
            out_vid=shot_vid_path,
            scene_idx=scene_idx,
            shot_idx=shot_idx,
        )

        cam_filter = get_camera_motion_filter(scene_idx * 3 + shot_idx, shot_dur, is_video=is_video)
        if is_video:
            bg_input_args = ["-stream_loop", "-1", "-t", f"{shot_dur:.3f}", "-i", str(shot_asset)]
        else:
            bg_input_args = ["-loop", "1", "-framerate", "30", "-t", f"{shot_dur:.3f}", "-i", str(shot_asset)]

        cmd_shot = [
            "ffmpeg",
            "-y",
            *bg_input_args,
            "-vf",
            cam_filter,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "22",
            "-t",
            f"{shot_dur:.3f}",
            str(shot_mp4),
        ]
        subprocess.run(cmd_shot, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        shot_mp4s.append(shot_mp4)

    shot_concat_file = work_dir / f"scene_{scene_idx:02d}_shots_list.txt"
    shot_concat_file.write_text(
        "\n".join(f"file '{s.resolve().as_posix()}'" for s in shot_mp4s), encoding="utf-8"
    )
    scene_bg_combined = work_dir / f"scene_{scene_idx:02d}_bg_combined.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(shot_concat_file), "-c", "copy", str(scene_bg_combined)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    events = build_word_by_word_events(word_timings, target_scene_duration, words_per_phrase=4)
    ov_concat_lines: List[str] = []
    elapsed_in_scene = 0.0

    for ev_idx, ev in enumerate(events):
        ov_png = work_dir / f"scene_{scene_idx:02d}_ov_{ev_idx:03d}.png"
        global_prog = (elapsed_before_scene + elapsed_in_scene) / max(total_video_duration, 1.0)
        render_kinetic_overlay_png(
            headline=scene.overlay_text,
            event=ev,
            trend_badge=trend_topic,
            scene_idx=scene_idx,
            total_scenes=total_scenes,
            global_progress=global_prog,
            is_scene_intro=(ev_idx == 0),
            out_png=ov_png,
        )
        ov_concat_lines.append(f"file '{ov_png.resolve().as_posix()}'")
        ov_concat_lines.append(f"duration {ev.duration_sec:.4f}")
        elapsed_in_scene += ev.duration_sec

    last_png = work_dir / f"scene_{scene_idx:02d}_ov_{len(events) - 1:03d}.png"
    ov_concat_lines.append(f"file '{last_png.resolve().as_posix()}'")

    ov_concat_file = work_dir / f"scene_{scene_idx:02d}_ov_list.txt"
    ov_concat_file.write_text("\n".join(ov_concat_lines), encoding="utf-8")

    scene_final = work_dir / f"scene_{scene_idx:02d}_final.mp4"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(scene_bg_combined),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(ov_concat_file),
        "-i",
        str(padded_audio),
        "-filter_complex",
        "[0:v]fps=30[bg];[1:v]fps=30,format=rgba[ov];[bg][ov]overlay=0:0:shortest=1,format=yuv420p[v]",
        "-map",
        "[v]",
        "-map",
        "2:a",
        "-t",
        f"{target_scene_duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(scene_final),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return scene_final


def assemble_full_reel(
    script: ReelScript,
    audio_paths: List[Path],
    all_word_timings: List[List[WordTiming]],
    config: BotConfig,
    work_dir: Path,
) -> Path:
    """
    Renders all scenes with kinetic animations and guarantees the final 1080x1920 MP4
    is at least `config.min_video_seconds` (125s / >2 minutes) and under `config.max_video_seconds` (175s).
    """
    raw_durations = [get_media_duration(p) + 0.45 for p in audio_paths]
    total_raw = sum(raw_durations)

    extra_per_scene = 0.0
    if total_raw < config.min_video_seconds and len(raw_durations) > 0:
        extra_per_scene = (config.min_video_seconds - total_raw) / len(raw_durations)
        logger.info(
            "Raw spoken duration is %.1fs; adding %.2fs pacing buffer per scene to hit >=%ds target.",
            total_raw,
            extra_per_scene,
            config.min_video_seconds,
        )

    target_durations = [d + extra_per_scene for d in raw_durations]
    total_planned = sum(target_durations)

    scene_clips: List[Path] = []
    elapsed_so_far = 0.0
    total_scenes = len(script.scenes)

    for idx, (scene, audio_path, w_timings, target_dur) in enumerate(
        zip(script.scenes, audio_paths, all_word_timings, target_durations)
    ):
        logger.info(
            "Rendering animated scene %d/%d ('%s', %d words, %.1fs)...",
            idx + 1,
            total_scenes,
            scene.overlay_text,
            len(w_timings),
            target_dur,
        )
        clip = render_scene_clip(
            scene_idx=idx,
            total_scenes=total_scenes,
            scene=scene,
            trend_topic=script.trend_topic,
            audio_path=audio_path,
            word_timings=w_timings,
            target_scene_duration=target_dur,
            elapsed_before_scene=elapsed_so_far,
            total_video_duration=total_planned,
            config=config,
            work_dir=work_dir,
        )
        scene_clips.append(clip)
        elapsed_so_far += target_dur

    concat_file = work_dir / "all_scenes.txt"
    concat_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in scene_clips),
        encoding="utf-8",
    )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    final_mp4 = config.output_dir / f"reel_{timestamp}.mp4"

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-t",
        str(config.max_video_seconds),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(final_mp4),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    logger.info("Final animated Instagram Reel saved to %s (%.1fs)", final_mp4, get_media_duration(final_mp4))
    return final_mp4


# ---------------------------------------------------------------------------
# Stage 5: Instagram Reels Automated Uploader (Meta Graph API + instagrapi)
# ---------------------------------------------------------------------------

def upload_to_instagram_graph_api(video_path: Path, script: ReelScript, config: BotConfig) -> Optional[str]:
    """
    Uploads to Instagram Reels using Meta's official Graph API Resumable Upload protocol.
    Supports Reels up to 15 minutes in length without needing an external S3 bucket.
    """
    api_version = "v21.0"
    init_url = f"https://graph.facebook.com/{api_version}/{config.ig_user_id}/media"
    init_payload = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": script.caption,
        "share_to_feed": "true",
        "access_token": config.ig_access_token,
    }
    logger.info("Initializing Instagram Graph API resumable Reel container...")
    init_resp = requests.post(init_url, data=init_payload, timeout=20)
    init_resp.raise_for_status()
    init_data = init_resp.json()
    container_id = init_data["id"]
    upload_uri = init_data.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{api_version}/{container_id}"

    file_size = video_path.stat().st_size
    with open(video_path, "rb") as fh:
        upload_headers = {
            "Authorization": f"OAuth {config.ig_access_token}",
            "offset": "0",
            "file_size": str(file_size),
            "Content-Type": "application/octet-stream",
        }
        logger.info("Uploading %d bytes to Meta rupload endpoint...", file_size)
        up_resp = requests.post(upload_uri, headers=upload_headers, data=fh, timeout=180)
        up_resp.raise_for_status()

    status_url = f"https://graph.facebook.com/{api_version}/{container_id}"
    for attempt in range(30):
        time.sleep(10)
        st_resp = requests.get(
            status_url,
            params={"fields": "status_code,status", "access_token": config.ig_access_token},
            timeout=15,
        ).json()
        code = st_resp.get("status_code")
        logger.info("Instagram container processing status (%d/30): %s", attempt + 1, code)
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram media processing failed: {st_resp}")
    else:
        raise TimeoutError("Instagram media container did not finish processing in time.")

    pub_url = f"https://graph.facebook.com/{api_version}/{config.ig_user_id}/media_publish"
    pub_resp = requests.post(
        pub_url,
        data={"creation_id": container_id, "access_token": config.ig_access_token},
        timeout=20,
    )
    pub_resp.raise_for_status()
    media_id = pub_resp.json().get("id")
    logger.info("Published to Instagram Reels via Graph API (media_id=%s)", media_id)
    return str(media_id)


def upload_to_instagram_instagrapi(video_path: Path, script: ReelScript, config: BotConfig) -> Optional[str]:
    """Fallback uploader using instagrapi for personal/creator accounts without a Meta App."""
    from instagrapi import Client

    cl = Client()
    if config.ig_session_json:
        cl.set_settings(json.loads(config.ig_session_json))
    cl.login(config.ig_username, config.ig_password)
    thumb_path = video_path.with_suffix(".jpg")
    if not thumb_path.exists():
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "00:00:02", "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(thumb_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        except Exception:
            pass

    logger.info("Uploading Reel via instagrapi...")
    media = cl.clip_upload(str(video_path), caption=script.caption, thumbnail=thumb_path if thumb_path.exists() else None)
    logger.info("Published to Instagram Reels via instagrapi (pk=%s)", media.pk)
    return str(media.pk)


def interactive_ig_session_setup() -> None:
    """One-time local helper to log into Instagram via instagrapi and export IG_SESSION_JSON for GitHub Secrets."""
    from instagrapi import Client

    config = BotConfig()
    username = config.ig_username or input("Enter your Instagram username: ").strip()
    password = config.ig_password or input("Enter your Instagram password: ").strip()
    cl = Client()
    cl.login(username, password)
    session_str = json.dumps(cl.get_settings())
    print("\n=== Add this secret to your .env and GitHub Repository Secrets ===")
    print(f"IG_SESSION_JSON='{session_str}'\n")


def upload_to_instagram_reels(video_path: Path, script: ReelScript, config: BotConfig) -> Optional[str]:
    if config.ig_user_id and config.ig_access_token:
        return upload_to_instagram_graph_api(video_path, script, config)
    if config.ig_username and config.ig_password:
        return upload_to_instagram_instagrapi(video_path, script, config)
    logger.warning("Instagram credentials not configured; skipping Instagram Reels upload.")
    return None


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(dry_run: bool = False) -> Path:
    config = BotConfig()
    past_topics = load_topic_history(config.history_file)

    logger.info("Step 1/5: Fetching live trending topics (GEO=%s)...", config.trend_geo)
    trends = fetch_trending_topics(geo=config.trend_geo)

    logger.info("Step 2/5: Generating 2+ minute Instagram Reel script with Gemini (%s)...", config.gemini_model)
    script = generate_reel_script(config, trends, past_topics)

    with tempfile.TemporaryDirectory(prefix="reel_bot_") as tmp_str:
        work_dir = Path(tmp_str)

        logger.info("Step 3/5: Synthesizing neural voiceover & word timings via edge-tts (%s)...", config.tts_voice)
        audio_paths, all_word_timings = generate_scene_voiceovers(
            script.scenes, config.tts_voice, config.tts_rate, work_dir
        )

        logger.info("Step 4/5: Rendering kinetic 1080x1920 vertical Reel (>=%ds)...", config.min_video_seconds)
        final_video = assemble_full_reel(script, audio_paths, all_word_timings, config, work_dir)

    meta_path = final_video.with_suffix(".json")
    meta_path.write_text(
        json.dumps(
            {
                "trend_topic": script.trend_topic,
                "title": script.title,
                "caption": script.caption,
                "tags": script.tags,
                "video_file": str(final_video),
                "duration_seconds": round(get_media_duration(final_video), 2),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if dry_run:
        logger.info("Dry-run enabled: skipping Instagram Reels upload.")
        return final_video

    logger.info("Step 5/5: Publishing to Instagram Reels...")
    ig_id = upload_to_instagram_reels(final_video, script, config)

    save_topic_history(config.history_file, script.trend_topic)
    logger.info("Pipeline complete! Instagram Reel ID=%s", ig_id)
    return final_video


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Daily Trend-to-Instagram Reels Bot (Kinetic Edition)")
    parser.add_argument("--dry-run", action="store_true", help="Generate video locally without uploading")
    parser.add_argument(
        "--setup-ig-session",
        action="store_true",
        help="Log in once locally via instagrapi to export IG_SESSION_JSON for GitHub Actions",
    )
    args = parser.parse_args()

    if args.setup_ig_session:
        interactive_ig_session_setup()
        return

    run_pipeline(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
