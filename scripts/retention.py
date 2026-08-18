from __future__ import annotations
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from .lib.config import load_json, save_json, channel_yt_token
from .llm import complete

# ---------------------------------------------------------------------------
# Complements hooks.py, which only looks at the first ~10% of the retention
# curve (did the hook work). This looks at the WHOLE curve to find where
# mid/late-video drop-off actually happens -- pacing or structure problems a
# hook-only view can't see -- and turns a consistent channel-wide pattern
# into a pacing instruction for script_writer.py, the same way
# hooks.update_guidance() does for openings.
# ---------------------------------------------------------------------------


def fetch_curve(video_id: str, channel: str) -> list[tuple[float, float]] | None:
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
        return sorted((float(elapsed), float(ratio)) for elapsed, ratio in rows)
    except Exception as exc:
        print(f"Full retention curve unavailable for {video_id} (continuing without it): {exc}")
        return None


def _biggest_drop(curve: list[tuple[float, float]]) -> tuple[float, float] | None:
    """(elapsed_ratio, drop_amount) for the steepest point of viewer loss
    between two consecutive samples, ignoring the first 15% -- that's the
    hook, already covered by hooks.py, and re-flagging it here would just
    duplicate that guidance instead of finding a distinct pacing problem."""
    worst = None
    for (t1, r1), (t2, r2) in zip(curve, curve[1:]):
        if t1 < 0.15:
            continue
        drop = r1 - r2
        if drop > 0 and (worst is None or drop > worst[1]):
            worst = (t2, drop)
    return worst


def refresh(folder: Path, channel: str, analytics_path: Path, max_videos: int = 15) -> int:
    """Weekly job (call alongside hooks.refresh_retention()): for recently
    uploaded videos not yet analyzed, fetch the full retention curve and
    record where the steepest mid/late-video drop-off happens."""
    known_path = folder / "retention_curve.json"
    known = load_json(known_path, [])
    known_ids = {r.get("video_id") for r in known}
    candidates = [
        r for r in load_json(analytics_path, [])
        if isinstance(r, dict) and r.get("video_id") and r.get("status") == "uploaded" and r["video_id"] not in known_ids
    ]
    added = 0
    for row in candidates[-max_videos:]:
        curve = fetch_curve(row["video_id"], channel)
        if not curve or len(curve) < 5:
            continue
        drop = _biggest_drop(curve)
        known.append({
            "video_id": row["video_id"], "topic": row.get("topic"),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "drop_at_ratio": drop[0] if drop else None, "drop_amount": round(drop[1], 4) if drop else None,
        })
        added += 1
    if added:
        save_json(known_path, known[-200:])
    return added


def update_pacing_guidance(folder: Path, min_rows: int = 5) -> str:
    rows = [r for r in load_json(folder / "retention_curve.json", [])[-15:] if r.get("drop_at_ratio") is not None]
    if len(rows) < min_rows:
        return ""

    def _bucket(ratio: float) -> str:
        return "middle third" if ratio < 0.66 else "final third"

    buckets = Counter(_bucket(r["drop_at_ratio"]) for r in rows)
    worst_bucket, count = buckets.most_common(1)[0]
    if count < max(3, len(rows) // 2):
        return ""  # no consistent channel-wide pattern -- don't fabricate advice from noise

    prompt = (
        f"Across this channel's last {len(rows)} videos, the biggest single viewer drop-off most often "
        f"happens in the video's {worst_bucket}. In 1-2 sentences, give a concrete script-pacing instruction "
        f"to reduce that specific drop-off (e.g. tighten a slow section, add a mid-video pattern interrupt, "
        f"move a payoff earlier). Avoid generic advice like 'keep it engaging'."
    )
    try:
        guidance = complete("You are a YouTube retention analyst who gives specific, actionable pacing notes.", prompt, temperature=0.3)
    except Exception as exc:
        print(f"Pacing guidance generation failed, keeping previous guidance if any: {exc}")
        return load_json(folder / "pacing_guidance.json", {}).get("guidance", "")
    save_json(folder / "pacing_guidance.json", {"guidance": guidance, "generated_at": datetime.now(timezone.utc).isoformat(), "based_on": worst_bucket})
    return guidance


def current_pacing_guidance(folder: Path) -> str:
    return load_json(folder / "pacing_guidance.json", {}).get("guidance", "")
