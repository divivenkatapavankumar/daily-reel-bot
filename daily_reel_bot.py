#!/usr/bin/env python3
"""
Autonomous Daily Trend-to-Reel & YouTube Shorts Bot (2+ Minute Deep-Dive Edition)
=================================================================================
End-to-end pipeline designed for daily execution via GitHub Actions (or cron):
1. Trend Discovery: Pulls daily trending topics from Google Trends RSS, Google News RSS, and Hacker News API.
2. AI Scriptwriter: Uses Google Gemini (`google-genai` free tier) to select the top trend and write a
   12-to-15-scene deep-dive vertical video script (320-380 spoken words, targeting 125-155 seconds / 2+ minutes,
   within the 3-minute limit for YouTube Shorts and Instagram Reels).
3. Neural Voiceover: Generates crisp spoken audio per scene using `edge-tts` (100% free, no API key required).
4. Visuals & 9:16 Video Renderer: Fetches vertical stock clips from Pexels API (free) or generates 1080x1920
   AI visuals via Pollinations.ai (free, keyless), renders dynamic captions with Pillow, and encodes a
   1080x1920 H.264 MP4 via FFmpeg guaranteed to be at least `MIN_VIDEO_SECONDS` (default 125s).
5. Dual Uploader: Publishes automatically to YouTube Shorts (YouTube Data API v3 with OAuth Refresh Token)
   and Instagram Reels (Official Meta Graph API Resumable Upload or `instagrapi` session fallback).

Usage:
  python daily_reel_bot.py                     # Run full pipeline (generate 2+ min video + upload)
  python daily_reel_bot.py --dry-run           # Generate 2+ min video locally without uploading
  python daily_reel_bot.py --setup-youtube     # Run once locally to obtain YOUTUBE_REFRESH_TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

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

    # YouTube Data API v3 credentials
    yt_client_id: str = field(default_factory=lambda: os.getenv("YOUTUBE_CLIENT_ID", "").strip())
    yt_client_secret: str = field(default_factory=lambda: os.getenv("YOUTUBE_CLIENT_SECRET", "").strip())
    yt_refresh_token: str = field(default_factory=lambda: os.getenv("YOUTUBE_REFRESH_TOKEN", "").strip())
    yt_privacy_status: str = field(default_factory=lambda: os.getenv("YOUTUBE_PRIVACY", "public").strip())

    # Instagram credentials (Official Graph API or instagrapi fallback)
    ig_user_id: str = field(default_factory=lambda: os.getenv("IG_USER_ID", "").strip())
    ig_access_token: str = field(default_factory=lambda: os.getenv("IG_ACCESS_TOKEN", "").strip())
    ig_username: str = field(default_factory=lambda: os.getenv("IG_USERNAME", "").strip())
    ig_password: str = field(default_factory=lambda: os.getenv("IG_PASSWORD", "").strip())
    ig_session_json: str = field(default_factory=lambda: os.getenv("IG_SESSION_JSON", "").strip())

    output_dir: Path = field(default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "output")))
    history_file: Path = field(default_factory=lambda: Path(os.getenv("HISTORY_FILE", "topic_history.json")))


@dataclass
class SceneSpec:
    narration: str
    overlay_text: str
    search_query: str
    image_prompt: str


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
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DailyReelBot/1.0)"}

    # 1. Google Trends Daily RSS Feed
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

    # 2. Google News Top/Tech Headlines RSS
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

    # 3. Hacker News Top Stories (Free Firebase API)
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
    deep-dive script totaling 320-380 spoken words (~125-155 seconds / 2+ minutes).
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

    prompt = f"""You are an expert vertical documentary & deep-dive producer for Instagram Reels and YouTube Shorts.

Here are today's live trending topics:
{trends_formatted}

Previously covered topics to AVOID repeating:
{avoid_str}

Preferred niche focus: {config.niche_filter}

Task:
1. Select the single most compelling, Advertiser-Friendly / Brand-Safe trending topic from the list above.
2. Write a comprehensive, high-retention 2-to-2.5-minute (125 to 150 seconds) vertical video script broken into **12 to 15 scenes** (total spoken word count MUST be between **320 and 380 words**, with **25 to 30 spoken words per scene**):
   - Scene 1: Scroll-stopping hook in the first 5 seconds introducing the breaking trend and why everyone is talking about it.
   - Scenes 2-4: Background context—how we got here and the core problem or catalyst.
   - Scenes 5-9: Deep-dive breakdown—key facts, how it works, major players involved, and surprising numbers or details.
   - Scenes 10-12: Real-world impact—what this changes for everyday people, the industry, or the economy.
   - Final Scene (Scene 13-15): Crisp future outlook and an engaging question to drive viewer comments.
