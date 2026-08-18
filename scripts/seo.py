from __future__ import annotations
from pathlib import Path
from .algo_strategy import context_block
from .llm import complete
from .lib.config import save_json
from .lib.llm_json import extract_json
from .lib.plugins import run_chained


def generate(topic: dict, script: str, output: Path) -> dict:
    is_short = topic.get("format") == "short"
    text = complete(
        "You are a YouTube SEO editor. Return valid JSON only, no markdown fences, no preamble, with keys: "
        "title, title_variants (array of exactly 2 alternative titles, each testing a different discovery "
        "strategy -- see notes below -- while staying truthful to the same video, for A/B rotation), "
        "description, tags (array), hashtags (array). Keep titles truthful and avoid clickbait that "
        "promises unsupported claims. The description must be substantial: 3-5 full sentences (roughly "
        "150-300 words) covering what the video explains, why it matters, and a soft call-to-action -- not "
        "a one-line summary. Include 8-15 relevant tags and 3-8 hashtags (each starting with #).\n\n"
        f"{context_block(shorts=is_short)}",
        f"Topic: {topic['title']}\nScript:\n{script}",
    )
    try:
        result = extract_json(text)
        if not isinstance(result, dict):
            raise ValueError("SEO response was not a JSON object")
    except Exception:
        result = {"title": topic["title"], "description": text[:5000], "tags": [], "hashtags": []}
    result.setdefault("title", topic["title"])
    result.setdefault("title_variants", [])
    result.setdefault("description", "")
    result.setdefault("tags", [])
    result.setdefault("hashtags", [])
    result["title_variants"] = [str(t).strip()[:100] for t in (result.get("title_variants") or []) if str(t).strip()][:2]

    # Defensive caps against YouTube Data API's hard validation limits.
    # Nothing upstream constrains the LLM's output length, and an oversized
    # title/tags list doesn't fail softly -- videos.insert() rejects the
    # whole upload outright, which would otherwise only surface as a
    # generic "Pipeline failed" notification with no obvious cause.
    result["title"] = str(result["title"])[:100]
    result["description"] = str(result["description"])[:5000]
    tags: list[str] = [str(t).strip() for t in (result.get("tags") or []) if str(t).strip()]
    capped_tags: list[str] = []
    total_len = 0
    for tag in tags:
        total_len += len(tag) + 1  # YouTube counts a comma/separator between tags
        if total_len > 460:  # stay safely under the ~500-char combined limit
            break
        capped_tags.append(tag)
    result["tags"] = capped_tags

    result = run_chained("post_process_seo", result, topic) or result
    save_json(output, result)
    return result
