from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .config import load_json, save_json
from .notify import send as _notify

# Spec item #18: a Telegram notification after every pipeline step. Firing on
# "running" as well as "complete" would double the message count for no real
# benefit, so only these terminal-for-the-step statuses notify.
_NOTIFY_STATUSES = {"complete", "skipped", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineState:
    def __init__(self, path: Path, channel: str):
        self.path = path
        self.data: dict[str, Any] = load_json(path, {"channel": channel, "status": "idle", "steps": {}})
        self.data["channel"] = channel
        self.data["updated_at"] = utc_now()

    def start(self) -> None:
        self.data.update({"status": "running", "started_at": utc_now(), "error": None})
        self.save()

    def step(self, name: str, status: str, notify: bool = True, **extra: Any) -> None:
        self.data.setdefault("steps", {})[name] = {"status": status, "updated_at": utc_now(), **extra}
        self.data["current_step"] = name
        self.data["updated_at"] = utc_now()
        self.save()
        if notify and status in _NOTIFY_STATUSES:
            channel = self.data.get("channel", "")
            detail = extra.get("note") or extra.get("reason") or extra.get("topic") or ""
            message = f"[{channel}] {name}: {status}" + (f" — {detail}" if detail else "")
            try:
                _notify(message)
            except Exception as exc:  # a notification failure must never break the pipeline
                print(f"Step notification failed: {exc}")

    def finish(self, status: str = "complete", **extra: Any) -> None:
        self.data.update({"status": status, "finished_at": utc_now(), **extra})
        self.save()

    def save(self) -> None:
        save_json(self.path, self.data)


def read_state(folder: Path) -> dict[str, Any]:
    return load_json(folder / "state.json", {"status": "idle", "steps": {}})


def resumable_steps(folder: Path, topic_hash: str) -> set[str]:
    """Steps that can be skipped on retry: the previous run failed midway on
    the SAME topic, and the JSON artifact for that step is already committed
    to the repo (research/script/seo are persisted; generated media is not,
    since .gitignore excludes it — so voice/footage/edit/thumbnail/upload
    always re-run)."""
    state = read_state(folder)
    if state.get("status") != "failed":
        return set()
    prior_topic = (state.get("topic") or {}).get("topic_hash")
    if prior_topic != topic_hash:
        return set()
    steps = state.get("steps", {})
    done = {name for name, info in steps.items() if info.get("status") == "complete"}
    resumable = {"research", "planning"} & done
    if "script" in done and (folder / "script_latest.json").exists():
        resumable.add("script")
    if "seo" in done and (folder / "seo_latest.json").exists():
        resumable.add("seo")
    return resumable
