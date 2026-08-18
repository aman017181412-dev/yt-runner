from __future__ import annotations
import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

from . import ab_test
from . import audience
from . import chapters
from . import crosspost
from . import ctr_check
from . import duration_strategy
from . import hooks
from . import retention
from . import visuals
from .analytics import check_quota_alert, log_api_call, record, refresh_metrics
from .calendar_planner import generate as generate_calendar
from .competitor import analyze as analyze_competitors
from .edit import probe_duration, render
from .footage import collect, fetch_music
from .learning import best_thumbnail_style, compute_health_score, get_or_decide_daily_quota, growth_report, suggest_upload_hours, update_learning_db
from .lib.config import DATA, channel_config, load_json, niche_dir, save_json
from .lib.notify import send as notify
from .lib.plugins import run_side_effect
from .lib.state import PipelineState
from .planner import choose_topic, decide_format, log_decision
from .research import collect as research
from .script_writer import fact_check, write_script
from .seo import generate as generate_seo
from .subtitles import generate_srt
from .telegram_bot import send_video, wait_for_decision
from .thumbnail import make as make_thumbnails
from .tutorial_capture import capture as capture_tutorial
from .voice import generate as voice_generate
from .youtube import add_to_playlist, ensure_playlist, post_comment, upload

COST_LIMITS = {"llm": 60, "research": 40, "footage": 40}  # soft daily alert thresholds, tune per free-tier quotas


def _find_resumable_topic(folder: Path, prev_state: dict) -> dict | None:
    """Spec #28 Error Recovery: if the previous run for this channel failed
    partway through and its research/script/SEO JSON artifacts are still on
    disk (they're committed to git, unlike generated media), reuse the same
    topic and those artifacts instead of burning fresh LLM calls."""
    topic = prev_state.get("topic")
    if prev_state.get("status") != "failed" or not topic or not topic.get("topic_hash"):
        return None
    script_data = load_json(folder / "script_latest.json", {})
    if script_data.get("topic", {}).get("topic_hash") == topic["topic_hash"] and script_data.get("script"):
        return topic
    return None


def _strip_visual_directions(script_text: str) -> str:
    """script_writer.py's prompt explicitly asks for bracketed visual
    directions like "[cut to a laptop screen]" mixed into the narration --
    useful context for a human editor, but NOT meant to be spoken. Without
    this, voice_generate() below was previously handed the raw script text
    verbatim, so the TTS engine read stage directions out loud, padding out
    the narration (and therefore the final video's duration) with sentences
    nobody wrote to be heard. hooks.py's _extract_hook() already strips the
    same pattern for its own purposes; this is that same fix applied to the
    actual narration audio, not just hook analysis."""
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", " ", script_text)).strip()


