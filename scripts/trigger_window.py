from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from . import ab_test
from . import reoptimize
from .telegram_bot import poll, dispatch_channel
from .lib.config import load_config, load_json, all_channels
from .lib.state import read_state
from .analytics import today_upload_count
from .learning import get_or_decide_daily_quota


def _channel_needs_a_run(channel_data: dict, folder: Path) -> bool:
    # Uses the same possibly-adjusted quota as planner.decide_format() (see
    # learning.get_or_decide_daily_quota), so the trigger window and the
    # pipeline always agree on how many videos today actually needs.
    quota = get_or_decide_daily_quota(channel_data, folder)
    total_quota = int(quota.get("shorts_per_day", 1)) + int(quota.get("long_per_day", 1))
    done_today = today_upload_count(folder / "analytics_log.json")
    if done_today >= total_quota:
        return False
    state = read_state(folder)
    if state.get("status") == "running":
        return False  # a run is already in flight for this channel; let it finish
    return True


def auto_trigger() -> None:
    """Spec section 4: in auto mode, the hourly trigger window should decide
    on its own whether today's video is still needed per channel and, if so,
    dispatch video-pipeline.yml — instead of only ever reacting to a manual
    /run command. Deliberately independent of `require_approval` (see
    run_pipeline.py): triggering a run automatically and requiring a human
    to approve the result before it uploads are two separate decisions, not
    one combined toggle -- a channel can run fully hands-off while still
    always waiting for a Telegram approval before anything goes live.

    Wave batching: with many channels configured, dispatching every eligible
    one in the same hourly pass would spike both GitHub's concurrent-job
    count and every channel's LLM calls into the same minute at once. So at
    most `max_channels_per_wave` channels get dispatched per gate check
    (default 6); the rest simply stay eligible and get picked up in a later
    hourly pass -- since this cron runs every hour and _channel_needs_a_run
    keeps returning True until a channel actually completes today's run,
    the remaining channels naturally queue into the next wave(s) rather
    than needing separate scheduling logic. Channels that have gone longest
    without a run (or never ran today) are prioritized first each wave, so
    the same few channels never get pushed to the back every time."""
    schedule = load_json(Path(__file__).resolve().parents[1] / "data/schedule_config.json", {})
    if not schedule.get("auto_trigger", False):
        return
    config = load_config()
    max_per_wave = int(schedule.get("max_channels_per_wave", 6))

    candidates = [
        (channel, folder, read_state(folder).get("started_at") or "")
        for channel, channel_data, folder in all_channels(config)
        if _channel_needs_a_run(channel_data, folder)
    ]
    # Empty string (never run today) sorts before any ISO timestamp, so
    # never-run channels always go first; among those that have run, the
    # oldest run goes first.
    candidates.sort(key=lambda row: row[2])

    wave = candidates[:max_per_wave]
    if len(candidates) > max_per_wave:
        print(f"{len(candidates)} channels need a run this hour; dispatching "
              f"{len(wave)} now, {len(candidates) - len(wave)} deferred to a later wave.")

    for channel, _folder, _last in wave:
        result = dispatch_channel(channel)
        print(f"Auto-trigger for {channel}: {result}")


def main() -> None:
    schedule = load_json(Path(__file__).resolve().parents[1] / "data/schedule_config.json", {})
    now = datetime.now(timezone.utc)
    hours = {int(x) for x in schedule.get("enabled_hours_utc", [])}
    if now.hour in hours:
        # Each step isolated so one failing check doesn't take the other
        # two down with it -- poll() also isolates each individual command
        # for the same reason (see telegram_bot.poll()).
        try:
            poll(int(schedule.get("window_minutes", 10)))
        except Exception as exc:
            print(f"Telegram poll failed, continuing: {exc}")
        try:
            auto_trigger()
        except Exception as exc:
            print(f"Auto-trigger check failed, continuing: {exc}")
    else:
        print(f"Outside trigger window: UTC hour {now.hour}")
    # Runs every hour regardless of the enabled-hours gate above: thumbnail
    # A/B rotation (spec item #23) is unrelated to whether a new video
    # should be generated this hour, and this cron already fires hourly.
    try:
        ab_test.run_rotation()
    except Exception as exc:
        print(f"A/B test rotation check failed, continuing: {exc}")
    # Underperformer re-optimization (reoptimize.py): same "runs every hour
    # regardless of the enabled-hours gate" reasoning as ab_test above --
    # this only checks videos that just crossed the 30h-old mark, which has
    # nothing to do with whether a NEW video should be generated this hour.
    try:
        reoptimize.run_check()
    except Exception as exc:
        print(f"Re-optimization check failed, continuing: {exc}")


if __name__ == "__main__": main()
