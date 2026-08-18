from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .lib.config import load_json, save_json
from .lib.llm_json import extract_json
from .llm import complete

# ---------------------------------------------------------------------------
# What the user asked for: instead of one fixed publish schedule for the
# whole channel, reason about THIS video's likely audience (age range,
# psychology, when they're online) the way a human social-media strategist
# would -- the same structure as the reference document they shared -- and
# use that to pick a publish time for this specific video.
#
# Important honesty note (kept here, not just in chat): there is no API or
# data feed that reports a not-yet-uploaded video's actual audience
# behavior. This is an LLM reasoning pass grounded in the topic, niche, and
# format -- a genuinely useful heuristic, same as a strategist's judgment
# call, but not a measured fact. It's clearly labeled as such wherever it's
# surfaced (decision_log, dashboard).
# ---------------------------------------------------------------------------


def analyze(topic: dict[str, Any], channel_data: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        f"Video topic: {topic['title']}\nNiche: {topic['niche']}\nFormat: {topic.get('format', 'long')}\n"
        f"Audience language: {channel_data.get('language', 'en')}\n\n"
        "Reason like a social media strategist profiling this specific video's likely audience. "
        "Return JSON only, no markdown fences, with keys: age_range (string), psychology (1 sentence on why "
        "they'd click and watch), best_hours_utc (array of 1-3 integers 0-23, UTC, when this audience is "
        "most likely to be watching), reason (1 sentence explaining the hours)."
    )
    try:
        text = complete("You are a YouTube audience strategist. Be specific to the topic, not generic.", prompt, temperature=0.4)
        data = extract_json(text)
        if not isinstance(data, dict) or not data.get("best_hours_utc"):
            raise ValueError("audience analysis response missing best_hours_utc")
        data["best_hours_utc"] = [int(h) % 24 for h in data["best_hours_utc"]][:3]
        return data
    except Exception as exc:
        print(f"Audience analysis unavailable, falling back to the channel's configured schedule: {exc}")
        return {}


def next_occurrence(hour_utc: int, *, min_lead_minutes: int = 20) -> str:
    """The next UTC clock time matching `hour_utc`, at least
    `min_lead_minutes` from now (so YouTube always has time to process the
    upload before the scheduled moment) -- today if it's still far enough
    ahead, otherwise tomorrow."""
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if candidate < now + timedelta(minutes=min_lead_minutes):
        candidate += timedelta(days=1)
    return candidate.isoformat()


def record(folder: Path, topic: dict[str, Any], profile: dict[str, Any], scheduled_for: str | None) -> None:
    path = folder / "audience_profiles.json"
    rows = load_json(path, [])
    rows.append({
        "timestamp": datetime.now(timezone.utc).isoformat(), "topic": topic.get("title"),
        "age_range": profile.get("age_range"), "psychology": profile.get("psychology"),
        "best_hours_utc": profile.get("best_hours_utc"), "reason": profile.get("reason"),
        "scheduled_for": scheduled_for,
    })
    save_json(path, rows[-200:])
