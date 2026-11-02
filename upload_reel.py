import argparse
import json
import logging
import os
import re
from pathlib import Path
from instagrapi import Client
from daily_reel_bot import BotConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("upload_reel")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", type=str, default="", help="6-digit 2FA / email challenge code")
    parser.add_argument("--video", type=str, default="", help="Path to MP4 video")
    args = parser.parse_args()

    config = BotConfig()

    if args.video:
        video_path = Path(args.video)
    else:
        mp4_files = sorted(Path("output").glob("reel_*.mp4"))
        if not mp4_files:
            raise FileNotFoundError("No rendered reel MP4 found in output/")
        video_path = mp4_files[-1]

    meta_path = video_path.with_suffix(".json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        caption = meta.get("caption", "")
    else:
        caption = "New Reel! #Shorts #Reels #Trending"

    logger.info("Target video: %s (%.1f MB)", video_path, video_path.stat().st_size / (1024 * 1024))
    logger.info("Caption: %s", caption[:80] + "...")

    cl = Client()

    session_data = None
    if config.ig_session_json:
        try:
            session_data = json.loads(config.ig_session_json)
        except Exception as e:
            logger.warning("Could not parse config.ig_session_json: %s", e)
    if not session_data and Path("ig_session.json").exists():
        try:
            session_data = json.loads(Path("ig_session.json").read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not parse ig_session.json: %s", e)

    if session_data:
        try:
            logger.info("Attempting login via existing IG_SESSION_JSON...")
            cl.set_settings(session_data)
            cl.login(config.ig_username, config.ig_password)
            logger.info("Logged in via session successfully! User ID: %s", cl.user_id)
        except Exception as exc:
            logger.warning("Session login failed: %s; falling back to direct login", exc)
            cl = Client()

    if not cl.user_id:
        def challenge_code_handler(username, choice):
            if args.code:
                return args.code
            return input(f"Enter 6-digit code sent to {choice}: ").strip()

        cl.challenge_code_handler = challenge_code_handler

        logger.info("Logging into Instagram as %s...", config.ig_username)
        if args.code:
            cl.login(config.ig_username, config.ig_password, verification_code=args.code)
        else:
            cl.login(config.ig_username, config.ig_password)

    logger.info("Successfully authenticated! User ID: %s", cl.user_id)

    session_str = json.dumps(cl.get_settings())
    Path("ig_session.json").write_text(session_str, encoding="utf-8")
    env_path = Path(".env")
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if "IG_SESSION_JSON=" in content:
            content = re.sub(r"^IG_SESSION_JSON=.*", f"IG_SESSION_JSON={session_str}", content, flags=re.MULTILINE)
        else:
            content += f"\nIG_SESSION_JSON={session_str}\n"
        env_path.write_text(content, encoding="utf-8")
        logger.info("Saved IG_SESSION_JSON to .env and ig_session.json")

    thumb_path = video_path.with_suffix(".jpg")
    if not thumb_path.exists():
        logger.info("Extracting thumbnail with FFmpeg...")
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "00:00:02", "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(thumb_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    logger.info("Uploading Reel to Instagram (this may take 1-2 minutes)...")
    media = cl.clip_upload(str(video_path), caption=caption, thumbnail=thumb_path)
    reel_url = f"https://www.instagram.com/reel/{media.code}/"
    logger.info("SUCCESS! Reel published successfully!")
    logger.info("Instagram URL: %s", reel_url)
    print(f"\nREEL_PUBLISHED_URL={reel_url}\n")


if __name__ == "__main__":
    main()
