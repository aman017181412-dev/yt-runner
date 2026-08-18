from __future__ import annotations
import os
from datetime import datetime, timezone
from pathlib import Path
from .lib.config import load_json, save_json
from .youtube import search_competitors

# ---------------------------------------------------------------------------
# Instead of one fixed target duration for every niche, this looks at what's
# ACTUALLY succeeding in this specific niche on YouTube right now and
# matches it: a "sleeping story" channel's competitors run 1+ hour; a
# "health tips" channel's competitors run a few minutes -- using the same
# target for both would make either format wrong for its audience.
#
# View-weighted (a video with 500k views says more about what works than
# one with 200 views), separately bucketed into short-form (<=3 min --
# YouTube's own Shorts duration ceiling since its Oct-2024 rule change) and
# long-form competitors, cached per niche and refreshed at most weekly since
# it costs a real YouTube Data API call.
# ---------------------------------------------------------------------------

_SHORT_BUCKET_MAX_SECONDS = 180.0
_SHORT_FLOOR_SECONDS, _SHORT_CEILING_SECONDS = 15.0, 180.0
_LONG_FLOOR_SECONDS, _LONG_CEILING_SECONDS = 90.0, 4 * 3600.0  # 4h ceiling is a sanity valve, not a real target
_FALLBACK_SHORT_SECONDS = 45.0
_FALLBACK_LONG_SECONDS = 360.0  # 6 minutes -- a generic, unremarkable explainer length
WORDS_PER_SECOND = 2.5  # ~150 words/minute, a typical narration TTS pace


def target_words(seconds: float) -> int:
    return max(20, round(seconds * WORDS_PER_SECOND))


def _weighted_median(rows: list[tuple[float, float]]) -> float | None:
    """View-weighted median duration: sort by duration, walk cumulative
    view-weight until crossing the halfway point. Median rather than mean
    so one viral outlier with an unusual length doesn't drag the target off
    of what's actually typical for the niche."""
    ordered = sorted(rows, key=lambda r: r[0])
    total = sum(w for _, w in ordered)
    if total <= 0:
        return None
    half, cumulative = total / 2, 0.0
    for duration, weight in ordered:
        cumulative += weight
        if cumulative >= half:
            return duration
    return ordered[-1][0]


def decide(niche: str, folder: Path, max_age_days: int = 7) -> dict:
    """Cached in duration_strategy.json. Safe to call more than once in the
    same pipeline run (script_writer.py and run_pipeline.py both need this)
    -- a same-day call for the same niche just re-reads the cache, no
    extra API usage."""
    path = folder / "duration_strategy.json"
    cached = load_json(path, {})
    if cached.get("niche") == niche and cached.get("computed_at"):
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["computed_at"])).days
            if age_days < max_age_days:
                return cached
        except ValueError:
            pass

    key = os.getenv("YOUTUBE_RESEARCH_API_KEY")
    short_rows: list[tuple[float, float]] = []
    long_rows: list[tuple[float, float]] = []
    if key:
        try:
            # max_results=25 (vs competitor.py's 10 for topic-planning) --
            # need enough of a sample to get a real short-vs-long split,
            # not just whatever the single top result happens to be.
            competitors = search_competitors(niche, key, max_results=25, include_duration=True)
            for row in competitors:
                duration = row.get("duration_seconds")
                if not duration or duration <= 0:
                    continue
                weight = max(1.0, float(row.get("view_count", 0)))
                (short_rows if duration <= _SHORT_BUCKET_MAX_SECONDS else long_rows).append((duration, weight))
        except Exception as exc:
            print(f"Duration-strategy competitor analysis unavailable, using fallback targets: {exc}")

    short_seconds = _weighted_median(short_rows)
    long_seconds = _weighted_median(long_rows)
    result = {
        "niche": niche,
        "short_seconds": round(min(max(short_seconds or _FALLBACK_SHORT_SECONDS, _SHORT_FLOOR_SECONDS), _SHORT_CEILING_SECONDS), 1),
        "long_seconds": round(min(max(long_seconds or _FALLBACK_LONG_SECONDS, _LONG_FLOOR_SECONDS), _LONG_CEILING_SECONDS), 1),
        "short_sample_size": len(short_rows),
        "long_sample_size": len(long_rows),
        "short_is_fallback": short_seconds is None,
        "long_is_fallback": long_seconds is None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(path, result)
    return result
