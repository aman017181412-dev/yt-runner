from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from .analytics import fetch_early_retention
from .lib.config import load_json, save_json
from .llm import complete

# ---------------------------------------------------------------------------
# What the user asked for: the hook (opening lines) should keep getting
# stronger by looking at which of this channel's own hooks actually kept
# viewers watching. This is real, data-grounded self-improvement -- not the
# LLM "getting smarter" on its own, but a weekly pass that compares this
# channel's best- and worst-early-retention hooks and turns the pattern into
# a concrete instruction, which script_writer.py then includes in every
# script prompt going forward.
# ---------------------------------------------------------------------------

_MIN_HOOK_CHARS = 400  # enough to capture the opening beat without the whole script


def _extract_hook(script_text: str) -> str:
    """The hook is the first few lines actually spoken -- strip bracketed
    visual directions like [cut to...] since those aren't narration."""
    import re
    clean = re.sub(r"\[[^\]]*\]", "", script_text).strip()
    return clean[:_MIN_HOOK_CHARS].strip()


def record_hook(folder: Path, video_id: str, script_text: str) -> None:
    path = folder / "hook_performance.json"
    rows = load_json(path, [])
    rows.append({
        "video_id": video_id, "hook": _extract_hook(script_text),
        "recorded_at": datetime.now(timezone.utc).isoformat(), "early_retention": None,
    })
    save_json(path, rows[-300:])


def refresh_retention(folder: Path, channel: str) -> int:
    """Backfill early_retention for hooks recorded before YouTube had
    accumulated enough views to report a retention curve. Call this
    periodically (weekly is enough -- retention data needs real viewers,
    which takes days, not minutes)."""
    path = folder / "hook_performance.json"
    rows = load_json(path, [])
    updated = 0
    for row in rows:
        if row.get("early_retention") is None and row.get("video_id"):
            score = fetch_early_retention(row["video_id"], channel)
            if score is not None:
                row["early_retention"] = score
                updated += 1
    if updated:
        save_json(path, rows)
    return updated


def update_guidance(folder: Path, min_rows: int = 6) -> str:
    """Compare this channel's best- and worst-early-retention hooks and ask
    the LLM to name the concrete pattern, saved for script_writer.py to
    quote directly in its prompt. Requires at least `min_rows` hooks with
    retention data -- there is no guidance (and no fabricated placeholder)
    before that."""
    rows = [r for r in load_json(folder / "hook_performance.json", []) if r.get("early_retention") is not None]
    if len(rows) < min_rows:
        return ""
    ranked = sorted(rows, key=lambda r: r["early_retention"], reverse=True)
    # Guards against top/bottom overlapping if called with a smaller
    # min_rows than the default (currently always 6, but this stays correct
    # if that ever changes) -- with e.g. 4 rows, ranked[:3] and ranked[-3:]
    # would otherwise share an index and double-count a hook as both.
    k = min(3, len(ranked) // 2)
    top, bottom = ranked[:k], ranked[-k:]
    prompt = (
        "Hooks (opening lines) from this channel's best early-retention videos:\n"
        + "\n".join(f"- \"{r['hook']}\" (retention {r['early_retention']:.2f})" for r in top)
        + "\n\nHooks from the worst-performing:\n"
        + "\n".join(f"- \"{r['hook']}\" (retention {r['early_retention']:.2f})" for r in bottom)
        + "\n\nIn 2-3 sentences, name the concrete, specific pattern that separates the best from the worst "
          "(e.g. 'opens with a specific number or stat', 'asks a direct question in the first line', "
          "'states the payoff before the setup'). Write it as a direct instruction for writing the next hook. "
          "Avoid generic advice like 'be engaging'."
    )
    try:
        guidance = complete("You are a YouTube retention analyst who gives specific, actionable notes.", prompt, temperature=0.3)
    except Exception as exc:
        print(f"Hook guidance generation failed, keeping previous guidance if any: {exc}")
        return load_json(folder / "hook_guidance.json", {}).get("guidance", "")
    save_json(folder / "hook_guidance.json", {
        "guidance": guidance, "generated_at": datetime.now(timezone.utc).isoformat(), "based_on_hooks": len(rows),
    })
    return guidance


def current_guidance(folder: Path) -> str:
    return load_json(folder / "hook_guidance.json", {}).get("guidance", "")
