from __future__ import annotations
import os
import requests

_TELEGRAM_MAX_CHARS = 4096  # Telegram's sendMessage hard limit; over this, the API just rejects the whole message


def send(message: str, chat_id: str | None = None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    target = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not target:
        return
    if len(message) > _TELEGRAM_MAX_CHARS:
        # Some callers embed a full API error/traceback (e.g. the
        # thumbnail-attach-failure notice in youtube.py) which can run long
        # -- better to deliver a truncated notification than have the
        # Telegram API reject the whole thing and the user get nothing.
        message = message[: _TELEGRAM_MAX_CHARS - 20] + "\n...[truncated]"
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": target, "text": message}, timeout=20).raise_for_status()
    except requests.RequestException as exc:
        print(f"Telegram notification failed: {exc}")
