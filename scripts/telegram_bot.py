from __future__ import annotations
import os
import time
from pathlib import Path
from typing import Any
import requests
from .lib.config import load_config, load_json, save_json, all_channels, DATA, CONFIG_PATH
from .lib.state import read_state


def _api(method: str, **kwargs: Any) -> dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: return {}
    return requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=35, **kwargs).json()


def _chat_id() -> str | None:
    value = os.getenv("TELEGRAM_CHAT_ID")
    return value.strip() if value else None


def _is_authorized(sender_chat_id: str) -> bool:
    """Only the one configured TELEGRAM_CHAT_ID may issue control commands.
    Without this check, anyone who finds the bot's @username on Telegram
    could send /run, /autotrigger, /approval, or /change_niche and control the pipeline."""
    owner = _chat_id()
    return bool(owner) and sender_chat_id == owner


_TELEGRAM_BOT_UPLOAD_LIMIT_MB = 50  # Telegram Bot API's hard limit for a multipart file upload


def send_video(video: Path, caption: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = _chat_id()
    if not token or not chat_id:
        # Loud on purpose: this used to fail silently (just `return False`),
        # which from run_pipeline.py looked identical to every other
        # failure mode and gave no hint in the Actions log about WHICH of
        # token/chat_id/file was actually the problem.
        print(f"Telegram preview skipped: TELEGRAM_BOT_TOKEN set={bool(token)}, TELEGRAM_CHAT_ID set={bool(chat_id)}.")
        return False
    if not video.exists():
        print(f"Telegram preview skipped: {video} does not exist.")
        return False
    size_mb = video.stat().st_size / (1024 * 1024)
    if size_mb > _TELEGRAM_BOT_UPLOAD_LIMIT_MB:
        # Fails fast with a clear reason instead of a generic
        # timeout/connection error that looks like something else went
        # wrong -- a common real cause of "no preview ever arrives".
        print(f"Telegram preview skipped: video is {size_mb:.1f}MB, over Telegram's {_TELEGRAM_BOT_UPLOAD_LIMIT_MB}MB bot upload limit.")
        return False
    try:
        with video.open("rb") as handle:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendVideo",
                data={"chat_id": chat_id, "caption": caption},
                files={"video": (video.name, handle, "video/mp4")},
                timeout=120,
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            print(f"Telegram preview upload rejected by the API: {result}")
        return bool(result.get("ok"))
    except requests.RequestException as exc:
        print(f"Telegram preview upload failed: {exc}")
        return False


def send_photo(photo: Path, caption: str) -> bool:
    """Used by youtube.py when a custom thumbnail can't be attached to the
    video (usually: the channel isn't phone-verified yet) -- the generated
    image still reaches the user this way so they have it on hand to add
    manually, instead of it just being discarded."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = _chat_id()
    if not token or not chat_id:
        print(f"Telegram thumbnail fallback skipped: TELEGRAM_BOT_TOKEN set={bool(token)}, TELEGRAM_CHAT_ID set={bool(chat_id)}.")
        return False
    if not photo.exists():
        print(f"Telegram thumbnail fallback skipped: {photo} does not exist.")
        return False
    try:
        with photo.open("rb") as handle:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": (photo.name, handle, "image/jpeg")},
                timeout=60,
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            print(f"Telegram thumbnail upload rejected by the API: {result}")
        return bool(result.get("ok"))
    except requests.RequestException as exc:
        print(f"Telegram thumbnail upload failed: {exc}")
        return False


def wait_for_decision(timeout_seconds: int = 300) -> str:
    """Wait for an approval command from the configured Telegram chat."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = _chat_id()
    if not token or not chat_id:
        return "timeout"

    start = time.time()
    offset = None
    while time.time() - start < timeout_seconds:
        result = _api("getUpdates", params={"timeout": 20, "offset": offset})
        for update in result.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message", {})
            sender_chat = str(message.get("chat", {}).get("id", ""))
            command = str(message.get("text", "")).strip().lower()
            if sender_chat != chat_id:
                continue
            if command == "/approve":
                _api("sendMessage", json={"chat_id": chat_id, "text": "Approval received. Uploading the video."})
                return "approve"
            if command == "/reject":
                _api("sendMessage", json={"chat_id": chat_id, "text": "Rejection received. The video will not be uploaded."})
                return "reject"
    return "timeout"


def dispatch_channel(channel: str) -> str:
    # Guards the manual /run path the same way trigger_window.py's
    # auto_trigger() already guards itself -- without this, sending /run
    # while a run is already in progress (e.g. during its 5-minute approval
    # wait) would start a second concurrent pipeline for the same channel,
    # both reading the same "0 done today" state and both potentially
    # choosing the same topic/format before either commits its results.
    config = load_config()
    channel_data = (config.get("channels") or {}).get(channel)
    if channel_data is None:
        return f"Unknown channel '{channel}'. Configured channels: {', '.join(config.get('channels', {}))}"
    from .lib.config import niche_dir
    folder = niche_dir(channel, channel_data)
    state = read_state(folder)
    if state.get("status") == "running":
        return f"A run is already in progress for {channel} (step: {state.get('current_step', '?')}). Not starting another."

    token = os.getenv("GH_TOKEN")
    repo = os.getenv("PUBLIC_REPO")
    if not token or not repo: return "GitHub dispatch is not available in this run."
    response = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/video-pipeline.yml/dispatches",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"ref": "main", "inputs": {"channel": channel}}, timeout=30,
    )
    if response.status_code not in (200, 201, 204): return f"Dispatch failed: {response.status_code} {response.text[:200]}"
    return f"Pipeline dispatched for {channel}."


