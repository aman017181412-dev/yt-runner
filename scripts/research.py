from __future__ import annotations
import os
from typing import Any
import feedparser
import requests
from .lib.plugins import run_collecting


def _get(url: str, **kwargs: Any) -> Any:
    response = requests.get(url, timeout=20, headers={"User-Agent": "yt-core-research/1.0"}, **kwargs)
    response.raise_for_status()
    return response


def rss_topics(niche: str) -> list[dict[str, str]]:
    urls = [
        f"https://news.google.com/rss/search?q={requests.utils.quote(niche)}&hl=en-US&gl=US&ceid=US:en",
        "https://hnrss.org/frontpage",
    ]
    rows: list[dict[str, str]] = []
    for url in urls:
        try:
            feed = feedparser.parse(_get(url).content)
            rows.extend({"title": e.get("title", ""), "url": e.get("link", ""), "source": "rss"} for e in feed.entries[:20])
        except Exception as exc:
            print(f"RSS source unavailable: {exc}")
    return rows


def reddit_topics(niche: str) -> list[dict[str, str]]:
    try:
        data = _get(f"https://www.reddit.com/search.json?q={requests.utils.quote(niche)}&sort=hot&limit=20").json()
        return [{"title": item["data"].get("title", ""), "url": "https://reddit.com" + item["data"].get("permalink", ""), "source": "reddit"} for item in data.get("data", {}).get("children", [])]
    except Exception as exc:
        print(f"Reddit source unavailable: {exc}")
        return []


def hacker_news() -> list[dict[str, str]]:
    try:
        ids = _get("https://hacker-news.firebaseio.com/v0/topstories.json").json()[:20]
    except Exception as exc:
        print(f"Hacker News unavailable: {exc}")
        return []
    rows = []
    for item_id in ids:
        # Per-item try/except: this was previously one big try/except
        # around the whole loop, so a single transient failure partway
        # through (e.g. item 5 of 20) discarded every item already fetched
        # instead of just skipping that one story.
        try:
            item = _get(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json").json()
            rows.append({"title": item.get("title", ""), "url": item.get("url", f"https://news.ycombinator.com/item?id={item_id}"), "source": "hacker-news"})
        except Exception as exc:
            print(f"Hacker News item {item_id} unavailable, skipping: {exc}")
    return rows


def trends_topics(niche: str) -> list[dict[str, str]]:
    """Google Trends related queries for the niche, via pytrends. "rising"
    queries are tagged with a growth score -- this is the trend-velocity
    signal that lets choose_topic() (via collect()'s sort below) react to a
    genuine spike instead of only ever working through the normal research
    queue in whatever order sources happened to return it."""
    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="en-US", tz=0, timeout=(5, 15))
        pytrends.build_payload([niche], timeframe="now 7-d")
        related = pytrends.related_queries().get(niche, {})
        rows: list[dict[str, str]] = []
        for key in ("rising", "top"):
            table = related.get(key)
            if table is None:
                continue
            for _, record in table.iterrows():
                query = str(record.get("query", "")).strip()
                if not query:
                    continue
                row = {"title": query, "url": f"https://trends.google.com/trends/explore?q={requests.utils.quote(query)}", "source": "google-trends"}
                if key == "rising":
                    # pytrends reports huge relative jumps as the string
                    # "Breakout" instead of a number -- treat that as
                    # maximum priority rather than failing to parse it.
                    raw_value = record.get("value", 0)
                    row["trending"], row["growth"] = True, (999999 if str(raw_value).strip().lower() == "breakout" else int(raw_value or 0))
                rows.append(row)
        rows.sort(key=lambda r: r.get("growth", 0), reverse=True)
        return rows[:20]
    except Exception as exc:
        print(f"Google Trends unavailable: {exc}")
        return []


def youtube_topics(niche: str) -> list[dict[str, str]]:
    key = os.getenv("YOUTUBE_RESEARCH_API_KEY")
    if not key:
        return []
    try:
        data = _get("https://www.googleapis.com/youtube/v3/search", params={"part": "snippet", "q": niche, "type": "video", "order": "viewCount", "maxResults": 20, "key": key}).json()
        return [{"title": item["snippet"].get("title", ""), "url": f"https://youtube.com/watch?v={item['id']['videoId']}", "source": "youtube"} for item in data.get("items", [])]
    except Exception as exc:
        print(f"YouTube research unavailable: {exc}")
        return []


def collect(niche: str) -> list[dict[str, str]]:
    rows = trends_topics(niche) + rss_topics(niche) + reddit_topics(niche) + hacker_news() + youtube_topics(niche)
    for plugin_rows in run_collecting("extra_research", niche):
        rows.extend(plugin_rows)
    seen: set[str] = set()
    unique = []
    for row in rows:
        title = row.get("title", "").strip()
        if title and title.lower() not in seen:
            seen.add(title.lower())
            unique.append(row)
    # Trend-velocity feature: genuine Google Trends "rising" spikes float to
    # the front regardless of which source found them first. Python's sort
    # is stable, so non-trending rows (growth=0) keep their original
    # relative order -- this only re-prioritizes real spikes, it doesn't
    # shuffle everything else. planner.choose_topic() scans in this order
    # and picks the first unused one, so no change is needed there.
    unique.sort(key=lambda r: r.get("growth", 0), reverse=True)
    return unique[:100]
