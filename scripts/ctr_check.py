from __future__ import annotations
import base64
import os
from pathlib import Path
from typing import Any
import requests

# ---------------------------------------------------------------------------
# Cheap, optional pre-upload sanity check: ask a vision-capable LLM to look
# at each already-generated thumbnail candidate next to the actual title and
# score how likely it is to earn a click on a crowded feed/search page --
# a second, independent judgment before committing to whichever variant
# happened to render first (or whichever style historically won -- see
# learning.best_thumbnail_style()). This picks among EXISTING variants, it
# never generates a new one, so it costs no extra image-generation work.
#
# Vision support here is Gemini-only (Groq/OpenRouter's free-tier text
# models can't see images), so this silently no-ops without GEMINI_API_KEY
# rather than failing the pipeline over an optional check.
# ---------------------------------------------------------------------------


def _gemini_vision(image_path: Path, prompt: str) -> str | None:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    model = "gemini-1.5-flash"
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key},
        json={
            "contents": [{"role": "user", "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
            ]}],
            "generationConfig": {"temperature": 0.2},
        },
        timeout=60,
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def pick_best(thumb_variants: list[dict], title: str) -> tuple[list[dict], str | None]:
    """Re-order thumb_variants so the vision-LLM's pick is first -- the one
    run_pipeline.py uploads as the primary thumbnail. Returns the list
    unchanged (and None for the note) if GEMINI_API_KEY isn't set, fewer
    than 2 variants exist, or the check fails for any reason: the existing
    preferred_style ordering (learning.best_thumbnail_style) is a perfectly
    good fallback on its own."""
    variants = [v for v in thumb_variants if Path(v["path"]).exists()]
    if len(variants) < 2 or not os.getenv("GEMINI_API_KEY"):
        return thumb_variants, None
    try:
        scored: list[tuple[float, int]] = []
        for i, v in enumerate(variants):
            prompt = (
                f"This is a YouTube thumbnail candidate (style: {v['style']}) for a video titled "
                f"\"{title}\". On a scrolling feed next to competing videos, rate how likely this "
                f"thumbnail+title pair is to get clicked, from 1-10. Consider legibility at small size and "
                f"whether it creates curiosity without being misleading. Reply with ONLY the number."
            )
            reply = _gemini_vision(Path(v["path"]), prompt)
            try:
                score = float((reply or "0").strip().split()[0])
            except (ValueError, IndexError):
                score = 0.0
            scored.append((score, i))
        if not scored:
            return thumb_variants, None
        best_score, best_i = max(scored)
        if best_i == 0:
            return thumb_variants, f"kept default ({variants[0]['style']}, score {best_score:.1f})"
        reordered = [variants[best_i]] + [v for j, v in enumerate(variants) if j != best_i]
        return reordered, f"switched to {variants[best_i]['style']} (score {best_score:.1f} vs default {scored[0][0]:.1f})"
    except Exception as exc:
        print(f"CTR pre-check failed, keeping default thumbnail order: {exc}")
        return thumb_variants, None
