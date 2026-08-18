from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .lib.config import load_json, save_json
from .llm import complete


# ---------------------------------------------------------------------------
# Spec #21 AI Memory / #14 Self Learning: turn analytics_log.json into a
# short block of text that gets injected into the script/SEO prompts, so the
# model has real feedback about what has and hasn't worked on this channel.
# ---------------------------------------------------------------------------
def memory_context(analytics_path: Path, max_rows: int = 30) -> str:
    rows = [r for r in load_json(analytics_path, []) if isinstance(r, dict)]
    if not rows:
        return ""
    recent = rows[-max_rows:]
    with_metrics = [r for r in recent if r.get("metrics", {}).get("views") is not None]
    lines = [f"Recent uploads on this channel ({len(recent)} runs considered):"]
    if with_metrics:
        best = sorted(with_metrics, key=lambda r: r["metrics"]["views"], reverse=True)[:5]
        worst = sorted(with_metrics, key=lambda r: r["metrics"]["views"])[:3]
        lines.append("Best performing recent topics: " + "; ".join(f"{r['topic']} ({r['metrics']['views']} views)" for r in best))
        lines.append("Weaker recent topics: " + "; ".join(f"{r['topic']} ({r['metrics']['views']} views)" for r in worst))
    else:
        lines.append("No view metrics collected yet — favor variety over repeating recent topics: " + "; ".join(r["topic"] for r in recent[-8:]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spec #17 Best Upload Time: look at which UTC hour recent uploads that
# performed well went out at, and suggest hours for schedule_config.json.
# This only ever WRITES a suggestion field — it never silently changes the
# active enabled_hours_utc, since that should stay a deliberate decision.
# ---------------------------------------------------------------------------
def suggest_upload_hours(analytics_path: Path, schedule_path: Path, top_n: int = 3) -> list[int] | None:
    rows = [r for r in load_json(analytics_path, []) if isinstance(r, dict) and r.get("metrics", {}).get("views") is not None]
    if len(rows) < 5:
        return None
    scored: list[tuple[int, int]] = []
    for row in rows:
        try:
            hour = datetime.fromisoformat(row["timestamp"]).hour
            scored.append((hour, row["metrics"]["views"]))
        except (KeyError, ValueError):
            continue
    if not scored:
        return None
    totals = Counter()
    counts = Counter()
    for hour, views in scored:
        totals[hour] += views
        counts[hour] += 1
    averages = {hour: totals[hour] / counts[hour] for hour in totals}
    best_hours = sorted(averages, key=averages.get, reverse=True)[:top_n]
    schedule = load_json(schedule_path, {})
    schedule["suggested_hours_utc"] = sorted(best_hours)
    schedule["suggested_at"] = datetime.now(timezone.utc).isoformat()
    save_json(schedule_path, schedule)
    return sorted(best_hours)


# ---------------------------------------------------------------------------
# Spec #30 Growth Strategy: periodically ask the LLM to read the trend in
# analytics_log.json and produce a short written strategy note for the
# dashboard. Cheap to run and skipped automatically if there isn't enough
# data yet.
# ---------------------------------------------------------------------------
def growth_report(analytics_path: Path, output: Path, min_rows: int = 8) -> dict[str, Any] | None:
    rows = [r for r in load_json(analytics_path, []) if isinstance(r, dict)]
    if len(rows) < min_rows:
        return None
    summary_lines = []
    for row in rows[-40:]:
        metrics = row.get("metrics") or {}
        views = metrics.get("views", "n/a")
        summary_lines.append(f"- {row.get('timestamp', '')[:10]} | {row.get('topic', '')} | status={row.get('status')} | views={views}")
    prompt = (
        "Here is recent upload history for one YouTube channel:\n" + "\n".join(summary_lines) +
        "\n\nIn under 150 words, note 2-3 concrete patterns (topic types, timing, or format) worth trying next, "
        "and 1 thing worth stopping. Be specific and avoid generic advice."
    )
    text = complete("You are a YouTube channel growth analyst. Be concrete and concise.", prompt, temperature=0.4)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "note": text, "based_on_runs": len(rows)}
    save_json(output, report)
    return report


# ---------------------------------------------------------------------------
# Spec section 10, "Future Learning DB": a small, growing dataset of which
# concrete choices (thumbnail style, voice) correlate with more views, kept
# separate from memory_context()'s free-text recap so it can be computed
# without an LLM call and consulted by thumbnail.py as well as the script
# prompt.
# ---------------------------------------------------------------------------
def update_learning_db(analytics_path: Path, output: Path, min_rows: int = 5) -> dict[str, Any] | None:
    rows = [r for r in load_json(analytics_path, []) if isinstance(r, dict) and r.get("metrics", {}).get("views") is not None]
    if len(rows) < min_rows:
        return None

    def _grouped(field: str) -> list[dict[str, Any]]:
        totals: Counter = Counter()
        counts: Counter = Counter()
        for row in rows:
            key = row.get(field)
            if not key:
                continue
            totals[key] += row["metrics"]["views"]
            counts[key] += 1
        ranked = sorted(counts, key=lambda k: totals[k] / counts[k], reverse=True)
        return [{"value": key, "avg_views": round(totals[key] / counts[key], 1), "sample_size": counts[key]} for key in ranked]

    db = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "based_on_runs": len(rows),
        "thumbnail_style_ranked": _grouped("thumbnail_style"),
        "voice_ranked": _grouped("voice"),
        "format_ranked": _grouped("format"),
    }
    save_json(output, db)
    return db