3. Return STRICT JSON matching this exact schema:
{{
  "trend_topic": "Exact name of the chosen trend",
  "title": "Catchy YouTube Shorts title under 65 chars ending with #Shorts",
  "caption": "Engaging Instagram Reel & YouTube description with a 2-sentence summary and 6-8 relevant hashtags",
  "tags": ["tag1", "tag2", "tag3", "shorts", "trending"],
  "scenes": [
    {{
      "narration": "2 to 3 full, engaging spoken sentences (MUST be 25-30 words per scene, natural spoken English, no emojis or hashtags).",
      "overlay_text": "3-5 WORD PUNCHY HEADLINE",
      "search_query": "2-3 word stock video keyword for Pexels (e.g. 'server room technology')",
      "image_prompt": "Vertical 9:16 cinematic illustration/photo prompt describing the visual scene, clean composition, no text, no watermarks"
    }}
  ]
}}"""

    response = client.models.generate_content(
        model=config.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.7,
        ),
    )

    raw_text = (response.text or "").strip()
    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.DOTALL)
    payload: dict[str, Any] = json.loads(raw_text)

    # Verify total spoken word count is sufficient for a 2+ minute video (~300+ words)
    total_words = sum(len(s.get("narration", "").split()) for s in payload.get("scenes", []))
    if total_words < 290:
        logger.info("Initial script was %d words (<290); requesting scene expansion from Gemini for 2+ min length...", total_words)
        expand_prompt = f"""The JSON script below currently has {total_words} total spoken words across {len(payload.get('scenes', []))} scenes, which is too short for our 2-minute (125+ second) minimum video length.

Expand this script to **13 to 15 scenes** with **26 to 32 spoken words per scene** (at least **340 spoken words total**) by adding deeper context, concrete examples, and implications, while keeping the exact same JSON schema:

{json.dumps(payload)}"""
        exp_resp = client.models.generate_content(
            model=config.gemini_model,
            contents=expand_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.6,
            ),
        )
        exp_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (exp_resp.text or "").strip(), flags=re.DOTALL)
        payload = json.loads(exp_text)

    scenes = [
        SceneSpec(
            narration=s["narration"].strip(),
            overlay_text=s["overlay_text"].strip(),
            search_query=s["search_query"].strip(),
            image_prompt=s["image_prompt"].strip(),
        )
        for s in payload["scenes"]
    ]

    title = payload["title"].strip()
    if "#shorts" not in title.lower():
        title = f"{title[:58].strip()} #Shorts"

    final_word_count = sum(len(s.narration.split()) for s in scenes)
    script = ReelScript(
        trend_topic=payload["trend_topic"].strip(),
        title=title,
        caption=payload["caption"].strip(),
        tags=[str(t).strip() for t in payload.get("tags", ["shorts", "trending", "reels"])],
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
# Stage 3: Free Neural Voiceover Generation (edge-tts)
# ---------------------------------------------------------------------------

async def _synthesize_scene_audio(text: str, voice: str, rate: str, out_mp3: Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(str(out_mp3))


def generate_scene_voiceovers(scenes: List[SceneSpec], voice: str, rate: str, work_dir: Path) -> List[Path]:
    """Generates an MP3 voiceover for each scene using Microsoft Edge's free neural TTS."""
    audio_paths: List[Path] = []
    for idx, scene in enumerate(scenes):
        out_mp3 = work_dir / f"scene_{idx:02d}_audio.mp3"
        logger.info("Synthesizing TTS voiceover for scene %d/%d...", idx + 1, len(scenes))
        asyncio.run(_synthesize_scene_audio(scene.narration, voice, rate, out_mp3))
        audio_paths.append(out_mp3)
    return audio_paths


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
# Stage 4: Free Visual Fetching (Pexels Stock Video + Pollinations AI + Fallback)
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