def _cap_words(text: str, max_words: int) -> str:
    """Duration guardrail for Shorts: script_writer.py's word-count
    instruction ("~100-150 words") is only a prompt suggestion the LLM
    doesn't reliably follow, and an oversized "short" script defeats the
    format (a 3-minute narration no longer reads as a snappy Short even
    though YouTube's own 2024 rule change still technically classifies any
    vertical video under 3 minutes as one). Cuts at the last full sentence
    within the cap rather than trimming the finished audio afterward, which
    risks an abrupt mid-word cutoff instead of a clean ending."""
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = " ".join(words[:max_words])
    for sep in (". ", "! ", "? "):
        idx = truncated.rfind(sep)
        if idx > len(truncated) * 0.5:  # don't throw away more than half the capped text chasing a boundary
            return truncated[: idx + 1].strip()
    return truncated.strip() + "."


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # This setup happens before there's a folder/state to write a "failed"
    # status into, so it gets its own try/except purely to guarantee a
    # Telegram notification -- without this, a bad --channel value (e.g. a
    # typo in the workflow_dispatch input) previously crashed with an
    # uncaught ValueError and the user got no notification at all, only a
    # raw traceback buried in the Actions log.
    try:
        channel, config, channel_data = channel_config(args.channel)
        folder = niche_dir(channel, channel_data)
        state = PipelineState(folder / "state.json", channel)
    except Exception as exc:
        notify(f"Pipeline could not start for --channel={args.channel!r}: {exc}")
        raise

    analytics_path = folder / "analytics_log.json"
    usage_path = folder / "api_usage.json"
    learning_db_path = folder / "learning_db.json"

    prev_state = dict(state.data)
    resumed_topic = _find_resumable_topic(folder, prev_state)
    state.start()

    try:
        niche = channel_data.get("niche", "general")

        if resumed_topic:
            state.step("research", "complete", notify=False, note="resumed from previous failed run")
            state.step("planning", "complete", notify=False, topic=resumed_topic["title"], note="resumed")
            topic = resumed_topic
            script_data = load_json(folder / "script_latest.json", {})
            seo_metadata = load_json(folder / "seo_latest.json", {})
            state.step("script", "complete", notify=False, note="resumed")
            state.step("seo", "complete", notify=False, note="resumed")
        else:
            state.step("research", "running")
            rows = research(niche)
            log_api_call(usage_path, "research")
            save_json(folder / "research_latest.json", rows)
            state.step("research", "complete", count=len(rows))

            analyze_competitors(niche, folder / "competitor_latest.json")

            state.step("planning", "running")
            topic = choose_topic(rows, folder / "topics_history.json", niche)
            # learning.get_or_decide_daily_quota() may adjust today's
            # shorts/long counts from the channel's configured base
            # content_mix, based on this channel's own age/growth data (see
            # that function's docstring for the exact rule).
            quota = get_or_decide_daily_quota(channel_data, folder)
            topic["format"], format_reason = decide_format(quota, analytics_path)
            log_decision(folder, topic, format_reason, quota)
            state.step("planning", "complete", topic=topic["title"], format=topic["format"], quota_reason=quota.get("reason"))

            if args.dry_run:
                state.finish("dry-run", topic=topic)
                print(f"Dry run passed for {channel}: {topic['title']}")
                return

            state.step("script", "running")
            # folder= lets write_script() consult duration_strategy.py for a
            # niche-adaptive length target (competitor-analysis-driven) --
            # see that module and script_writer.py for how the target is
            # decided and, for very long content, split into sections.
            script_data = write_script(topic, channel_data | {"language": config.get("language", "en")}, folder / "script_latest.json", analytics_path, folder)
            script_data = fact_check(script_data, topic.get("url"))
            save_json(folder / "script_latest.json", script_data)
            log_api_call(usage_path, "llm")
            state.step("script", "complete", fact_check_passed=script_data.get("fact_check", {}).get("passed"))

            state.step("seo", "running")
            seo_metadata = generate_seo(topic, script_data["script"], folder / "seo_latest.json")
            log_api_call(usage_path, "llm")
            state.step("seo", "complete")

        if args.dry_run:
            state.finish("dry-run", topic=topic)
            return

        # Per-video audience-profile timing (what the user asked for): reason
        # about this specific topic's likely audience the same way the
        # reference document does, and use it to schedule the exact publish
        # moment via YouTube's own scheduled-publish mechanism rather than
        # relying on cron timing precision. Runs every time (cheap, one LLM
        # call) so a resumed run still gets a fresh profile for the same topic.
        state.step("audience", "running")
        profile = audience.analyze(topic, channel_data)
        scheduled_for = None
        if profile.get("best_hours_utc") and channel_data.get("scheduled_publish", True):
            scheduled_for = audience.next_occurrence(profile["best_hours_utc"][0])
        audience.record(folder, topic, profile, scheduled_for)
        state.step("audience", "complete", scheduled_for=scheduled_for, age_range=profile.get("age_range"))

        media = folder / "media"
        media.mkdir(exist_ok=True)

        voice_name = channel_data.get("voice", config.get("voice", "en-US-AriaNeural"))
        state.step("voice", "running")
        audio = media / "voice.wav"
        # Bug fix: previously the raw script (including bracketed visual
        # directions) went straight to TTS -- see _strip_visual_directions()
        # docstring above. Shorts additionally get a hard word-count cap
        # (_cap_words()) since the LLM doesn't reliably honor the prompt's
        # length instruction on its own.
        narration_text = _strip_visual_directions(script_data["script"])
        if topic.get("format") == "short":
            # 20% slack over the prompted target before hard-capping -- a
            # little natural variance is fine, but the LLM overshooting by
            # 3-4x (the original bug) is not. Tied to whatever
            # script_writer.py actually targeted (niche-adaptive via
            # duration_strategy.py, or a manual shorts_max_words override)
            # rather than a separate hardcoded number living in two places.
            narration_text = _cap_words(narration_text, int(script_data.get("target_word_count", 150) * 1.2))
        voice_generate(narration_text, voice_name, audio)
        state.step("voice", "complete")

        state.step("footage", "running")
        footage_clips = []
        # The other half of the shorts-vs-long fix (see script_writer.py):
        # a "short" now renders vertical instead of reusing the same
        # horizontal resolution as "long" videos, which is required for
        # YouTube to actually treat it as a Short regardless of duration.
        # Computed before the tutorial-capture call below (it used to come
        # after, so a "short" tutorial video was always screen-recorded
        # horizontally and only got letterboxed into vertical afterward).
        resolution = (
            channel_data.get("shorts_resolution", config.get("shorts_resolution", "1080x1920"))
            if topic.get("format") == "short"
            else config.get("resolution", "1280x720")
        )
        target_height = int(resolution.split("x", 1)[1])
        resolution_tuple = tuple(int(x) for x in resolution.split("x", 1))
        if channel_data.get("format_style") == "tutorial" and topic.get("url"):
            captured = capture_tutorial(topic["url"], media / "footage", resolution=resolution_tuple)
            if captured:
                footage_clips.append(captured)
        # Concrete, literal search phrases instead of the raw (often
        # headline-style) topic title -- see visuals.py for why.
        footage_queries = visuals.suggest_queries(topic, script_data["script"])
        # Scale how many distinct clips to fetch with the actual narration
        # length: 5 fixed clips was fine for a ~1-3 minute video, but an
        # hour-long "sleeping story" cutting every ~90s (see cut_seconds
        # below) needs ~40 segments -- 5 clips would mean each one repeats
        # 8x. Capped at 20 since fetching more has diminishing returns and
        # costs more footage-API calls (see COST_LIMITS above).
        estimated_seconds = probe_duration(audio)
        footage_limit = min(20, max(5, round(estimated_seconds / 30)))
        stock = collect(
            footage_queries, media / "footage", folder / "used_footage.json", limit=footage_limit,
            target_height=target_height, fallback_query=niche,
        )
        log_api_call(usage_path, "footage")
        footage_clips.extend(stock)
        music = fetch_music(channel_data.get("music_mood", "ambient"), media / "music", folder / "used_music.json")
        state.step("footage", "complete", count=len(footage_clips), music=bool(music))

        subtitle_path = generate_srt(audio, media / "captions.srt") if channel_data.get("subtitles", True) else None

        state.step("edit", "running")
        video = media / "video.mp4"
        # Cut frequency (what the user asked for): footage should change at
        # least every N seconds, separately configurable for shorts vs long
        # -- previously edit.py only cut once per downloaded clip
        # (duration / len(clips)), which meant a 3-minute video with 5 clips
        # only cut roughly every 36 seconds regardless of format.
        if topic.get("format") == "short":
            cut_seconds = float(channel_data.get("shorts_cut_seconds", 10))
        elif "long_cut_seconds" in channel_data:
            cut_seconds = float(channel_data["long_cut_seconds"])  # explicit override always wins over the adaptive default below
        else:
            # A fixed 30s cut rate makes sense for a 5-minute explainer but
            # not for an hour-long "sleeping story" (that would be 120
            # jarring cuts in a niche that wants the opposite: calm, mostly-
            # static visuals). Scaling keeps roughly the same NUMBER of
            # cuts across very different lengths instead of one interval
            # applied regardless of how long the video actually turned out.
            cut_seconds = min(120.0, max(20.0, estimated_seconds / 40))
        render(footage_clips, audio, video, resolution, music=music, subtitles=subtitle_path, max_segment_seconds=cut_seconds)
        state.step("edit", "complete")

        state.step("thumbnail", "running")
        # Future Learning DB (spec section 10): if we already know which
        # thumbnail style tends to win on this channel, generate it first so
        # it becomes the primary upload thumbnail; every style still gets
        # produced for the A/B rotation below.
        preferred_style = best_thumbnail_style(learning_db_path)
        thumb_variants = make_thumbnails(footage_clips[0], topic["title"], media / "thumbnail.jpg", preferred_style=preferred_style, size=resolution_tuple)
        state.step("thumbnail", "complete", variants=len(thumb_variants), preferred_style=preferred_style)

        upload_enabled = bool(config.get("upload_enabled", False))
        if upload_enabled:
            # CTR pre-check (ctr_check.py): ask a vision LLM to pick the
            # strongest of the already-generated thumbnail variants against
            # the actual title, instead of always trusting preferred_style
            # alone. No-ops (returns the list unchanged) without
            # GEMINI_API_KEY, so this is free to leave on.
            thumb_variants, ctr_note = ctr_check.pick_best(thumb_variants, seo_metadata["title"])
            if ctr_note:
                print(f"[{channel}] CTR pre-check: {ctr_note}")

            # Chapters (chapters.py): real YouTube chapter markers anchored
            # to actual caption timestamps, appended to the description.
            # Helps both search (mini table-of-contents in results) and
            # session watch time (viewers jump to what they want).
            chapter_list = chapters.generate(script_data["script"], subtitle_path)
            if chapter_list:
                seo_metadata["description"] = (seo_metadata.get("description", "") + chapters.format_for_description(chapter_list))[:5000]

        seo_metadata["privacy"] = channel_data.get("publish_privacy", "private")
        if topic.get("format") == "short":
            # YouTube's own convention for surfacing a video in the Shorts
            # shelf reliably -- vertical aspect + short duration is what
            # actually qualifies it, but the hashtag is still the documented
            # signal to add on top of that.
            hashtags = seo_metadata.get("hashtags") or []
            if not any(h.lower() == "#shorts" for h in hashtags):
                seo_metadata["hashtags"] = hashtags + ["#Shorts"]

        alert = check_quota_alert(usage_path, COST_LIMITS)
        if alert:
            notify(f"[{channel}] {alert}")

        video_id = None
        primary_style = thumb_variants[0]["style"] if thumb_variants else None
        # Independent of auto_trigger (trigger_window.py): whether a run was
        # started automatically or via /run has no bearing on whether it
        # needs a human's OK before uploading. Defaults to True (safest) if
        # unset for any reason.
        require_approval = bool(load_json(DATA / "schedule_config.json", {}).get("require_approval", True))
        if upload_enabled:
            decision = "approve"
            if require_approval:
                state.step("approval", "running")
                schedule_note = f"\nScheduled to publish: {scheduled_for} (UTC)" if scheduled_for else ""
                preview_sent = send_video(video, f"Preview ready: {topic['title']}{schedule_note}\nReply /approve or /reject within 5 minutes.")
                if preview_sent:
                    decision = wait_for_decision(300)
                else:
                    # BUG FIX: this previously fell through to decision =
                    # "timeout", which is treated the same as an approval
                    # TIMEOUT below -- i.e. the video uploaded automatically
                    # even though no preview was ever actually delivered and
                    # nobody had a chance to see or reject it. A failed SEND
                    # must not be silently equivalent to an unanswered
                    # approval. Held instead, and reported with plain
                    # sendMessage (notify()) which doesn't share sendVideo's
                    # file-size/attachment failure modes, so the user
                    # actually hears about it instead of the run just
                    # looking like a normal successful upload.
                    decision = "hold"
                    notify(
                        f"[{channel}] Could not deliver the video preview to Telegram -- see the Actions log "
                        f"for the exact error (a common cause is the file exceeding Telegram's ~50MB bot "
                        f"upload limit). Upload was HELD, not sent automatically. Re-run with /run {channel} "
                        f"once you've checked TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID and file size."
                    )
                state.step("approval", "complete", decision=decision, preview_sent=preview_sent)
            if decision in ("reject", "hold"):
                reason = "rejected in Telegram approval mode" if decision == "reject" else "preview delivery to Telegram failed; held for safety"
                status = "rejected" if decision == "reject" else "held"
                state.step("upload", "skipped", reason=reason)
                record(analytics_path, channel, topic["title"], None, status, format=topic.get("format", "long"))
                state.finish(status, topic=topic)
                if decision == "reject":
                    notify(f"Video rejected: {channel} — {topic['title']}")
                return
            state.step("upload", "running", reason="approved or approval timeout")
            primary_thumb = Path(thumb_variants[0]["path"]) if thumb_variants else None
            run_side_effect("before_upload", str(video), seo_metadata, channel)
            video_id = upload(video, seo_metadata, channel, thumbnail=primary_thumb, publish_at=scheduled_for)
            # Spec item #23 A/B Testing: register the remaining variants for
            # scheduled thumbnail+title rotation instead of leaving them
            # unused. seo_metadata["title"] is included first so index 0
            # matches the title actually uploaded above.
            title_variants = [seo_metadata["title"]] + seo_metadata.get("title_variants", [])
            ab_test.register(folder, video_id, thumb_variants, titles=title_variants)
            # Hook-performance learning (what the user asked for): tie this
            # video's opening lines to its eventual retention, so
            # hooks.update_guidance() has something to compare later.
            hooks.record_hook(folder, video_id, script_data["script"])
            state.step("upload", "complete", video_id=video_id, thumbnail_variants=[v["style"] for v in thumb_variants], scheduled_for=scheduled_for)

            # Playlist auto-add: groups this channel's own videos by niche so
            # YouTube can chain "watch next" within the channel -- a direct
            # session-watch-time lever. Opt-out via channel config
            # "auto_playlist": false.
            if channel_data.get("auto_playlist", True):
                playlist_title = channel_data.get("playlist_title") or f"{niche.title()} videos"
                playlist_id = ensure_playlist(channel, playlist_title)
                if playlist_id:
                    add_to_playlist(video_id, playlist_id, channel)

            # Early CTA comment (posts only -- see youtube.post_comment() for
            # why auto-PINNING isn't possible through the public API). Off
            # by default; set channel config "cta_comment" to enable.
            if channel_data.get("cta_comment"):
                post_comment(video_id, channel, channel_data["cta_comment"])

            # Cross-post Shorts to Instagram Reels / TikTok (crosspost.py):
            # no-ops entirely unless CROSSPOST_UPLOAD_WEBHOOK plus at least
            # one platform's credentials are configured -- see that module's
            # module docstring before enabling.
            if topic.get("format") == "short":
                crosspost.post_short(video, seo_metadata, channel)
        else:
            state.step("upload", "skipped", reason="upload_enabled is false")

        record(
            analytics_path, channel, topic["title"], video_id, "uploaded" if video_id else "prepared",
            format=topic.get("format", "long"), thumbnail_style=primary_style, voice=voice_name,
        )

        refresh_metrics(analytics_path, channel)
        if datetime.now(timezone.utc).weekday() == 6:  # weekly, to limit extra LLM/API cost
            suggest_upload_hours(analytics_path, DATA / "schedule_config.json")
            growth_report(analytics_path, folder / "growth_report.json")
            generate_calendar(channel_data, load_json(folder / "research_latest.json", []), folder / "topics_history.json", folder / "content_calendar.json", channel=channel)
            update_learning_db(analytics_path, learning_db_path)
            compute_health_score(channel_data, analytics_path, folder / "health_score.json")
            hooks.refresh_retention(folder, channel)
            hooks.update_guidance(folder)
            # Full-video retention curve (retention.py): complements
            # hooks.py's opening-only analysis with a whole-video pacing
            # check, fed into script_writer.py's next prompt.
            retention.refresh(folder, channel, analytics_path)
            retention.update_pacing_guidance(folder)

        state.finish("complete", topic=topic, video_id=video_id)
        notify(f"Pipeline complete: {channel} — {topic['title']}")
    except Exception as exc:
        state.finish("failed", error=str(exc), topic=locals().get("topic"))
        notify(f"Pipeline failed for {channel}: {exc}")
        raise


if __name__ == "__main__": main()
