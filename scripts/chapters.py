from __future__ import annotations
import json
import re
from pathlib import Path
from .llm import complete

# ---------------------------------------------------------------------------
# Chapters help both discovery paths at once: they surface as a mini table-
# of-contents in search results, and they raise session watch time (viewers
# jump to what they want instead of bouncing). YouTube's own hard
# requirements -- first chapter at 0:00, minimum 3 chapters, each at least
# 10 seconds apart -- are enforced here rather than trusted from the LLM,
# since violating any of them makes YouTube silently ignore the whole
# chapter list with no error surfaced anywhere.
# ---------------------------------------------------------------------------


def _parse_srt_times(srt_path: Path) -> list[tuple[float, str]]:
    """(seconds, caption_text) for every caption block -- gives the LLM real
    timestamps to anchor chapters to instead of inventing them."""
    if not srt_path or not srt_path.exists():
        return []
    text = srt_path.read_text(encoding="utf-8")
    rows: list[tuple[float, str]] = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+):(\d+):(\d+),\d+", lines[1])
        if not m:
            continue
        h, mnt, s = (int(x) for x in m.groups())
        rows.append((h * 3600 + mnt * 60 + s, " ".join(lines[2:])))
    return rows


def generate(script_text: str, srt_path: Path | None) -> list[dict]:
    """Returns [] (no chapters added) if there's no SRT to anchor real
    timestamps to, or if the LLM call/parse fails -- a missing chapter list
    is harmless, a fabricated one with wrong timestamps is worse than none."""
    caption_rows = _parse_srt_times(srt_path) if srt_path else []
    if not caption_rows:
        return []

    transcript_with_times = "\n".join(f"[{int(t)}s] {text}" for t, text in caption_rows)
    prompt = (
        "Given this timestamped transcript of a YouTube video, propose 3-6 chapter markers splitting it "
        "into its real topic segments. Return valid JSON only, no markdown fences, no preamble: a JSON "
        "array of objects with keys 'seconds' (integer, must be one of the timestamps shown) and 'label' "
        "(3-6 word chapter title, no numbering). The first chapter MUST be seconds=0.\n\n"
        + transcript_with_times[:6000]
    )
    try:
        text = complete("You are a YouTube editor writing chapter markers.", prompt, temperature=0.3)
        cleaned = text.strip().strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        start, end = cleaned.find("["), cleaned.rfind("]")
        raw = json.loads(cleaned[start:end + 1]) if start != -1 and end != -1 else []
    except Exception as exc:
        print(f"Chapter generation failed, continuing without chapters: {exc}")
        return []

    cleaned_chapters: list[dict] = []
    for ch in sorted((c for c in raw if isinstance(c, dict)), key=lambda c: c.get("seconds", 0)):
        try:
            seconds, label = int(ch["seconds"]), str(ch["label"]).strip()[:60]
        except (KeyError, TypeError, ValueError):
            continue
        if not label:
            continue
        if cleaned_chapters and seconds - cleaned_chapters[-1]["seconds"] < 10:
            continue  # YouTube requires >=10s between chapters
        cleaned_chapters.append({"seconds": seconds, "label": label})
    if cleaned_chapters and cleaned_chapters[0]["seconds"] != 0:
        cleaned_chapters = [{"seconds": 0, "label": "Intro"}] + cleaned_chapters
    return cleaned_chapters if len(cleaned_chapters) >= 3 else []


def format_for_description(chapters: list[dict]) -> str:
    if not chapters:
        return ""
    lines = [f"{c['seconds'] // 60:02d}:{c['seconds'] % 60:02d} {c['label']}" for c in chapters]
    return "\n\nChapters:\n" + "\n".join(lines)