def best_thumbnail_style(learning_db_path: Path) -> str | None:
    """Read update_learning_db()'s output and return the currently
    best-performing thumbnail style, if there's enough data to have one."""
    db = load_json(learning_db_path, {})
    ranked = db.get("thumbnail_style_ranked") or []
    for entry in ranked:
        if entry.get("sample_size", 0) >= 3:
            return entry.get("value")
    return None


def patterns_context(analytics_path: Path) -> str:
    """Short text block summarizing update_learning_db()'s findings, meant
    for injection into the script prompt alongside memory_context()."""
    db_path = analytics_path.parent / "learning_db.json"
    db = load_json(db_path, {})
    if not db:
        return ""
    lines = ["Patterns from this channel's past performance:"]
    for label, key in (("Thumbnail styles", "thumbnail_style_ranked"), ("Voices", "voice_ranked"), ("Formats", "format_ranked")):
        ranked = db.get(key) or []
        if ranked:
            top = ranked[0]
            lines.append(f"- {label}: '{top['value']}' performs best so far ({top['avg_views']} avg views, {top['sample_size']} samples)")
    return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# Spec section 13, "Channel Health Score": a single composite view of channel
# health (Growth / SEO / CTR / Retention / Consistency / Overall), each
# 0-100, for the dashboard. Every sub-score degrades gracefully to None when
# there isn't enough data yet rather than showing a misleading number.
# ---------------------------------------------------------------------------
def _clip(value: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, value))


def compute_health_score(channel_data: dict[str, Any], analytics_path: Path, output: Path) -> dict[str, Any]:
    rows = [r for r in load_json(analytics_path, []) if isinstance(r, dict)]
    with_metrics = [r for r in rows if r.get("metrics", {}).get("views") is not None]
    scores: dict[str, float | None] = {"growth": None, "seo": None, "ctr": None, "retention": None, "consistency": None}

    if len(with_metrics) >= 6:
        recent, prior = with_metrics[-5:], with_metrics[-10:-5]
        if prior:
            recent_avg = sum(r["metrics"]["views"] for r in recent) / len(recent)
            prior_avg = sum(r["metrics"]["views"] for r in prior) / len(prior) or 1
            growth_pct = ((recent_avg - prior_avg) / prior_avg) * 100
            scores["growth"] = round(_clip(50 + growth_pct), 1)  # 50 = flat, above/below tracks the % change

    if rows:
        seo_rows = rows[-20:]
        # SEO is judged on the same fields generate_seo() always fills, so
        # this is really "did the SEO step produce non-empty output", which
        # is the best proxy available without re-fetching every seo_latest.
        scored = [1 for r in seo_rows if r.get("status") in ("uploaded", "prepared")]
        if scored:
            scores["seo"] = round(_clip(70 + 30 * (sum(scored) / len(seo_rows))), 1)

    ctr_rows = [r for r in with_metrics if r["metrics"].get("impressions_ctr") is not None]
    if ctr_rows:
        avg_ctr = sum(r["metrics"]["impressions_ctr"] for r in ctr_rows) / len(ctr_rows)
        scores["ctr"] = round(_clip(avg_ctr * 100 * 10), 1)  # ~10% CTR -> 100; YouTube average is roughly 2-10%

    pct_rows = [r for r in with_metrics if r["metrics"].get("average_view_percentage") is not None]
    if pct_rows:
        scores["retention"] = round(_clip(sum(r["metrics"]["average_view_percentage"] for r in pct_rows) / len(pct_rows)), 1)

    mix = channel_data.get("content_mix", {"shorts_per_day": 1, "long_per_day": 1})
    expected_per_day = int(mix.get("shorts_per_day", 1)) + int(mix.get("long_per_day", 1))
    if expected_per_day and rows:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        recent_uploads = sum(1 for r in rows if r.get("status") == "uploaded" and str(r.get("timestamp", "")) >= cutoff)
        expected = expected_per_day * 14
        scores["consistency"] = round(_clip((recent_uploads / expected) * 100), 1) if expected else None

    known = [v for v in scores.values() if v is not None]
    overall = round(sum(known) / len(known), 1) if known else None

    result = {"generated_at": datetime.now(timezone.utc).isoformat(), **scores, "overall": overall, "based_on_runs": len(rows)}
    save_json(output, result)
    return result


