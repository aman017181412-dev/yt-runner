from __future__ import annotations
from datetime import datetime, timedelta, timezone
from .analytics import fetch_video_metrics
from .lib.config import all_channels, load_config, load_json, save_json
from .lib.llm_json import extract_json
from .llm import complete
from .youtube import update_title

# ---------------------------------------------------------------------------
# YouTube allows changing a video's title/description any time after upload
# with no penalty (unlike re-uploading, which resets everything). This
# checks recently published videos once they've had a fair first look
# (default 30 hours -- long enough for the initial impressions wave, short
# enough that a fix still matters) and, if a video's early view count is
# clearly behind this channel's own recent median, asks the LLM for one
# alternative, more specific title and applies it. Runs at most ONCE per
# video (tracked in reoptimize_log.json) -- this is a single corrective
# nudge, not a repeating experiment; ab_test.py already owns repeated
# thumbnail/title rotation.
# ---------------------------------------------------------------------------

CHECK_AFTER_HOURS = 30
CHECK_WINDOW_HOURS = 6  # candidates land in [30h, 36h) old -- a later hourly pass catches any missed
UNDERPERFORM_RATIO = 0.5  # flag if views are below this fraction of the channel's recent median


def _recent_median_views(analytics_path, exclude_video_id: str) -> float | None:
    rows = [
        r for r in load_json(analytics_path, [])
        if isinstance(r, dict) and r.get("video_id") and r["video_id"] != exclude_video_id
        and isinstance((r.get("metrics") or {}).get("views"), (int, float))
    ]
    if len(rows) < 3:
        return None
    views = sorted(r["metrics"]["views"] for r in rows[-20:])
    mid = len(views) // 2
    return float(views[mid] if len(views) % 2 else (views[mid - 1] + views[mid]) / 2)


def _suggest_title(old_title: str, niche: str) -> str | None:
    prompt = (
        f"This YouTube video's current title is underperforming relative to the channel's typical views: "
        f"\"{old_title}\" (niche: {niche}). Propose ONE alternative title that is more specific and "
        f"click-worthy without becoming misleading or changing the video's actual topic. Return valid JSON "
        f'only, no markdown fences: {{"title": "..."}}'
    )
    try:
        text = complete("You are a YouTube title editor focused on honest, specific, high-CTR titles.", prompt, temperature=0.5)
        result = extract_json(text)
        title = str(result.get("title", "")).strip()
        return title[:100] if title else None
    except Exception as exc:
        print(f"Re-optimization title suggestion failed: {exc}")
        return None


def run_check() -> None:
    """Called from the hourly trigger window, same pattern as
    ab_test.run_rotation()."""
    config = load_config()
    for channel, channel_data, folder in all_channels(config):
        if not channel_data.get("reoptimize", True):
            continue
        analytics_path = folder / "analytics_log.json"
        log_path = folder / "reoptimize_log.json"
        already_checked = {r.get("video_id") for r in load_json(log_path, [])}
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=CHECK_AFTER_HOURS + CHECK_WINDOW_HOURS)
        window_end = now - timedelta(hours=CHECK_AFTER_HOURS)

        candidates = []
        for row in load_json(analytics_path, []):
            if not isinstance(row, dict) or row.get("status") != "uploaded" or not row.get("video_id"):
                continue
            if row["video_id"] in already_checked:
                continue
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except (KeyError, ValueError):
                continue
            if window_start <= ts <= window_end:
                candidates.append(row)
        if not candidates:
            continue

        log = load_json(log_path, [])
        for row in candidates:
            metrics = fetch_video_metrics(row["video_id"], channel)
            entry = {"video_id": row["video_id"], "checked_at": now.isoformat()}
            if not metrics:
                entry["action"] = "metrics_unavailable"
                log.append(entry)
                continue
            median = _recent_median_views(analytics_path, row["video_id"])
            views = metrics.get("views") or 0
            entry["views"], entry["channel_median"] = views, median
            if median and median >= 5 and views < median * UNDERPERFORM_RATIO:
                new_title = _suggest_title(row["topic"], channel_data.get("niche", ""))
                if new_title and update_title(row["video_id"], channel, new_title):
                    entry.update({"action": "retitled", "old_title": row["topic"], "new_title": new_title})
                    print(f"[{channel}] Re-titled underperforming video {row['video_id']}: {new_title!r}")
                else:
                    entry["action"] = "flagged_no_change"
            else:
                entry["action"] = "no_action_needed"
            log.append(entry)
        save_json(log_path, log[-300:])
