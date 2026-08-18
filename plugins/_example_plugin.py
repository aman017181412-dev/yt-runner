"""Example plugin — disabled by default (leading underscore in the
filename). Rename to example_plugin.py to activate it. See README.md in
this folder for the full hook list.
"""
from __future__ import annotations


def extra_research(niche: str) -> list[dict]:
    # Example: pull one hand-picked evergreen topic into the candidate pool
    # for a specific niche, so it can still be chosen by planner.choose_topic
    # if nothing fresher is unused.
    if niche.lower() != "technology explainers":
        return []
    return [{"title": f"{niche}: beginner's glossary", "url": "", "source": "plugin"}]


def post_process_script(script_data: dict, topic: dict) -> dict:
    # Example: append a fixed sign-off line to every script from this niche.
    return script_data


def post_process_seo(seo_metadata: dict, topic: dict) -> dict:
    # Example: force a hashtag onto every video.
    tags = seo_metadata.get("hashtags", [])
    if "#shorts" not in tags and topic.get("format") == "short":
        seo_metadata["hashtags"] = tags + ["#shorts"]
    return seo_metadata


def before_upload(video_path: str, metadata: dict, channel: str) -> None:
    print(f"[example_plugin] about to upload {video_path} for {channel}: {metadata.get('title')}")
