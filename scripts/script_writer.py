from __future__ import annotations
from pathlib import Path
from .algo_strategy import context_block
from .duration_strategy import WORDS_PER_SECOND, decide as decide_duration, target_words
from .llm import complete
from .lib.config import save_json
from .lib.plugins import run_chained
from .hooks import current_guidance
from .learning import memory_context, patterns_context
from .rag import query as rag_query
from .retention import current_pacing_guidance

# A safe ceiling for what one LLM completion reliably produces at good
# quality across all three providers (none of which set an explicit
# max_tokens -- see llm.py -- so this is deliberately conservative rather
# than assuming a provider's default output limit). Content whose target
# exceeds this is written in multiple sections instead of one oversized
# request -- see _write_long_form_in_sections().
_SINGLE_CALL_WORD_LIMIT = 700


def _write_long_form_in_sections(base_prompt: str, system: str, target_word_count: int) -> str:
    """For content well beyond what a single LLM completion reliably
    produces (e.g. a niche where competitor analysis shows hour-long videos
    are the norm -- "sleeping story" channels routinely run 1h+), write it
    as consecutive sections instead of one oversized request: each section
    gets the same base instructions plus the tail of the previous section
    for continuity, and is told explicitly whether it's the opening,
    a middle section, or the closing one. This is also what lets the
    matching audio be synthesized section-by-section in voice.py instead of
    one very large TTS call."""
    num_sections = max(1, round(target_word_count / _SINGLE_CALL_WORD_LIMIT))
    sections: list[str] = []
    for i in range(num_sections):
        is_first, is_last = i == 0, i == num_sections - 1
        if is_first and is_last:
            position_note = "Write the complete script: include the hook at the start and a natural, concise ending."
        elif is_first:
            position_note = "This is the OPENING section -- include the hook."
        elif is_last:
            position_note = "This is the CLOSING section -- bring the narration to a natural, concise end, no cliffhanger, no new topic."
        else:
            position_note = "This is a MIDDLE section -- continue directly from where the previous section left off. No re-introduction, no repeated hook, no summarizing what was already said."
        continuity = f"\n\nThe previous section ended with:\n...{sections[-1][-400:]}\n" if sections else ""
        prompt = f"{base_prompt}\n\n{position_note} Write approximately {_SINGLE_CALL_WORD_LIMIT} words for THIS SECTION ONLY.{continuity}"
        try:
            sections.append(complete(system, prompt))
        except Exception as exc:
            print(f"Long-form section {i + 1}/{num_sections} failed, stopping here with what was generated so far: {exc}")
            break
    return "\n\n".join(sections)


def write_script(topic: dict[str, str], channel_data: dict, output: Path, analytics_path: Path | None = None, folder: Path | None = None) -> dict:
    language = channel_data.get("language", "en")
    memory = memory_context(analytics_path) if analytics_path else ""
    memory_block = f"\nChannel history/context:\n{memory}\n" if memory else ""

    patterns = patterns_context(analytics_path) if analytics_path else ""
    patterns_block = f"\n{patterns}\n" if patterns else ""

    knowledge = rag_query(f"{topic['title']} ({topic['niche']})")
    knowledge_block = f"\n{knowledge}\n" if knowledge else ""

    guidance = current_guidance(analytics_path.parent) if analytics_path else ""
    guidance_block = (
        f"\nBased on this channel's own retention data, the strongest hooks so far follow this pattern: "
        f"{guidance}\nApply this pattern to the opening of this script.\n" if guidance else ""
    )

    pacing = current_pacing_guidance(analytics_path.parent) if analytics_path else ""
    pacing_block = f"\nPacing note from this channel's own retention data: {pacing}\n" if pacing else ""

    is_short = topic.get("format") == "short"

    # Niche-adaptive duration (what the user asked for): analyze what's
    # ACTUALLY succeeding for this specific niche's competitors right now
    # (duration_strategy.py) instead of one fixed length for every niche --
    # a "sleeping story" channel's competitors run 1+ hour, a "health tips"
    # channel's run a few minutes, and the same target would be wrong for
    # either audience. Falls back to a generic default if no folder was
    # given (dry runs) or competitor data is unavailable.
    niche_seconds = None
    if folder is not None:
        try:
            strategy = decide_duration(topic["niche"], folder)
            niche_seconds = strategy["short_seconds"] if is_short else strategy["long_seconds"]
        except Exception as exc:
            print(f"Duration strategy unavailable, falling back to static defaults: {exc}")

    if is_short:
        # An explicit shorts_max_words in config.json is a manual override
        # and always wins over the auto-decided target -- automation should
        # never silently overrule something the user typed in on purpose.
        manual = channel_data.get("shorts_max_words")
        target_word_count = int(manual) if manual else target_words(niche_seconds) if niche_seconds else 150
        length_instruction = (
            f"This is a YouTube Shorts script: target roughly {target_word_count} words of narration total "
            f"(about {target_word_count / WORDS_PER_SECOND:.0f} seconds spoken), a single core idea, no filler, "
            "hook in the first 1-2 seconds."
        )
    else:
        manual = channel_data.get("long_target_words")
        target_word_count = int(manual) if manual else target_words(niche_seconds) if niche_seconds else 700
        length_instruction = (
            f"Use a strong first 15-second hook, clear sections, and a concise ending. Target roughly "
            f"{target_word_count} words of narration total (about {target_word_count / WORDS_PER_SECOND / 60:.1f} "
            "minutes spoken), matching what similar videos in this niche typically run."
        )

    base_prompt = (
        f"Write a fact-aware YouTube script in {language}. Topic: {topic['title']}. "
        f"Niche: {topic['niche']}. {length_instruction} Include visual directions in [brackets]. "
        f"Do not invent statistics.\n{context_block(shorts=is_short)}"
        f"{memory_block}{patterns_block}{knowledge_block}{guidance_block}{pacing_block}"
        "Return only the narration and bracketed visual directions."
    )
    system = "You are a careful YouTube script editor. Prefer qualified claims and cite source URLs when possible."
    if target_word_count <= _SINGLE_CALL_WORD_LIMIT:
        text = complete(system, base_prompt)
    else:
        text = _write_long_form_in_sections(base_prompt, system, target_word_count)

    result = {"topic": topic, "language": language, "script": text, "target_word_count": target_word_count}
    result = run_chained("post_process_script", result, topic) or result
    save_json(output, result)
    return result


def fact_check(script: dict, source_url: str | None = None) -> dict:
    """Spec item #24: a second, independent LLM pass that checks the drafted
    script for unverifiable or overstated claims before it goes to voice/edit.
    This must never take the whole run down: the main script already
    generated successfully by this point, so a transient LLM hiccup on this
    second pass should just mean "not checked", not a failed pipeline run."""
    context = f"Source article for reference: {source_url}\n\n" if source_url else ""
    prompt = (
        f"{context}Review this YouTube narration for factual claims that are unverifiable, overstated, "
        f"or missing a qualifier (e.g. 'studies show' with no study named). List each problem claim on its "
        f"own line with a suggested fix. If nothing needs fixing, reply exactly: OK.\n\nScript:\n{script['script']}"
    )
    try:
        review = complete("You are a fact-checking editor for a YouTube channel. Be skeptical and specific.", prompt, temperature=0.2)
        passed = review.strip().upper() == "OK"
        script["fact_check"] = {"passed": passed, "notes": None if passed else review}
    except Exception as exc:
        script["fact_check"] = {"passed": None, "notes": f"Fact-check unavailable: {exc}"}
    return script
