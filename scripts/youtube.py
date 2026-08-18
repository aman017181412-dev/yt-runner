from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any
from .lib.notify import send as notify
from .lib.config import channel_yt_token


def build_credentials(token_json: str):
    """Build refreshable OAuth credentials for a channel.

    The per-channel token (see lib.config.channel_yt_token) stores the
    refresh token. If that token JSON doesn't already carry its own
    client_id/client_secret, YOUTUBE_CLIENT_SECRET_JSON is used as a
    shared fallback client. Note: a shared client means every channel
    using it draws against ONE Google Cloud project's 10,000 unit/day
    quota together, not independently -- for truly separate per-channel
    quota (the point of running each channel under its own Google
    account), give each channel's own token JSON its own client_id and
    client_secret from its own Cloud project instead of relying on this
    shared fallback.
    """
    from google.oauth2.credentials import Credentials

    token_info = json.loads(token_json)
    if "client_id" not in token_info or "client_secret" not in token_info:
        client_raw = os.getenv("YOUTUBE_CLIENT_SECRET_JSON")
        if not client_raw:
            raise RuntimeError("YOUTUBE_CLIENT_SECRET_JSON is missing; upload cannot start safely.")
        client_info = json.loads(client_raw)
        client_config = client_info.get("installed") or client_info.get("web") or client_info
        token_info.setdefault("client_id", client_config.get("client_id"))
        token_info.setdefault("client_secret", client_config.get("client_secret"))
        token_info.setdefault("token_uri", client_config.get("token_uri", "https://oauth2.googleapis.com/token"))
    return Credentials.from_authorized_user_info(
        token_info, scopes=["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/yt-analytics.readonly"]
    )


def _service_for(channel: str):
    token = channel_yt_token(channel)
    if not token:
        raise RuntimeError(
            f"No YouTube token for '{channel}': set {channel.upper()}_YT_TOKEN, or add "
            f"'{channel}' to the YT_CHANNEL_TOKENS_JSON secret. This operation cannot start safely."
        )
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=build_credentials(token))


def set_thumbnail(video_id: str, channel: str, thumbnail: Path) -> None:
    """Set (or swap) a video's thumbnail. Used both by upload() for the
    initial thumbnail and by ab_test.py to rotate between variants. Custom
    thumbnails require the uploading channel to be in good standing
    (phone-verified) -- callers should treat a failure here as non-fatal."""
    from googleapiclient.http import MediaFileUpload

    service = _service_for(channel)
    service.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail))).execute()


def upload(video: Path, metadata: dict[str, Any], channel: str, thumbnail: Path | None = None, publish_at: str | None = None) -> str:
    import time
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    service = _service_for(channel)
    status: dict[str, Any] = {"privacyStatus": metadata.get("privacy", "private")}
    if publish_at:
        # YouTube's scheduled-publish mechanism (spec-driven by
        # audience.py's per-video timing analysis): a video with
        # privacyStatus "private" + a future publishAt automatically flips
        # to public at that moment -- no cron precision-timing needed on our
        # side. Scheduling REQUIRES privacyStatus "private" regardless of
        # the channel's configured publish_privacy; YouTube also always
        # transitions a scheduled video to "public", never "unlisted" -- a
        # platform constraint, not a choice made here.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at
    body = {
        "snippet": {"title": metadata.get("title", "Untitled"), "description": metadata.get("description", ""), "tags": metadata.get("tags", [])},
        "status": status,
    }
    request = service.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(video), chunksize=-1, resumable=True))
    response = None
    retries = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as exc:
            # Everything upstream (research/script/voice/footage/edit/
            # thumbnail) has already succeeded by this point -- letting a
            # single transient 5xx during a multi-minute upload abort the
            # whole run would throw away all of that work over what a retry
            # would likely fix. Google's own resumable-upload guidance is to
            # retry these with backoff; non-transient errors (4xx, bad
            # metadata, etc.) still raise immediately.
            if exc.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                wait = min(2 ** retries, 60)
                print(f"Upload chunk failed with HTTP {exc.resp.status}, retrying in {wait}s ({retries}/5): {exc}")
                time.sleep(wait)
                continue
            raise
        except (ConnectionError, TimeoutError) as exc:
            if retries < 5:
                retries += 1
                wait = min(2 ** retries, 60)
                print(f"Upload chunk network error, retrying in {wait}s ({retries}/5): {exc}")
                time.sleep(wait)
                continue
            raise
    video_id = str(response["id"])

    # Attach the primary generated thumbnail. The video has already uploaded
    # successfully at this point, so a failure here must not raise --
    # YouTube will just keep its auto-picked thumbnail instead. It's a
    # common, expected failure (custom thumbnails require the channel to be
    # phone-verified), but silent failures are confusing, so it's reported
    # over Telegram rather than only printed to the Actions job log.
    if thumbnail is not None and Path(thumbnail).exists():
        try:
            set_thumbnail(video_id, channel, Path(thumbnail))
        except Exception as exc:
            print(f"Custom thumbnail upload failed (video is still live): {exc}")
            notify(
                f"[{channel}] Video uploaded, but the custom thumbnail could not be attached "
                f"(video_id={video_id}). This usually means the channel isn't phone-verified for "
                f"custom thumbnails yet -- verify at youtube.com/verify, or check the Actions log for the exact error: {exc}"
            )
            # Send the image itself too -- the notify() text alone left the
            # user with no way to get the generated thumbnail onto the
            # video except regenerating it; this way they have the actual
            # file to add manually from YouTube Studio in the meantime.
            from .telegram_bot import send_photo
            if not send_photo(Path(thumbnail), f"[{channel}] Thumbnail for video_id={video_id} (attach failed, add manually)"):
                print("Sending the thumbnail image over Telegram also failed; it only exists in this job's workspace now.")

    return video_id


