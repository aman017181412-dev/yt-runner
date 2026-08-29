"""Run daily by .github/workflows/token-renew-reminder.yml.

Two independent checks decide whether a channel needs a reminder:

  1. AGE CHECK (preventive): data/token_renewed.json says how long ago
     each channel was last renewed. >= RENEW_AFTER_DAYS old (or never
     renewed) -> reminder, so you renew *before* Google's 7-day
     Testing-mode cutoff.

  2. LIVE CHECK (reactive safety net): actually calls Google's token
     endpoint with each channel's current stored refresh_token (read
     from the CHANNELN_YT_TOKEN secret, injected as an env var by the
     workflow). If Google rejects it right now for ANY reason --
     invalid_grant (expired/revoked), invalid_scope (consent screen
     scope mismatch), whatever -- that channel is flagged urgently,
     even if it's "only" 1 day old. This is what would have caught
     the invalid_scope issue immediately instead of only surfacing it
     when the video pipeline itself failed.

Either check being true sends a tap-to-authorize Google OAuth link via
Telegram. The redirect target is the Vercel function (yt-token-renewer),
which does the actual token exchange + GitHub secret update once you
tap "Allow".

This script only ever *sends a link* and *tests a refresh* -- it never
completes authorization on its own. That tap is always yours.
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
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
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


def read_channel_token_env(channel_key: str) -> str | None:
    """Same three-tier lookup as yt-core's channel_yt_token(): a direct
    CHANNELN_YT_TOKEN secret first, then YT_CHANNEL_TOKENS_JSON, then
    ALL_SECRETS_JSON (`${{ toJSON(secrets) }}`, passed once from the
    workflow YAML) -- so a channel's token is found here the moment
    oauth-callback.js creates its secret, with no YAML edit needed."""
    env_key = f"{channel_key.upper()}_YT_TOKEN"
    direct = os.environ.get(env_key)
    if direct:
        return direct
    bundle_raw = os.environ.get("YT_CHANNEL_TOKENS_JSON")
    if bundle_raw:
        try:
            bundle = json.loads(bundle_raw)
        except json.JSONDecodeError:
            bundle = {}
        value = bundle.get(channel_key)
        if value:
            return value if isinstance(value, str) else json.dumps(value)
    all_secrets_raw = os.environ.get("ALL_SECRETS_JSON")
    if all_secrets_raw:
        try:
            all_secrets = json.loads(all_secrets_raw)
        except json.JSONDecodeError:
            all_secrets = {}
        value = all_secrets.get(env_key)
        if value:
            return value
    return None


def check_token_live(channel_key: str) -> tuple[bool, str]:
    """Actually asks Google whether this channel's CURRENT stored token
    still works. Returns (is_valid, reason). reason is "" when valid,
    or Google's error code/description when not.

    Looks up the token via read_channel_token_env() (see there for the
    three sources checked, in order). If none of them have it, treated
    as "unknown, skip live check" -- the age check will still catch it
    since it'll never have a token_renewed.json entry either.
    """
    raw = read_channel_token_env(channel_key)
    if not raw:
        return True, ""  # nothing to test; age check handles "never authorized"

    try:
        token = json.loads(raw)
        refresh_token = token["refresh_token"]
        client_id = token["client_id"]
        client_secret = token["client_secret"]
    except (json.JSONDecodeError, KeyError) as e:
        return False, f"stored token JSON malformed ({e})"

    try:
        resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
    except requests.RequestException as e:
        return True, f"network error checking token, skipping ({e})"  # don't false-alarm on a flaky network

    if resp.ok:
        return True, ""
    try:
        err = resp.json()
        reason = f"{err.get('error')}: {err.get('error_description', '')}"
    except ValueError:
        reason = f"HTTP {resp.status_code}"
    return False, reason


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

    due = []  # list of (channel, reason) -- reason is "" for a normal age-based reminder
    for ch in config.get("channels", []):
        key = ch["key"]

        # Check 1: age
        last = renewed.get(key)
        if last:
            age = now - datetime.fromisoformat(last.replace("Z", "+00:00"))
        else:
            age = None  # never renewed via this system
        age_due = age is None or age >= timedelta(days=RENEW_AFTER_DAYS)

        # Check 2: live refresh test
        is_valid, live_reason = check_token_live(key)

        if not is_valid:
            due.append((ch, live_reason))
        elif age_due:
            due.append((ch, ""))

    if not due:
        print("No channels due for renewal today.")
        return 0

    for ch, reason in due:
        gmail_hint = ch.get("gmail_hint", "")
        yt_name = ch.get("youtube_channel_name", "")
        client_id = resolve_client_id(ch, config)
        url = build_auth_url(ch["key"], client_id, gmail_hint)
        label = ch.get("label", ch["key"])

        if reason:
            header = f"ЁЯЪи \"{label}\" ржПрж░ token ржПржЦржиржЗ ржХрж╛ржЬ ржХрж░ржЫрзЗ ржирж╛ ({reason})ред ржПржЦржиржЗ renew ржХрж░рзБржиред"
        else:
            header = f"ЁЯФФ \"{label}\" ржПрж░ YouTube token renew ржХрж░рж╛рж░ рж╕ржоржпрж╝ рж╣ржпрж╝рзЗржЫрзЗред"

        lines = [header, ""]
        if gmail_hint:
            lines.append(f"ЁЯУз Gmail: {gmail_hint}")
        if yt_name:
            lines.append(f"ЁЯУ║ Consent рж╕рзНржХрзНрж░рж┐ржирзЗ channel picker ржЖрж╕рж▓рзЗ ржмрзЗржЫрзЗ ржирж┐ржи: \"{yt_name}\"")
        lines += [
            "",
            f"рж▓рж┐ржВржХ (ржЯрзНржпрж╛ржк ржХрж░рзБржи):\n{url}",
            "",
            "Allow ржжрзЗржУржпрж╝рж╛рж░ ржкрж░ ржПржХржЯрж╛ тЬЕ ржХржиржлрж╛рж░рзНржорзЗрж╢ржи ржорзЗрж╕рзЗржЬ ржкрж╛ржмрзЗржиред",
        ]
        send_telegram("\n".join(lines))
        print(f"Reminder sent for {ch['key']}" + (f" (live check failed: {reason})" if reason else ""))

    return 0


if __name__ == "__main__":
    sys.exit(main())
