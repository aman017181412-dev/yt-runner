# yt-core

Private engine for the automated YouTube content factory. Import this folder into a **private** GitHub repository named `yt-core`. Do not add GitHub Actions workflows here.

## Required setup

1. Copy `config.example.json` to `config.json` and edit the channel, niche, language, and upload settings. Per-channel fields: `content_mix` (shorts/long daily quota), `subtitles` (bool), `music_mood`, `format_style` (`"explainer"` or `"tutorial"` — tutorial triggers a Playwright screen recording of the source URL as one of the footage clips), `auto_playlist` (bool, default true — groups this channel's uploads into a niche playlist), `playlist_title` (defaults to `"{niche} videos"`), `cta_comment` (optional — posts this text as a comment right after upload; unset means no comment is posted), `reoptimize` (bool, default true — allows the hourly job to re-title a video that's clearly underperforming its own channel's median, once, ~30h after upload), `shorts_cut_seconds` (default 10 — how often Shorts footage cuts to a new clip). Duration and long-form cut pacing are niche-adaptive by default (see "Niche-adaptive duration" under Growth features below) — `shorts_max_words`, `long_target_words`, and `long_cut_seconds` are optional manual overrides, left unset in the example config on purpose so the automatic behavior is what new channels get out of the box. Top-level `voice_provider` and `llm.provider` can be pinned to one provider instead of `"auto"` (see "Provider behavior" below).
2. Update `data/schedule_config.json` if you want different UTC trigger windows. Two independent settings live here (see "Automation controls" below): `"auto_trigger"` (does the hourly window start runs on its own?) and `"require_approval"` (does a run wait for a Telegram approval before uploading?). The shipped default is `auto_trigger: true, require_approval: true` — fully hands-off content generation, with a human always making the final call before anything goes live.
3. (Optional) Drop reference documents into `knowledge/` for the RAG knowledge base, and/or add `*.py` files to `plugins/` — see each folder's `README.md`.
4. In the public `yt-runner` repository, add these Actions secrets:

| Secret | Purpose |
|---|---|
| `PRIVATE_REPO_PAT` | Fine-grained Contents read/write PAT for this private repo only |
| `TELEGRAM_BOT_TOKEN` | Telegram control and notifications |
| `TELEGRAM_CHAT_ID` | The **only** Telegram chat allowed to send control commands (`/run`, `/autotrigger`, `/approval`, etc.) or approve/reject uploads — messages from any other chat are ignored |
| `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY` | Script, fact-check, SEO, self-learning, and growth-report prompts — tried in that order (`llm.py`); only one is required. `GEMINI_API_KEY` specifically is also required for `ctr_check.py`'s vision-based thumbnail pre-check (Gemini-only — Groq/OpenRouter free-tier models can't see images) and for `reoptimize.py`'s title-suggestion pass if it's the only key configured |
| `ELEVENLABS_API_KEY`, `FISH_API_KEY` | Optional paid voice fallbacks if `edge-tts` (free, no key needed) becomes unavailable (`voice.py`) |
| `PEXELS_API_KEY` | Free stock video search |
| `PIXABAY_API_KEY` | Free stock video + background music search |
| `YOUTUBE_RESEARCH_API_KEY` | YouTube Data API key (search+videos, read-only) used for competitor analysis and the YouTube research source — separate from the per-channel upload tokens below |
| `YOUTUBE_CLIENT_SECRET_JSON` | OAuth client id/secret JSON, shared by all channels under one Google Cloud project; merged with each channel's refresh token so uploads (and thumbnail A/B rotation) can auto-refresh |
| `CHANNEL1_YT_TOKEN`, `CHANNEL2_YT_TOKEN`, ... | Per-channel OAuth token JSON (refresh token; client id/secret may be included here instead of relying on `YOUTUBE_CLIENT_SECRET_JSON`) |
| `CROSSPOST_UPLOAD_WEBHOOK`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_BUSINESS_ID`, `TIKTOK_ACCESS_TOKEN` | **Optional**, all unset by default — enables cross-posting Shorts to Instagram Reels/TikTok (`crosspost.py`). Read that file's module docstring before setting these up; it's the most involved feature here to configure (needs your own public-file-hosting webhook plus each platform's own business/developer app approval) |

5. In both public workflow files replace `your-username/yt-core`.
6. Set `upload_enabled` to `true` only after a dry run succeeds and YouTube OAuth is configured.
7. `require_approval` in `data/schedule_config.json` (on by default) makes every run send a Telegram preview and wait up to five minutes for `/approve` or `/reject`; a timeout proceeds with upload. Turn it off with `/approval off` (or by editing the file) to upload automatically with no preview step.

## Bug fixes (this update)

Four real, reported problems in the running pipeline, root-caused and fixed:

1. **TTS was reading the bracketed visual directions out loud.** `script_writer.py`'s prompt asks for narration with `[cut to a laptop screen]`-style cues mixed in (for footage/context), but `run_pipeline.py` was passing that raw text straight to `voice_generate()` unmodified — so every `[bracket direction]` got spoken as part of the video, padding out the audio (and therefore final video length) with sentences nobody meant to be heard. `hooks.py` already stripped this same pattern for its own hook analysis; that fix is now applied to the actual narration too, via `_strip_visual_directions()` right before TTS.
2. **Shorts had no real duration cap, so they weren't actually short.** The word-count instruction in `script_writer.py`'s prompt is only a suggestion — the LLM doesn't reliably follow it, and combined with bug #1's padding, a "short" could easily run to several minutes. `_cap_words()` now hard-caps Shorts narration at 20% over whatever target was actually decided (niche-adaptive via `duration_strategy.py` by default, or a manual `shorts_max_words` override — see "Niche-adaptive duration" below) at the last full sentence within the limit before TTS runs, so a Short is actually the intended length regardless of what the LLM wrote. (Note: YouTube itself only requires a vertical video be under 3 minutes to count as a Short at all, since its Oct-2024 rule change — but a 3-minute video isn't the snappy, fast-paced format most creators mean by "Short," which is why the default target sits well below that.)
3. **Footage only cut once per downloaded clip, not on a real time interval.** `edit.py` computed cut frequency as `duration / number_of_clips_downloaded` — with 5 clips and a 3-minute video, that's a new clip only every ~36 seconds regardless of format. `render()` now takes `max_segment_seconds` (wired to `shorts_cut_seconds`/`long_cut_seconds` per format) and computes how many segments are actually needed to hit that cut rate, cycling through the downloaded clips (with a varied start point on repeats) if more segments are needed than there are distinct clips.
4. **A failed Telegram preview silently skipped approval and uploaded anyway.** If `send_video()` failed for any reason (most commonly: the file exceeds Telegram bot API's ~50MB upload limit), the code treated that identically to an approval TIMEOUT and uploaded the video with nobody ever having seen a preview or had a chance to reject it — approval mode was silently defeated. This is now a distinct `"hold"` outcome: the run stops, nothing uploads, and a plain text Telegram message (which doesn't share `sendVideo`'s attachment-size failure mode) explains what happened. `send_video()`/`send_photo()` also now print a specific reason (missing token/chat ID, missing file, or over the size limit) instead of returning `False` with no diagnostic at all — check the Actions log for that line if a preview still doesn't arrive.

If YouTube itself still isn't showing a custom thumbnail after these fixes, that's almost always the channel not being phone-verified yet (`youtube.com/verify`) rather than a code issue — `youtube.py` already sends the generated thumbnail image to Telegram as a fallback when the API attach call fails, so check for that message too.

## Local checks

```bash
python -m compileall scripts
python -m scripts.run_pipeline --channel channel1 --dry-run
```

The pipeline stores only JSON/history and generated metadata in git. Generated video/audio/image files are ignored by `.gitignore` and deleted by the runner after the job — with one deliberate exception: `ab_tests.json` embeds small (640x360, resized) thumbnail variants as base64 so a *later* hourly trigger-window run can still swap the live thumbnail (see "A/B testing" below). Because research/script/SEO JSON *are* committed, a run that fails partway through (e.g. footage or upload step) will reuse the same topic and script on the next `/run` instead of burning fresh LLM calls (`_find_resumable_topic` in `run_pipeline.py`).

## Automation controls

`data/schedule_config.json` has two independent toggles, both changeable live via Telegram (no redeploy needed) or by editing the file directly:

- **`auto_trigger`** (`/autotrigger on|off`) — whether the hourly trigger window checks each channel's daily `content_mix` quota on its own and starts `video-pipeline.yml` without anyone sending `/run`.
- **`require_approval`** (`/approval on|off`) — whether a run sends a Telegram preview and waits for `/approve`/`/reject` before uploading, regardless of how the run was started.

These are deliberately separate: the shipped default (`auto_trigger: true, require_approval: true`) means content generation is fully hands-off, but nothing reaches YouTube without an explicit approval. Turning `require_approval` off is a real "fully automatic, no human check" mode — only turn it off once you trust the output quality.

## Telegram commands

`/run <channel>`, `/approve`, `/reject`, `/change_niche <name>`, `/set_schedule <hours>`, `/autotrigger on|off`, `/approval on|off`, `/status` (reads live `state.json` per channel, not a static message)

Every pipeline step also sends its own Telegram notification on completion (spec item #18), in addition to the run-level "complete/failed/rejected" messages.

## What's implemented

All 30 spec points plus the section-10/13 "extra" features are implemented:

- Research: Google Trends (pytrends), Google News RSS, Reddit, Hacker News, YouTube Data API, plus anything a plugin's `extra_research()` hook adds — each source degrades independently.
- Competitor analysis via the YouTube Data API (`competitor.py`).
- Content-mix planner: picks short vs. long form per run against each channel's daily quota, with the reason logged (`planner.decide_format` / `planner.log_decision`). **The format actually changes what gets produced**: a "short" gets a ~100-150 word script (script_writer.py), renders at `shorts_resolution` (default 1080x1920, vertical) instead of `resolution`, and gets `#Shorts` added to its hashtags — earlier revisions only used the format label for quota counting, so a "short" was functionally identical to a "long" video (same full-length script, same horizontal resolution) and could never actually surface as a Short on YouTube regardless of what analytics_log said.
- Script writer with channel-history context, learned performance patterns, and RAG knowledge-base excerpts all injected into the prompt, plus a separate fact-check LLM pass before voice/edit.
- Multi-clip video editing (uses every downloaded footage clip, not just the first), optional ducked background music, and burned-in captions from `faster-whisper` word timestamps.
- Footage search uses several short, concrete phrases derived from the script (`visuals.suggest_queries()`) rather than the raw topic title as one literal query — a full headline like "YouTube Mistakenly Penalizes..." matches almost nothing in a stock library, which used to leave some videos stretched from a single, barely-related clip. Falls back to the channel's niche as a broader search if every phrase still comes up thin.
- If a custom thumbnail can't be attached to the video (most often: the channel isn't phone-verified yet), the image itself is sent to Telegram as a photo so it's not just discarded — you can add it manually from YouTube Studio while waiting to verify.
- Multiple thumbnail variants per run; the historically best-performing style (once there's enough data) is generated first and used as the primary upload thumbnail.
- Real YouTube Analytics import (views, watch time, likes, and impressions/CTR where the account has access) merged into `analytics_log.json`.
- Best-upload-time suggestion written to `schedule_config.json` as `suggested_hours_utc` (never auto-applied — that stays a deliberate choice).
- Weekly rule-based content calendar (`calendar_planner.py`).
- Cost/quota tracking with a Telegram alert when a provider's daily call count crosses a configured soft limit.
- Optional Playwright tutorial screen-recording for channels with `"format_style": "tutorial"`.
- **AI/Voice provider switch**: LLM tries Groq → OpenRouter → Gemini; voice tries edge-tts → ElevenLabs → Fish Speech. Config-driven, no code changes needed to switch (`llm.py`, `voice.py`).
- **AI Decision Log**: `decision_log.json` per channel records why each topic and format were chosen (`planner.log_decision`).
- **Future Learning DB**: `learning_db.json` ranks thumbnail styles, voices, and formats by average views once there's enough history, and feeds back into both the script prompt and thumbnail style selection (`learning.update_learning_db`).
- **Channel Health Score**: `health_score.json`, a weekly Growth/SEO/CTR/Retention/Consistency/Overall composite, 0-100 each (`learning.compute_health_score`).
- **Plugin architecture**: drop-in `plugins/*.py` files can add research sources or post-process the script/SEO/upload steps (`lib/plugins.py`).
- **Knowledge Base (RAG, Phase 2)**: `knowledge/*.md` files are indexed into a local, in-memory ChromaDB collection each run and the most relevant excerpts are added to the script prompt (`rag.py`).
- **A/B testing**: YouTube's Studio "Test & compare" feature has no public API, so this automates the practical substitute instead — the live thumbnail rotates through the generated variants on a 48-hour schedule (checked by the hourly trigger window) and the best-performing one by view count is kept once every variant has had a turn (`ab_test.py`).
- **Hook-performance learning**: each upload's opening lines are tied to its early-video retention (`elapsedVideoTimeRatio`/`audienceWatchRatio` from YouTube Analytics). Weekly, once there are enough data points, an LLM pass compares the best- and worst-retention hooks and writes a concrete instruction to `hook_guidance.json`, which every subsequent script prompt includes (`hooks.py`).
- **Dynamic daily quota**: instead of a fixed `content_mix` forever, `learning.get_or_decide_daily_quota()` adjusts today's shorts/long counts (capped by `max_shorts_per_day`/`max_long_per_day`) based on this channel's own age and its weekly-computed growth score — e.g. testing +1 short/day when growth is soft. This is a rule grounded in the channel's own real analytics, not a live web search for generic growth advice. Recomputed once per UTC day, cached in `daily_plan.json`.
- **Per-video audience-profile timing + scheduled publish**: before rendering, an LLM reasons about the specific video's likely audience (age range, psychology, best hours) the same way a human strategist would — there's no data feed for a not-yet-uploaded video's actual audience, so this is a labeled heuristic, not a measured fact (`audience.py`). If it produces a time, the video is uploaded with YouTube's native scheduled-publish (`status.privacyStatus: "private"` + `status.publishAt`), so it goes public automatically at that moment instead of relying on cron timing precision. Disable per-channel with `"scheduled_publish": false`.

## Growth features (added on top of the 30-point spec)

- **Algorithm-aware prompts** (`algo_strategy.py`): a written-down summary of YouTube's own public search/discovery guidance is injected into every script and SEO prompt, so titles/descriptions target search intent while hooks/pacing target the suggested/Shorts-feed audience (people who've never seen the channel before) — these are two different discovery paths with different requirements, and the prompts now address both explicitly instead of only search-style keywording.
- **Title A/B testing**: `seo.py` now also generates two alternative titles (`title_variants`), and `ab_test.py`'s existing 48-hour thumbnail rotation swaps the title alongside the thumbnail on each turn (`youtube.update_title`) instead of testing thumbnails alone.
- **YouTube chapters** (`chapters.py`): real chapter markers anchored to actual caption timestamps (not invented ones), enforcing YouTube's own requirements (first chapter at 0:00, ≥3 chapters, ≥10s apart) before appending them to the description. Needs `subtitles: true` for that channel, since chapters are built from the SRT.
- **CTR pre-check** (`ctr_check.py`): before upload, a vision-capable LLM (Gemini) scores each already-generated thumbnail variant against the actual title and picks the strongest one as primary — a second, independent judgment instead of always trusting whichever style rendered/ranked first. No-ops without `GEMINI_API_KEY`.
- **Playlist auto-add**: each upload is added to a per-niche playlist (auto-created if it doesn't exist yet) — a direct session-watch-time lever, since YouTube can chain "watch next" within the channel. Toggle with `auto_playlist`/`playlist_title`.
- **Early CTA comment**: if `cta_comment` is set, it's posted as a top-level comment right after upload. Note: the YouTube Data API has no public endpoint for *pinning* a comment (only Studio's UI can) — this posts, it doesn't pin.
- **Underperformer re-optimization** (`reoptimize.py`): the hourly job checks videos ~30 hours old; if a video's views are clearly behind this channel's own recent median, one alternative title is generated and applied automatically. Runs at most once per video (tracked in `reoptimize_log.json`) — a single corrective nudge, not a repeating experiment.
- **Full retention-curve pacing guidance** (`retention.py`): complements `hooks.py` (which only looks at the opening ~10%) by finding where in the REST of the video viewers consistently drop off, and turning a real channel-wide pattern into a pacing instruction for the next script — same mechanism as hook guidance, applied to structure instead of openings.
- **Trend-velocity prioritization**: Google Trends "rising" queries are now tagged with a growth score and sorted to the front of the merged research list (`research.py`), so `planner.choose_topic()`'s existing first-unused scan naturally reacts to a genuine spike instead of only working through the queue in whatever order sources happened to return it.
- **Cross-posting** (`crosspost.py`, optional): Shorts can be re-posted to Instagram Reels and/or TikTok with a caption linking back to YouTube. This is the most involved feature to actually turn on — read the module's docstring before setting up `CROSSPOST_UPLOAD_WEBHOOK` and each platform's credentials.
- **Niche-adaptive duration** (`duration_strategy.py`): instead of one fixed target length for every channel, this analyzes up to 25 top competitor videos for the channel's actual niche (view-weighted, split into a ≤3-minute "short" bucket and a longer bucket — YouTube's own Shorts ceiling since its Oct-2024 rule change) and targets the median of what's actually succeeding there. A "sleeping story" niche's long-form competitors typically run 1h+; a "health tips" niche's typically run a few minutes — both get their own real target instead of a generic default, and it's re-computed at most weekly per niche (cached in `duration_strategy.json`) since it costs a YouTube Data API call. `shorts_max_words`/`long_target_words` in a channel's config remain available as **manual overrides** that always win over the automatic target. For a niche whose target is well beyond what one LLM completion reliably produces (roughly >700 words), `script_writer.py` writes the script in multiple continuous sections instead of one oversized request, and `voice.py` synthesizes long narration in matching chunks (concatenated with ffmpeg) rather than trusting a single very large TTS call — this is what actually makes an hour-long "sleeping story" video work end to end, not just the word-count math.

