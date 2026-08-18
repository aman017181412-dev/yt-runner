from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"
KNOWLEDGE_DIR = ROOT / "knowledge"
PLUGINS_DIR = ROOT / "plugins"


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.exists() else ROOT / "config.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


def channel_config(channel: str | None = None) -> tuple[str, dict[str, Any], dict[str, Any]]:
    config = load_config()
    key = channel or os.getenv("CHANNEL") or config.get("default_channel", "channel1")
    channels = config.get("channels", {})
    if key not in channels:
        raise ValueError(f"Unknown channel '{key}'. Add it under config.json -> channels.")
    return key, config, channels[key]


def channel_yt_token(channel: str) -> str | None:
    """Look up a channel's YouTube OAuth token JSON (as a string).

    Two ways to provide it, checked in order:
    1. A dedicated secret named e.g. CHANNEL3_YT_TOKEN -- fine for a
       handful of channels, but means editing both the GitHub Secret list
       AND both workflow YAML files' `env:` block every time a channel is
       added, since GitHub Actions never exposes a secret that isn't
       explicitly mapped.
    2. YT_CHANNEL_TOKENS_JSON -- one secret holding a JSON object of
       {"channel_key": "<token json as a string>", ...} for every channel
       at once. This scales to 10-20 channels without touching the
       workflow YAML again: add a channel, add one key to this JSON blob,
       done. The token value inside can be either a JSON string or an
       already-parsed object; both are normalized back to a string here
       since build_credentials() expects to json.loads() it itself.
    Option 1 wins if both are present for the same channel, so an existing
    single/double-channel setup keeps working unchanged after adopting
    option 2 for the rest.
    """
    direct = os.getenv(f"{channel.upper()}_YT_TOKEN")
    if direct:
        return direct
    bundle_raw = os.getenv("YT_CHANNEL_TOKENS_JSON")
    if not bundle_raw:
        return None
    try:
        bundle = json.loads(bundle_raw)
    except json.JSONDecodeError:
        return None
    value = bundle.get(channel)
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def niche_dir(channel: str, channel_data: dict[str, Any]) -> Path:
    # Any character that isn't alphanumeric becomes a hyphen -- not just
    # spaces -- so a niche like "ai/tech education" or "sci-fi & fantasy"
    # can't accidentally create nested folders (a stray "/") or otherwise
    # break the one-flat-folder-per-niche layout the rest of the code
    # assumes (analytics_log.json, decision_log.json, etc. all live directly
    # in this folder).
    raw = str(channel_data.get("niche", channel)).strip().lower()
    safe = re.sub(r"[^a-z0-9]+", "-", raw).strip("-") or "default"
    path = DATA / channel / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def all_channels(config: dict[str, Any] | None = None) -> list[tuple[str, dict[str, Any], Path]]:
    """Return (channel_key, channel_data, niche_dir) for every configured channel."""
    config = config or load_config()
    rows = []
    for key, channel_data in config.get("channels", {}).items():
        rows.append((key, channel_data, niche_dir(key, channel_data)))
    return rows


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
