"""Run daily by .github/workflows/token-renew-reminder.yml.

For every channel listed in data/channel_auth_config.json, checks
data/token_renewed.json for when it was last renewed. If it's due
(>= RENEW_AFTER_DAYS old, or never renewed), sends a Telegram message
with a tap-to-authorize Google OAuth link. The redirect target is the
Vercel function (yt-token-renewer), which does the actual token
exchange + GitHub secret update once you tap "Allow".

This script only ever *sends a link* -- it never touches your Google
account itself. Nothing here can complete authorization on its own;
that tap is always yours.
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

RENEW_AFTER_DAYS = 6  # send the reminder a day before Google's 7-day cutoff
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    ).raise_for_status()


def resolve_client_id(ch: dict, config: dict) -> str:
    """Client_id for this channel's project (or 'default' if unset).
    client_id is not sensitive -- it's fine to keep in this repo file.
    client_secret lives only in Vercel's CLIENT_SECRETS_JSON env var,
    keyed by the same project name."""
    project = ch.get("project", "default")
    return config.get("projects", {}).get(project, "")


def build_auth_url(channel_key: str, client_id: str, gmail_hint: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": os.environ["REDIRECT_URI"],  # https://<app>.vercel.app/api/oauth-callback
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": " ".join(SCOPES),
        "state": channel_key,
    }
    if gmail_hint:
        # Pre-fills (doesn't force) the Google login screen with this email --
        # still doesn't pick the *channel* if that Gmail has more than one.
        params["login_hint"] = gmail_hint
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def main() -> int:
    config = load_json(ROOT / "data" / "channel_auth_config.json", {"channels": []})
    renewed = load_json(ROOT / "data" / "token_renewed.json", {})
    now = datetime.now(timezone.utc)

    due = []
    for ch in config.get("channels", []):
        key = ch["key"]
        last = renewed.get(key)
        if last:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            age = now - last_dt
        else:
            age = None  # never renewed via this system
        if age is None or age >= timedelta(days=RENEW_AFTER_DAYS):
            due.append(ch)

    if not due:
        print("No channels due for renewal today.")
        return 0

    for ch in due:
        gmail_hint = ch.get("gmail_hint", "")
        yt_name = ch.get("youtube_channel_name", "")
        client_id = resolve_client_id(ch, config)
        url = build_auth_url(ch["key"], client_id, gmail_hint)
        label = ch.get("label", ch["key"])

        lines = [f"🔔 \"{label}\" এর YouTube token renew করার সময় হয়েছে।", ""]
        if gmail_hint:
            lines.append(f"📧 Gmail: {gmail_hint}")
        if yt_name:
            lines.append(f"📺 Consent স্ক্রিনে channel picker আসলে বেছে নিন: \"{yt_name}\"")
        lines += [
            "",
            f"লিংক (ট্যাপ করুন):\n{url}",
            "",
            "Allow দেওয়ার পর একটা ✅ কনফার্মেশন মেসেজ পাবেন।",
        ]
        send_telegram("\n".join(lines))
        print(f"Reminder sent for {ch['key']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