# ---------------------------------------------------------------------------
# What the user asked for: let the channel's own age and real growth trend
# decide today's video count, instead of a fixed daily number forever. This
# is a rule-based decision grounded in this channel's own analytics -- not a
# live web search for "how to grow on YouTube" (that would be generic advice
# untethered to this channel's actual numbers, which is far less reliable
# than the data already being collected). Runs at most once per UTC day;
# capped by max_shorts_per_day/max_long_per_day in config.json so a rough
# patch doesn't runaway the API budget.
# ---------------------------------------------------------------------------
def _decide_daily_quota(channel_data: dict[str, Any], folder: Path) -> dict[str, Any]:
    base_mix = channel_data.get("content_mix", {"shorts_per_day": 1, "long_per_day": 1})
    base_shorts, base_long = int(base_mix.get("shorts_per_day", 1)), int(base_mix.get("long_per_day", 1))
    max_shorts = int(channel_data.get("max_shorts_per_day", base_shorts * 2))
    max_long = int(channel_data.get("max_long_per_day", base_long * 2))
    today = datetime.now(timezone.utc).date().isoformat()

    rows = [r for r in load_json(folder / "analytics_log.json", []) if isinstance(r, dict) and r.get("status") == "uploaded"]
    if not rows:
        return {"date": today, "shorts_per_day": base_shorts, "long_per_day": base_long, "reason": "no upload history yet; using the configured base quota"}

    timestamps = [r["timestamp"] for r in rows if r.get("timestamp")]
    try:
        first_upload = min(datetime.fromisoformat(t) for t in timestamps)
    except ValueError:
        first_upload = datetime.now(timezone.utc)
    age_days = (datetime.now(timezone.utc) - first_upload).days

    health = load_json(folder / "health_score.json", {})
    growth = health.get("growth")

    if age_days < 14 or growth is None:
        return {
            "date": today, "shorts_per_day": base_shorts, "long_per_day": base_long,
            "reason": f"channel is {age_days}d into uploads or growth score isn't available yet; too early to adjust, using base quota",
        }
    if growth < 40:
        shorts = min(base_shorts + 1, max_shorts)
        return {
            "date": today, "shorts_per_day": shorts, "long_per_day": base_long,
            "reason": f"growth score {growth} is soft; testing +1 short/day (capped at {max_shorts}/day) to see if more frequent posting helps",
        }
    if growth > 65:
        return {
            "date": today, "shorts_per_day": base_shorts, "long_per_day": base_long,
            "reason": f"growth score {growth} is healthy; holding the base quota rather than risking quality by overproducing",
        }
    return {
        "date": today, "shorts_per_day": base_shorts, "long_per_day": base_long,
        "reason": f"growth score {growth} is neutral; holding the base quota",
    }


def get_or_decide_daily_quota(channel_data: dict[str, Any], folder: Path) -> dict[str, Any]:
    """Cached per UTC day in daily_plan.json -- recomputes once a day, not
    once per hourly trigger-window check."""
    path = folder / "daily_plan.json"
    today = datetime.now(timezone.utc).date().isoformat()
    plan = load_json(path, {})
    if plan.get("date") == today:
        return plan
    plan = _decide_daily_quota(channel_data, folder)
    save_json(path, plan)
    return plan
