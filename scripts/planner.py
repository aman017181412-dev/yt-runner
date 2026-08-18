from __future__ import annotations
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .lib.config import load_json, save_json


def _key(text: str) -> str:
    return hashlib.sha256(re.sub(r"[^a-z0-9 ]", "", text.lower()).encode()).hexdigest()[:16]


def choose_topic(research: list[dict[str, str]], history_path: Path, niche: str) -> dict[str, str]:
    history = load_json(history_path, [])
    used = {item.get("topic_hash") for item in history if isinstance(item, dict)}
    for rank, item in enumerate(research):
        title = item.get("title", "").strip()
        topic_hash = _key(title)
        if topic_hash not in used:
            chosen = {
                "title": title,
                "source": item.get("source", "unknown"),
                "url": item.get("url", ""),
                "topic_hash": topic_hash,
                "niche": niche,
                # Spec section 13, "AI Decision Log": a short human-readable
                # reason this specific topic was picked, so the dashboard
                # can show it without anyone needing to read logs.
                "why": (
                    f"first unused candidate (rank {rank + 1} of {len(research)} researched) "
                    f"from source '{item.get('source', 'unknown')}'; {len(used)} topics already used for this niche"
                ),
            }
            history.append(chosen)
            save_json(history_path, history[-500:])
            return chosen
    raise RuntimeError("No unused topic found; add more research sources or review topics_history.json")


# ---------------------------------------------------------------------------
# Spec item #16 Content Mix Planner: decide short vs long form for today's
# run, based on today's quota (from learning.get_or_decide_daily_quota(),
# which may have adjusted the channel's configured base content_mix for the
# day) and how many of each already went out today, per analytics_log.json.
# ---------------------------------------------------------------------------
def decide_format(quota: dict[str, Any], analytics_path: Path) -> tuple[str, str]:
    shorts_quota, long_quota = int(quota.get("shorts_per_day", 1)), int(quota.get("long_per_day", 1))
    today = datetime.now(timezone.utc).date().isoformat()
    rows = [r for r in load_json(analytics_path, []) if isinstance(r, dict) and str(r.get("timestamp", "")).startswith(today) and r.get("status") == "uploaded"]
    shorts_done = sum(1 for r in rows if r.get("format") == "short")
    long_done = sum(1 for r in rows if r.get("format") == "long")
    if shorts_done < shorts_quota:
        return "short", f"shorts quota not yet met today ({shorts_done}/{shorts_quota})"
    if long_done < long_quota:
        return "long", f"long-form quota not yet met today ({long_done}/{long_quota})"
    fallback = "short" if shorts_quota >= long_quota else "long"
    return fallback, "today's quotas for both formats are already met; falling back to the larger configured quota"


# ---------------------------------------------------------------------------
# Spec section 13, "AI Decision Log": persist why today's topic and format
# were chosen so the dashboard can show it without reading raw logs. Kept
# separate from state.json (which tracks pipeline progress, not reasoning)
# and from topics_history.json (which exists purely to prevent repeats).
# ---------------------------------------------------------------------------
def log_decision(folder: Path, topic: dict[str, Any], format_reason: str, quota: dict[str, Any] | None = None) -> None:
    path = folder / "decision_log.json"
    entries = load_json(path, [])
    entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "topic": topic.get("title"),
        "topic_reason": topic.get("why", ""),
        "format": topic.get("format"),
        "format_reason": format_reason,
        "quota_reason": (quota or {}).get("reason"),
    })
    save_json(path, entries[-200:])
