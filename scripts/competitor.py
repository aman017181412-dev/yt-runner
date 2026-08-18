from __future__ import annotations
import os
from pathlib import Path
from .lib.config import save_json
from .youtube import search_competitors


def analyze(niche: str, output: Path) -> list[dict]:
    """Spec item #3: competitor analysis. Uses the same
    YOUTUBE_RESEARCH_API_KEY as research.py's YouTube source; if it's not
    configured this degrades to an empty list rather than failing the run."""
    key = os.getenv("YOUTUBE_RESEARCH_API_KEY")
    if not key:
        save_json(output, [])
        return []
    try:
        rows = search_competitors(niche, key, max_results=10)
    except Exception as exc:
        print(f"Competitor analysis unavailable: {exc}")
        rows = []
    save_json(output, rows)
    return rows
