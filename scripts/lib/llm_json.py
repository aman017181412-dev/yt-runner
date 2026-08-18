from __future__ import annotations
import json


def extract_json(text: str) -> dict:
    """LLMs frequently wrap JSON in ```json fences or add stray preamble
    text despite instructions not to. Strip that before parsing instead of
    letting every JSON-expecting LLM call silently fall back to empty
    output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)
