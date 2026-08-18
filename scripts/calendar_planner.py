from __future__ import annotations
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .lib.config import load_json, save_json


def _topic_hash(text: str) -> str:
    # Same hashing scheme as planner._key() so "already used" checks agree.
    return hashlib.sha256(re.sub(r"[^a-z0-9 ]", "", text.lower()).encode()).hexdigest()[:16]


def generate(
    channel_data: dict[str, Any],
    research: list[dict[str, str]],
    history_path: Path,
    output: Path,
    days: int = 30,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    """Spec item #29 Content Calendar: a rule-based day-by-day plan (30/60/90
    days) built from the configured shorts/long cadence and unused research
    candidates. This does not lock in exact topics (research refreshes daily
    and topics can go stale), it plans FORMAT + a few candidate titles per
    day so the dashboard/Telegram can show what's coming."""
    mix = channel_data.get("content_mix", {"shorts_per_day": 1, "long_per_day": 1})
    shorts_per_day, long_per_day = int(mix.get("shorts_per_day", 1)), int(mix.get("long_per_day", 1))
    history = load_json(history_path, [])
    used_hashes = {item.get("topic_hash") for item in history if isinstance(item, dict)}
    # Filter out topics already covered -- this set was computed but never
    # actually applied before, so the calendar kept re-suggesting topics
    # that had already been used.
    candidates = [r for r in research if r.get("title") and _topic_hash(r["title"]) not in used_hashes]
    label = channel or channel_data.get("name", "")
    today = datetime.now(timezone.utc).date()
    calendar: list[dict[str, Any]] = []
    idx = 0
    for offset in range(days):
        day = (today + timedelta(days=offset)).isoformat()
        slots = ["short"] * shorts_per_day + ["long"] * long_per_day
        picks = []
        for _ in slots:
            if idx < len(candidates):
                picks.append(candidates[idx]["title"])
                idx += 1
            else:
                picks.append("(research pool exhausted — will re-run research.py closer to this date)")
        calendar.append({"date": day, "channel": label, "slots": slots, "candidate_titles": picks})
    save_json(output, calendar)
    return calendar