def fetch_pollinations_vertical_image(prompt: str, seed: int, out_path: Path) -> bool:
    """Downloads a free 1080x1920 AI-generated vertical image from Pollinations.ai (no API key needed)."""
    encoded = urllib.parse.quote(f"{prompt}, vertical 9:16 aspect ratio, cinematic lighting, high detail, no text")
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1920&seed={seed}&nologo=true"
    try:
        resp = requests.get(url, timeout=35)
        if resp.status_code == 200 and len(resp.content) > 10_000:
            out_path.write_bytes(resp.content)
            logger.info("Generated 1080x1920 AI image via Pollinations.ai (seed=%d)", seed)
            return True
    except Exception as exc:
        logger.warning("Pollinations.ai image generation failed: %s", exc)
    return False


def create_fallback_background(out_path: Path, scene_idx: int) -> None:
    """Generates a sleek dark gradient 1080x1920 background using Pillow if network visual calls time out."""
    palettes = [
        ((15, 23, 42), (30, 58, 138)),
        ((17, 24, 39), (88, 28, 135)),
        ((9, 25, 45), (13, 148, 136)),
        ((24, 24, 27), (153, 27, 27)),
        ((20, 20, 35), (67, 56, 202)),
    ]
    top_rgb, bot_rgb = palettes[scene_idx % len(palettes)]
    width, height = 1080, 1920
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / height
        r = int(top_rgb[0] * (1 - ratio) + bot_rgb[0] * ratio)
        g = int(top_rgb[1] * (1 - ratio) + bot_rgb[1] * ratio)
        b = int(top_rgb[2] * (1 - ratio) + bot_rgb[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    img.save(out_path, "PNG")


# ---------------------------------------------------------------------------
# Stage 4B: Pillow Caption Overlay & FFmpeg 1080x1920 Renderer
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


def render_caption_overlay_png(
    headline: str,
    narration_chunk: str,
    trend_badge: str,
    out_png: Path,
) -> None:
    """Renders a transparent 1080x1920 PNG overlay with a top trend pill, headline banner, and bottom captions."""
    width, height = 1080, 1920
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Subtle bottom and top vignette gradients for legibility
    for y in range(0, 380):
        alpha = int(150 * (1 - y / 380))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
    for y in range(height - 650, height):
        alpha = int(195 * ((y - (height - 650)) / 650))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

    font_badge = _load_font(30)
    font_headline = _load_font(52)
    font_sub = _load_font(44)

    # 1. Top Trend Badge
    badge_text = f"TRENDING: {trend_badge.upper()[:34]}"
    bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bx = (width - bw) // 2
    by = 140
    draw.rounded_rectangle(
        [bx - 28, by - 14, bx + bw + 28, by + bh + 18],
        radius=24,
        fill=(220, 38, 38, 230),
    )
    draw.text((bx, by), badge_text, font=font_badge, fill=(255, 255, 255, 255))

    # 2. Upper-Middle Headline Banner
    wrapped_head = textwrap.wrap(headline.upper(), width=20)[:2]
    hy = 240
    for line in wrapped_head:
        lb = draw.textbbox((0, 0), line, font=font_headline)
        lw, lh = lb[2] - lb[0], lb[3] - lb[1]
        lx = (width - lw) // 2
        draw.text((lx + 3, hy + 3), line, font=font_headline, fill=(0, 0, 0, 220))
        draw.text((lx, hy), line, font=font_headline, fill=(250, 204, 21, 255))
        hy += lh + 16

    # 3. Lower-Third Narration Captions
    wrapped_sub = textwrap.wrap(narration_chunk, width=26)[:4]
    sy = height - 470
    for line in wrapped_sub:
        lb = draw.textbbox((0, 0), line, font=font_sub)
        lw, lh = lb[2] - lb[0], lb[3] - lb[1]
        lx = (width - lw) // 2
        draw.rounded_rectangle(
            [lx - 20, sy - 8, lx + lw + 20, sy + lh + 14],
            radius=14,
            fill=(15, 23, 42, 205),
        )
        draw.text((lx, sy), line, font=font_sub, fill=(255, 255, 255, 255))
        sy += lh + 28

    overlay.save(out_png, "PNG")


def split_narration_into_chunks(narration: str, max_words: int = 8) -> List[str]:
    words = narration.split()
    if not words:
        return [""]
    return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]


def render_scene_clip(
    scene_idx: int,
    scene: SceneSpec,
    trend_topic: str,
    audio_path: Path,
    target_scene_duration: float,
    config: BotConfig,
    work_dir: Path,
) -> Path:
    """Renders a single scene into a 1080x1920 30fps H.264 MP4 synced to target_scene_duration."""
    padded_audio = work_dir / f"scene_{scene_idx:02d}_padded.mp3"
    pad_audio_with_silence(audio_path, target_scene_duration, padded_audio)

    chunks = split_narration_into_chunks(scene.narration, max_words=8)
    chunk_duration = target_scene_duration / len(chunks)

    bg_video_path = work_dir / f"scene_{scene_idx:02d}_raw.mp4"
    bg_image_path = work_dir / f"scene_{scene_idx:02d}_bg.png"

    got_video = False
    if config.visual_source in ("auto", "pexels") and config.pexels_api_key:
        got_video = fetch_pexels_vertical_video(scene.search_query, config.pexels_api_key, bg_video_path)

    if not got_video:
        got_img = False
        if config.visual_source in ("auto", "pollinations"):
            got_img = fetch_pollinations_vertical_image(
                scene.image_prompt, seed=int(time.time()) + scene_idx * 97, out_path=bg_image_path
            )
        if not got_img:
            create_fallback_background(bg_image_path, scene_idx)

    subclip_paths: List[Path] = []
    for c_idx, chunk_text in enumerate(chunks):
        overlay_png = work_dir / f"scene_{scene_idx:02d}_ov_{c_idx:02d}.png"
        render_caption_overlay_png(scene.overlay_text, chunk_text, trend_topic, overlay_png)
        sub_out = work_dir / f"scene_{scene_idx:02d}_sub_{c_idx:02d}.mp4"

        if got_video:
            cmd = [
                "ffmpeg",
                "-y",
                "-stream_loop",
                "-1",
                "-ss",
                f"{c_idx * chunk_duration:.2f}",
                "-t",
                f"{chunk_duration:.3f}",
                "-i",
                str(bg_video_path),
                "-i",
                str(overlay_png),
                "-filter_complex",
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p[bg];"
                "[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
                "-map",
                "[v]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                str(sub_out),
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-t",
                f"{chunk_duration:.3f}",
                "-i",
                str(bg_image_path),
                "-i",
                str(overlay_png),
                "-filter_complex",
                "[0:v]scale=1140:2026:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p[bg];"
                "[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
                "-map",
                "[v]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                str(sub_out),
            ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subclip_paths.append(sub_out)

    concat_list = work_dir / f"scene_{scene_idx:02d}_subs.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in subclip_paths),
        encoding="utf-8",
    )
    scene_final = work_dir / f"scene_{scene_idx:02d}_final.mp4"
    mux_cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-i",
        str(padded_audio),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(scene_final),
    ]
    subprocess.run(mux_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return scene_final


def assemble_full_reel(
    script: ReelScript,
    audio_paths: List[Path],
    config: BotConfig,
    work_dir: Path,
) -> Path:
    """
    Renders all scenes and guarantees the final 1080x1920 MP4 is at least `config.min_video_seconds`
    (default 125s / >2 minutes) while staying safely below `config.max_video_seconds` (175s / <3 minutes).
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

    scene_clips: List[Path] = []
    for idx, (scene, audio_path, base_dur) in enumerate(zip(script.scenes, audio_paths, raw_durations)):
        target_dur = base_dur + extra_per_scene
        logger.info(
            "Rendering scene %d/%d ('%s', %.1fs)...",
            idx + 1,
            len(script.scenes),
            scene.overlay_text,
            target_dur,
        )
        clip = render_scene_clip(idx, scene, script.trend_topic, audio_path, target_dur, config, work_dir)
        scene_clips.append(clip)

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
    logger.info("Final vertical Reel/Short saved to %s (%.1fs)", final_mp4, get_media_duration(final_mp4))
    return final_mp4


# ---------------------------------------------------------------------------
# Stage 5A: YouTube Shorts Automated Uploader (YouTube Data API v3)
# ---------------------------------------------------------------------------

def upload_to_youtube_shorts(video_path: Path, script: ReelScript, config: BotConfig) -> Optional[str]:
    """Uploads the vertical MP4 (up to 3 mins) to YouTube Shorts using an OAuth 2.0 Refresh Token."""
    if not (config.yt_client_id and config.yt_client_secret and config.yt_refresh_token):
        logger.warning("YouTube credentials not fully configured; skipping YouTube Shorts upload.")
        return None

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=config.yt_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.yt_client_id,
        client_secret=config.yt_client_secret,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )

    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": script.title[:100],
            "description": f"{script.caption}\n\n#Shorts #Trending",
            "tags": script.tags[:15],
            "categoryId": "28",  # 28 = Science & Tech (use 25 for News, 22 for People & Blogs)
        },
        "status": {
            "privacyStatus": config.yt_privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True, chunksize=4 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    logger.info("Uploading '%s' to YouTube Shorts...", script.title)
    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response.get("id")
    short_url = f"https://www.youtube.com/shorts/{video_id}"
    logger.info("Published to YouTube Shorts: %s", short_url)
    return short_url


def interactive_youtube_oauth_setup() -> None:
    """One-time local helper to generate YOUTUBE_REFRESH_TOKEN from a Google Cloud client_secret.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret_path = Path("client_secret.json")
    if not secret_path.exists():
        print(
            "Error: Place your downloaded OAuth Desktop 'client_secret.json' in the current directory first.\n"
            "Get it from Google Cloud Console -> APIs & Services -> Credentials -> Create OAuth Client ID (Desktop App)."
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(secret_path),
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds = flow.run_local_server(port=8080, access_type="offline", prompt="consent")
    print("\n=== Add these 3 secrets to your GitHub Repository Secrets ===")
    print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}\n")

    env_path = Path(".env")
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        content = re.sub(r"^YOUTUBE_CLIENT_ID=.*", f"YOUTUBE_CLIENT_ID={creds.client_id}", content, flags=re.MULTILINE)
        content = re.sub(r"^YOUTUBE_CLIENT_SECRET=.*", f"YOUTUBE_CLIENT_SECRET={creds.client_secret}", content, flags=re.MULTILINE)
        content = re.sub(r"^YOUTUBE_REFRESH_TOKEN=.*", f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}", content, flags=re.MULTILINE)
        env_path.write_text(content, encoding="utf-8")
        print("Updated .env with YouTube credentials successfully!")


# ---------------------------------------------------------------------------
# Stage 5B: Instagram Reels Automated Uploader (Meta Graph API + instagrapi)
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
    logger.info("Uploading Reel via instagrapi...")
    media = cl.clip_upload(str(video_path), caption=script.caption)
    logger.info("Published to Instagram Reels via instagrapi (pk=%s)", media.pk)
    return str(media.pk)


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

    logger.info("Step 2/5: Generating 2+ minute script with Gemini (%s)...", config.gemini_model)
    script = generate_reel_script(config, trends, past_topics)

    with tempfile.TemporaryDirectory(prefix="reel_bot_") as tmp_str:
        work_dir = Path(tmp_str)

        logger.info("Step 3/5: Synthesizing neural voiceover via edge-tts (%s)...", config.tts_voice)
        audio_paths = generate_scene_voiceovers(script.scenes, config.tts_voice, config.tts_rate, work_dir)

        logger.info("Step 4/5: Fetching visuals & rendering 1080x1920 vertical video (>=%ds)...", config.min_video_seconds)
        final_video = assemble_full_reel(script, audio_paths, config, work_dir)

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
        logger.info("Dry-run enabled: skipping YouTube & Instagram uploads.")
        return final_video

    logger.info("Step 5/5: Publishing to YouTube Shorts and Instagram Reels...")
    yt_url = upload_to_youtube_shorts(final_video, script, config)
    ig_id = upload_to_instagram_reels(final_video, script, config)

    save_topic_history(config.history_file, script.trend_topic)
    logger.info("Pipeline complete! YouTube=%s | Instagram=%s", yt_url, ig_id)
    return final_video


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Daily Trend-to-Reel & YouTube Shorts Bot (2+ Min Edition)")
    parser.add_argument("--dry-run", action="store_true", help="Generate video locally without uploading")
    parser.add_argument(
        "--setup-youtube",
        action="store_true",
        help="Run interactive OAuth flow locally to print YOUTUBE_REFRESH_TOKEN",
    )
    args = parser.parse_args()

    if args.setup_youtube:
        interactive_youtube_oauth_setup()
        return

    run_pipeline(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