## Provider behavior

The code uses free/public sources first and is explicit when a required provider is missing. LLM calls try Groq, then OpenRouter, then Gemini (first one with a key configured, in `"auto"` mode; pin one with `llm.provider` in config.json). Voice generation tries edge-tts (free, no key) first, then ElevenLabs, then Fish Speech if edge-tts fails or `voice_provider` is pinned elsewhere. Research sources degrade independently so one unavailable feed does not discard the rest of the research set. Optional features (music, subtitles, competitor analysis, tutorial capture, RAG, plugins) all fail soft — a missing key, empty folder, or unavailable library skips that one feature instead of failing the run.

## Honest limits of the self-improvement features

- Thumbnail style / voice / format ranking, hook guidance, and dynamic quota all genuinely improve using this channel's own accumulated data — but none of them make the underlying LLM itself smarter. Script/title writing quality is bounded by whichever LLM provider is actually generating the text; it doesn't get better with more runs on its own. If script quality matters most, pin a stronger model with `llm.provider`.
- Hook guidance and the health score both need real accumulated history (roughly 6-10 uploaded videos with a few days of views each) before they produce anything — until then they're silently absent, not filled with placeholder guesses.
- Audience-profile timing is LLM reasoning about a topic, not measured data — treat `audience_profiles.json` as an informed guess, same as a human strategist's judgment call, not a fact.
- Underperformer re-optimization changes the title only, not the thumbnail or video itself, and only once per video — it's a small corrective nudge for a title that's clearly not working, not a substitute for making a genuinely good video.
- CTR pre-check and re-optimization's title suggestions both depend on the LLM having a reasonable sense of what makes a title clickable for the channel's specific niche/audience — neither is a guaranteed CTR improvement, just an informed second opinion grounded in the channel's own topic and (for CTR pre-check) the actual generated thumbnail image.
- Cross-posting requires infrastructure this project doesn't provide out of the box (a public file host for the webhook) plus Instagram Business/Facebook Page setup and a TikTok developer app that's passed their content-posting audit — it's realistic to leave this off entirely and it doesn't block or slow down anything else in the pipeline either way.
