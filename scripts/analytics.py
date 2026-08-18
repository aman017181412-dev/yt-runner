from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .lib.config import load_json, save_json, channel_yt_token


def record(
    path: Path, channel: str, topic: str, video_id: str | None, status: str,
    format: str | None = None, thumbnail_style: str | None = None, voice: str | None = None,
) -> None:
    rows = load_json(path, [])
    row = {"timestamp": datetime.now(timezone.utc).isoformat(), "channel": channel, "topic": topic, "video_id": video_id, "status": status}
    if format:
        row["format"] = format
    # thumbnail_style/voice feed learning.update_learning_db() (Future Learning
    # DB, spec section 10) so it can tell which choices correlate with views.
    if thumbnail_style:
        row["thumbnail_style"] = thumbnail_style
    if voice:
        row["voice"] = voice
    rows.append(row)
    save_json(path, rows[-1000:])


def today_upload_count(analytics_path: Path) -> int:
    """Count today's completed runs for quota purposes. This counts both
    "uploaded" and "prepared" (upload_enabled=false / dry-testing) -- if it
    only counted "uploaded", a channel running with uploads turned off would
    never appear to have met its daily quota, and the auto-trigger in
    trigger_window.py would keep dispatching a fresh run every enabled hour
    indefinitely. "rejected" is intentionally NOT counted, since a rejected
    preview means that slot is still genuinely open."""
    today = datetime.now(timezone.utc).date().isoformat()
    rows = load_json(analytics_path, [])
    return sum(
        1 for r in rows
        if isinstance(r, dict) and r.get("status") in ("uploaded", "prepared") and str(r.get("timestamp", "")).startswith(today)
    )


# ---------------------------------------------------------------------------
# Spec item #13: pull real performance metrics (views/CTR/watch time) from
# the YouTube Analytics API for videos uploaded in the last N days, and merge
# them back into analytics_log.json so the dashboard and self-learning code
# (learning.py) have real numbers instead of only pipeline-run status.
# ---------------------------------------------------------------------------
_BASE_METRICS = ["views", "averageViewDuration", "averageViewPercentage", "likes", "comments"]
_CTR_METRICS = ["impressions", "impressionsClickThroughRate"]


