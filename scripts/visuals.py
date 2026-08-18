from __future__ import annotations
from .lib.llm_json import extract_json
from .llm import complete

# ---------------------------------------------------------------------------
# What caused the "whole video is one barely-related clip" bug: footage.py
# used to search Pexels/Pixabay with the raw topic title (often a full news
# headline) as one literal query. Stock libraries are indexed by concrete,
# real-world subjects (a laptop, a city street, hands typing) -- an abstract
# headline like "YouTube Mistakenly Penalizes Popular Science Channel..."
# matches almost nothing on-topic, so the search would return very few
# results, sometimes just one loosely-matching template clip stretched
# across the entire runtime. This turns the topic into several short,
# concrete search phrases instead.
# ---------------------------------------------------------------------------


def suggest_queries(topic: dict, script_text: str, min_queries: int = 4) -> list[str]:
    prompt = (
        f"Topic: {topic['title']}\nNiche: {topic['niche']}\n\nScript excerpt:\n{script_text[:1500]}\n\n"
        f"List {min_queries}-6 short (2-4 word), concrete, literal stock-video search phrases that would find "
        "relevant B-roll footage for this video (e.g. 'laptop typing close up', 'city skyline sunset', "
        "'person reading phone'). Avoid abstract concepts, brand names, and full sentences. "
        'Return JSON only, no markdown fences: {"queries": ["...", ...]}'
    )
    try:
        text = complete("You are a video producer picking stock footage search terms.", prompt, temperature=0.5)
        data = extract_json(text)
        queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
        if queries:
            return queries[:6]
    except Exception as exc:
        print(f"Visual query suggestion failed, falling back to the topic title alone: {exc}")
    return [topic["title"]]