def update_title(video_id: str, channel: str, title: str) -> bool:
    """Change a video's title after upload -- YouTube allows this any time,
    with no penalty (unlike re-uploading, which resets the video entirely).
    Used by reoptimize.py (underperformer correction) and ab_test.py's
    title-variant rotation. videos().update requires the FULL snippet, not
    a partial patch, so the existing snippet is read first and only the
    title field is changed -- sending a snippet with just {"title": ...}
    would wipe the description/tags/categoryId."""
    try:
        service = _service_for(channel)
        current = service.videos().list(part="snippet", id=video_id).execute()
        items = current.get("items") or []
        if not items:
            return False
        snippet = items[0]["snippet"]
        snippet["title"] = title[:100]
        service.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()
        return True
    except Exception as exc:
        print(f"Could not update title for {video_id}: {exc}")
        return False


def ensure_playlist(channel: str, title: str, description: str = "") -> str | None:
    """Find-or-create a playlist by title and return its ID. Grouping a
    channel's own videos into niche playlists is a real session-watch-time
    lever -- YouTube can chain "watch next" within the channel instead of
    routing viewers elsewhere. Looked up by title each call (cheap, one
    list() of up to 50 playlists) rather than cached, so repeat pipeline
    runs never create duplicate playlists."""
    try:
        service = _service_for(channel)
        resp = service.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        for item in resp.get("items", []):
            if item["snippet"]["title"].strip().lower() == title.strip().lower():
                return item["id"]
        created = service.playlists().insert(
            part="snippet,status",
            body={"snippet": {"title": title, "description": description}, "status": {"privacyStatus": "public"}},
        ).execute()
        return created.get("id")
    except Exception as exc:
        print(f"Could not find/create playlist '{title}': {exc}")
        return None


def add_to_playlist(video_id: str, playlist_id: str, channel: str) -> bool:
    try:
        service = _service_for(channel)
        service.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        ).execute()
        return True
    except Exception as exc:
        print(f"Could not add {video_id} to playlist {playlist_id}: {exc}")
        return False


def post_comment(video_id: str, channel: str, text: str) -> bool:
    """Post a top-level comment (e.g. a subscribe/playlist CTA) right after
    upload -- an early first comment is a real, common engagement-signal
    tactic. Note: YouTube Data API v3 has no public endpoint for PINNING a
    comment -- only the channel owner can pin, and only through Studio's UI
    (there is no equivalent Data API call). This posts the comment only;
    pin it manually in Studio afterward if you want it pinned."""
    try:
        service = _service_for(channel)
        service.commentThreads().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}},
        ).execute()
        return True
    except Exception as exc:
        print(f"Could not post comment on {video_id}: {exc}")
        return False


def parse_iso8601_duration(value: str) -> float:
    """'PT1H2M10S' -> 3730.0 seconds. YouTube's contentDetails.duration is
    always ISO 8601; this only needs the H/M/S components real videos
    actually use."""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0.0
    h, mnt, s = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mnt * 60 + s)


def search_competitors(query: str, api_key: str, max_results: int = 10, include_duration: bool = False) -> list[dict[str, Any]]:
    """Spec item #3 — competitor analysis: find top videos for a niche and
    pull their view counts so the planner/learning code can see what's
    already working before picking a topic. `include_duration=True` (used
    by duration_strategy.py) additionally requests contentDetails and
    parses each video's real length -- off by default since it's an extra
    field most callers of this function don't need."""
    import requests

    search = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={"part": "snippet", "q": query, "type": "video", "order": "viewCount", "maxResults": max_results, "key": api_key},
        timeout=20,
    ).json()
    ids = [item["id"]["videoId"] for item in search.get("items", []) if item.get("id", {}).get("videoId")]
    if not ids:
        return []
    parts = "snippet,statistics,contentDetails" if include_duration else "snippet,statistics"
    stats = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": parts, "id": ",".join(ids), "key": api_key},
        timeout=20,
    ).json()
    rows = []
    for item in stats.get("items", []):
        snippet, stat = item.get("snippet", {}), item.get("statistics", {})
        row = {
            "video_id": item.get("id"), "title": snippet.get("title", ""), "channel": snippet.get("channelTitle", ""),
            "published_at": snippet.get("publishedAt", ""), "view_count": int(stat.get("viewCount", 0)),
            "like_count": int(stat.get("likeCount", 0)) if stat.get("likeCount") else None,
        }
        if include_duration:
            row["duration_seconds"] = parse_iso8601_duration(item.get("contentDetails", {}).get("duration", ""))
        rows.append(row)
    return sorted(rows, key=lambda r: r["view_count"], reverse=True)