def _status_report() -> str:
    config = load_config()
    rows = all_channels(config)
    if not rows:
        return "No channels configured."
    lines = []
    for channel, channel_data, folder in rows:
        state = read_state(folder)
        status = state.get("status", "idle")
        step = state.get("current_step", "-")
        topic = (state.get("topic") or {}).get("title", "-")
        updated = state.get("updated_at", "-")
        lines.append(f"{channel} [{channel_data.get('niche', '-')}] — {status} (step: {step})\n  topic: {topic}\n  updated: {updated}")
    schedule = load_json(DATA / "schedule_config.json", {})
    lines.append(
        f"\nauto_trigger: {'on' if schedule.get('auto_trigger') else 'off'} "
        f"| require_approval: {'on' if schedule.get('require_approval', True) else 'off'} "
        f"| enabled_hours_utc: {schedule.get('enabled_hours_utc', [])}"
    )
    return "\n\n".join(lines)


def handle(text: str, chat_id: str) -> str:
    if not _is_authorized(chat_id):
        return ""  # silently ignore commands from anyone but the configured owner chat
    parts = text.strip().split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if command == "/run":
        return dispatch_channel(arg or load_config().get("default_channel", "channel1"))
    if command == "/autotrigger" and arg in ("on", "off"):
        path = DATA / "schedule_config.json"
        data = load_json(path, {})
        data["auto_trigger"] = arg == "on"
        save_json(path, data)
        return f"Auto-trigger turned {arg}. " + (
            "The hourly trigger window will now start runs on its own once each channel's daily quota isn't met yet."
            if arg == "on" else
            "Runs will only start when you send /run."
        )
    if command == "/approval" and arg in ("on", "off"):
        path = DATA / "schedule_config.json"
        data = load_json(path, {})
        data["require_approval"] = arg == "on"
        save_json(path, data)
        return f"Approval requirement turned {arg}. " + (
            "Every run will send a preview and wait for /approve or /reject before uploading."
            if arg == "on" else
            "Runs will upload automatically with no preview step -- use with caution."
        )
    if command == "/change_niche" and arg:
        config = load_config()
        channels = config.get("channels", {})
        if not channels:
            return "No channels configured."
        # For a multi-channel setup, an explicit "/change_niche channel2 new
        # niche text" targets that channel; otherwise (and for the common
        # single-channel case) the whole argument is the niche, applied to
        # default_channel -- this used to always silently target
        # default_channel even when the first word matched another
        # configured channel, so a multi-channel setup had no way to
        # /change_niche on a non-default channel at all.
        first_word, _, rest = arg.partition(" ")
        if first_word in channels and rest.strip():
            channel, new_niche = first_word, rest.strip()
        else:
            channel, new_niche = config.get("default_channel", "channel1"), arg
        if channel not in channels:
            return f"Unknown channel '{channel}'. Configured channels: {', '.join(channels)}"
        channels[channel]["niche"] = new_niche
        save_json(CONFIG_PATH, config)
        return f"Niche changed to '{new_niche}' for {channel}."
    if command == "/set_schedule" and arg:
        try:
            hours = [int(x.strip()) for x in arg.split(",")]
        except ValueError:
            return "Use comma-separated UTC hours, for example /set_schedule 0,6,12,18"
        path = DATA / "schedule_config.json"
        data = load_json(path, {})
        data["enabled_hours_utc"] = hours
        save_json(path, data)
        return "Schedule updated."
    if command == "/status":
        return _status_report()
    if command in ("/approve", "/reject"):
        return f"{command[1:].title()} received. (This only takes effect while a pipeline run is actively waiting for approval.)"
    return "Commands: /run <channel>, /autotrigger on|off, /approval on|off, /change_niche <name>, /set_schedule <hours>, /status"


def poll(minutes: int = 10) -> None:
    if not os.getenv("TELEGRAM_BOT_TOKEN"): return
    start = time.time(); offset = None
    while time.time() - start < minutes * 60:
        result = _api("getUpdates", params={"timeout": 20, "offset": offset})
        for update in result.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message", {}); text = message.get("text", ""); chat_id = str(message.get("chat", {}).get("id", ""))
            if not text or not chat_id:
                continue
            if not _is_authorized(chat_id):
                print(f"Ignoring command from unauthorized chat {chat_id}")
                continue
            try:
                reply = handle(text, chat_id)
            except Exception as exc:
                # A bug in one command must not crash the whole hourly
                # trigger-window job -- that would silently take down
                # auto_trigger() and ab_test.run_rotation() for the rest of
                # this run too, since they execute after poll() returns.
                print(f"Command '{text}' raised an error, ignoring and continuing to poll: {exc}")
                reply = f"That command hit an internal error: {exc}"
            if reply:
                _api("sendMessage", json={"chat_id": chat_id, "text": reply})
