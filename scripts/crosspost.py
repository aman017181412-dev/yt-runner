from __future__ import annotations
import os
import time
from pathlib import Path
from typing import Any
import requests

# ---------------------------------------------------------------------------
# Optional traffic-funnel feature: re-post a Short to Instagram Reels and/or
# TikTok with a caption pointing back to the YouTube video. Fully env-gated
# -- with no credentials set, post_short() no-ops immediately and the main
# pipeline is completely unaffected.
#
# IMPORTANT, READ BEFORE ENABLING: neither platform's API accepts a raw file
# upload directly from a GitHub Actions runner in one call the way YouTube's
# resumable upload does -- both require the video to already be reachable at
# a public HTTPS URL. This project deliberately has no persistent file host
# (spec section 8: generated media is never committed to git), so
# CROSSPOST_UPLOAD_WEBHOOK is a manual integration point you must provide
# yourself: a small endpoint of your own (e.g. backed by Cloudflare R2,
# Bunny, or S3) that accepts a POSTed file and returns {"url": "https://..."}.
# Without it, cross-posting is skipped outright -- it never fails partway.
# Instagram also requires a Business/Creator account connected to a Facebook
# Page (Graph API), and TikTok's Content Posting API requires your own
# TikTok developer app to pass their audit before it can publish publicly.
# Neither is a small setup step; treat this as the most involved feature to
# turn on.
# ---------------------------------------------------------------------------


def _public_video_url(video: Path) -> str | None:
    webhook = os.getenv("CROSSPOST_UPLOAD_WEBHOOK")
    if not webhook:
        return None
    try:
        with video.open("rb") as fh:
            response = requests.post(webhook, files={"file": fh}, timeout=120)
        response.raise_for_status()
        return response.json().get("url")
    except Exception as exc:
        print(f"Crosspost: could not obtain a public video URL, skipping: {exc}")
        return None


def _post_instagram(video_url: str, caption: str) -> bool:
    token, ig_user_id = os.getenv("INSTAGRAM_ACCESS_TOKEN"), os.getenv("INSTAGRAM_BUSINESS_ID")
    if not token or not ig_user_id:
        return False
    try:
        create = requests.post(
            f"https://graph.facebook.com/v20.0/{ig_user_id}/media",
            data={"media_type": "REELS", "video_url": video_url, "caption": caption, "access_token": token},
            timeout=60,
        ).json()
        creation_id = create.get("id")
        if not creation_id:
            print(f"Instagram Reels container creation failed: {create}")
            return False
        # Reels processing is async -- Meta's own docs warn the publish call
        # will fail if the container isn't FINISHED yet, so poll status
        # before publishing rather than firing publish immediately.
        for _ in range(10):
            time.sleep(15)
            status = requests.get(
                f"https://graph.facebook.com/v20.0/{creation_id}",
                params={"fields": "status_code", "access_token": token}, timeout=30,
            ).json()
            if status.get("status_code") == "FINISHED":
                break
        else:
            print("Instagram Reels container never finished processing; skipping publish.")
            return False
        publish = requests.post(
            f"https://graph.facebook.com/v20.0/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token}, timeout=60,
        ).json()
        return bool(publish.get("id"))
    except Exception as exc:
        print(f"Instagram cross-post failed: {exc}")
        return False


def _post_tiktok(video_url: str, caption: str) -> bool:
    token = os.getenv("TIKTOK_ACCESS_TOKEN")
    if not token:
        return False
    try:
        response = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "post_info": {"title": caption, "privacy_level": "PUBLIC_TO_EVERYONE"},
                "source_info": {"source": "PULL_FROM_URL", "video_url": video_url},
            },
            timeout=60,
        )
        response.raise_for_status()
        return "publish_id" in response.json().get("data", {})
    except Exception as exc:
        print(f"TikTok cross-post failed: {exc}")
        return False


def post_short(video: Path, seo_metadata: dict[str, Any], channel: str) -> None:
    if not (os.getenv("INSTAGRAM_ACCESS_TOKEN") or os.getenv("TIKTOK_ACCESS_TOKEN")):
        return  # not configured -- silent no-op, this feature is entirely optional
    video_url = _public_video_url(video)
    if not video_url:
        return
    caption = f"{seo_metadata.get('title', '')}\n\nFull video on YouTube (link in bio).".strip()
    if _post_instagram(video_url, caption):
        print(f"[{channel}] Cross-posted to Instagram Reels.")
    if _post_tiktok(video_url, caption):
        print(f"[{channel}] Cross-posted to TikTok.")