def fetch_video_metrics(video_id: str, channel: str) -> dict[str, Any] | None:
    token = channel_yt_token(channel)
    if not token:
        return None
    try:
        from googleapiclient.discovery import build
        from .youtube import build_credentials
        credentials = build_credentials(token)
        yt_analytics = build("youtubeAnalytics", "v2", credentials=credentials)
        end = datetime.now(timezone.utc).date()

        # Impressions/CTR need the "advertised search" traffic-source scope
        # and aren't available for every account, so they're requested
        # separately and simply omitted (rather than failing the whole
        # metrics fetch) if the channel doesn't have access to them.
        def _query(metric_names: list[str]) -> list | None:
            report = yt_analytics.reports().query(
                # Lifetime-to-date, not a rolling recent window: the filter
                # already scopes this to one video, so a fixed early
                # startDate gives cumulative totals. A rolling window here
                # previously meant "views" was really "views in the last 28
                # days as of whenever this happened to run" -- non-
                # monotonic and misleading for anything older than 28 days,
                # which corrupted every learning/ranking feature that
                # compares views across videos of different ages.
                ids="channel==MINE", startDate="2020-01-01", endDate=end.isoformat(),
                metrics=",".join(metric_names), filters=f"video=={video_id}",
            ).execute()
            rows = report.get("rows") or []
            return rows[0] if rows else None

        base_row = _query(_BASE_METRICS)
        if not base_row:
            return None
        views, avg_duration, avg_pct, likes, comments = base_row
        result = {
            "views": views, "average_view_duration_s": avg_duration,
            "average_view_percentage": avg_pct, "likes": likes, "comments": comments,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            ctr_row = _query(_CTR_METRICS)
            if ctr_row:
                result["impressions"], result["impressions_ctr"] = ctr_row
        except Exception as exc:
            print(f"Impressions/CTR unavailable for {video_id} (continuing without it): {exc}")
        return result
    except Exception as exc:
        print(f"YouTube Analytics fetch failed for {video_id}: {exc}")
        return None


def refresh_metrics(analytics_path: Path, channel: str, max_age_days: int = 21, max_calls: int = 15) -> int:
    """Update analytics_log rows that have a video_id but no recent metrics.
    Capped at `max_calls` API calls per invocation -- without a cap, this
    would make one YouTube Analytics call per stale video on every single
    pipeline run, growing unbounded (and risking quota exhaustion) as a
    channel's video count grows over months. Oldest-refreshed-first, so
    every video eventually gets caught up over successive runs rather than
    the same few videos always winning the cap."""
    rows = load_json(analytics_path, [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    def _staleness(row: dict) -> datetime:
        fetched_at = (row.get("metrics") or {}).get("fetched_at")
        if not fetched_at:
            return datetime.min.replace(tzinfo=timezone.utc)  # never fetched -- highest priority
        try:
            return datetime.fromisoformat(fetched_at)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    candidates = [
        row for row in rows
        if isinstance(row, dict) and row.get("video_id") and _staleness(row) <= cutoff
    ]
    candidates.sort(key=_staleness)

    updated = 0
    for row in candidates[:max_calls]:
        fresh = fetch_video_metrics(row["video_id"], channel)
        if fresh:
            row["metrics"] = fresh
            updated += 1
    if updated:
        save_json(analytics_path, rows[-1000:])
    return updated


# ---------------------------------------------------------------------------
# Spec item #27: cost management — count paid/rate-limited API calls per UTC
# day and alert once a configured soft-limit is crossed, so nobody is
# surprised by a free-tier quota getting exhausted mid-run.
# ---------------------------------------------------------------------------
def log_api_call(usage_path: Path, provider: str) -> dict[str, int]:
    today = datetime.now(timezone.utc).date().isoformat()
    data = load_json(usage_path, {})
    day = data.setdefault(today, {})
    day[provider] = day.get(provider, 0) + 1
    for key in list(data.keys()):
        if key < (datetime.now(timezone.utc).date() - timedelta(days=14)).isoformat():
            del data[key]
    save_json(usage_path, data)
    return data[today]


def check_quota_alert(usage_path: Path, limits: dict[str, int]) -> str | None:
    today = datetime.now(timezone.utc).date().isoformat()
    data = load_json(usage_path, {}).get(today, {})
    warnings = [f"{provider}: {count}/{limit}" for provider, limit in limits.items() if (count := data.get(provider, 0)) >= limit]
    if not warnings:
        return None
    return "API usage nearing configured limits today — " + "; ".join(warnings)


# ---------------------------------------------------------------------------
# Powers hooks.py's hook-performance learning: the "audienceWatchRatio" /
# "elapsedVideoTimeRatio" report is YouTube Analytics' actual retention
# curve for one video. Averaging the ratio over the first ~10% of the video
# gives a single "did the hook work" number per video, which is what
# hooks.py ranks hooks by.
# ---------------------------------------------------------------------------
def fetch_early_retention(video_id: str, channel: str, cutoff_ratio: float = 0.1) -> float | None:
    token = channel_yt_token(channel)
    if not token:
        return None
    try:
        from googleapiclient.discovery import build
        from .youtube import build_credentials
        credentials = build_credentials(token)
        yt_analytics = build("youtubeAnalytics", "v2", credentials=credentials)
        report = yt_analytics.reports().query(
            ids="channel==MINE", startDate="2020-01-01", endDate=datetime.now(timezone.utc).date().isoformat(),
            metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio", filters=f"video=={video_id}",
        ).execute()
        rows = report.get("rows") or []
        early = [ratio for elapsed, ratio in rows if elapsed <= cutoff_ratio]
        return round(sum(early) / len(early), 4) if early else None
    except Exception as exc:
        print(f"Retention curve unavailable for {video_id} (continuing without it): {exc}")
        return None
