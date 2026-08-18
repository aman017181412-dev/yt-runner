from __future__ import annotations

"""
Centralized, evidence-based YouTube discovery guidance, injected into the SEO
and script-writing LLM prompts so every generated title/description/hook is
written with two separate discovery paths in mind:

  1. SEARCH: a viewer typing a query into YouTube/Google search.
  2. SUGGESTED / HOME / SHORTS FEED: YouTube's recommender showing the video
     to people who never searched for it and don't know the channel yet --
     this is what actually grows a channel past its search-only ceiling, and
     it optimizes for session-level signals (click-through rate relative to
     impressions, watch time relative to the video's own length, and whether
     the *next* video also gets watched), not keyword density.

This is a written-down summary of YouTube Creator Academy's own public
guidance, kept in one place so every prompt that touches title/description/
hook writing stays consistent instead of each one re-deriving its own
(possibly inconsistent) advice. It is not a claim about undocumented ranking
internals -- no such claim is made or needed for this to be useful.
"""

SEARCH_PRINCIPLES = (
    "For SEARCH discovery: include the exact phrase a viewer would type into the search bar in the title "
    "(near the front, not buried) and as the opening sentence of the description -- the first ~1-2 lines of "
    "the description carry more search weight than the rest. Match search INTENT, not just keywords: "
    "content answering 'how to X' should read as a direct answer to 'how to X', not merely mention X in "
    "passing. Never drop the actual searchable phrase in favor of vague clickbait."
)

DISCOVERY_PRINCIPLES = (
    "For SUGGESTED/Shorts-feed discovery (shown to people who have never seen this channel before): the "
    "first 3-5 seconds must make sense with zero prior context -- no 'as I mentioned before', no assumed "
    "familiarity with the channel or its previous videos. Open with the payoff or a concrete, specific "
    "curiosity gap (a number, a contradiction, a stake) rather than a slow windup or channel-intro. Watch "
    "time relative to the video's OWN length matters more than absolute seconds watched, and a clean, "
    "un-padded ending that doesn't waste the last few seconds helps the next video in-session also get "
    "watched -- both feed the same discovery loop."
)

SHORTS_PRINCIPLES = (
    "For Shorts specifically: vertical framing and a loopable or clean ending (no dead air, no slow fade) "
    "measurably help re-watch/completion rate, which is the primary Shorts-feed ranking signal."
)

TITLE_VARIANT_PRINCIPLE = (
    "When asked for alternative title variants, make them meaningfully different strategies -- not just "
    "reworded synonyms -- e.g. one variant leads with the exact search phrase, another leads with a "
    "curiosity gap for the suggested/feed audience. Both must stay truthful to the actual video content; a "
    "title that overpromises gets a worse average-view-duration, which is itself a ranking signal, so "
    "misleading titles are self-defeating, not just against policy."
)


def context_block(*, shorts: bool = False) -> str:
    """Compact combined block for injecting into an LLM system/user prompt."""
    parts = [SEARCH_PRINCIPLES, DISCOVERY_PRINCIPLES]
    if shorts:
        parts.append(SHORTS_PRINCIPLES)
    return "\n".join(parts)
