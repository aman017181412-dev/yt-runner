from __future__ import annotations
import os
from pathlib import Path
import requests
from .lib.config import load_json, save_json


def _download(url: str, path: Path) -> None:
    with requests.get(url, stream=True, timeout=90, headers={"User-Agent": "yt-core-footage/1.0"}) as res:
        res.raise_for_status()
        with path.open("wb") as fh:
            for chunk in res.iter_content(1024 * 256):
                if chunk: fh.write(chunk)


def _closest_pexels_file(files: list[dict], target_height: int) -> dict | None:
    """Pick the file closest to the target resolution instead of always the
    largest -- always grabbing the biggest available (often 4K) file wastes
    bandwidth and Actions minutes for a video that gets scaled down anyway."""
    if not files:
        return None
    return min(files, key=lambda f: abs((f.get("height") or 0) - target_height))


def _search_one(query: str, per_page: int, target_height: int) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    if os.getenv("PEXELS_API_KEY"):
        try:
            data = requests.get(
                "https://api.pexels.com/videos/search",
                params={"query": query, "per_page": per_page},
                headers={"Authorization": os.environ["PEXELS_API_KEY"]}, timeout=30,
            ).json()
            for video in data.get("videos", []):
                best = _closest_pexels_file(video.get("video_files", []), target_height)
                if best and best.get("link"):
                    results.append((f"pexels-{video['id']}", best["link"]))
        except Exception as exc:
            print(f"Pexels search for '{query}' unavailable: {exc}")
    if os.getenv("PIXABAY_API_KEY"):
        try:
            data = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": os.environ["PIXABAY_API_KEY"], "q": query, "per_page": per_page}, timeout=30,
            ).json()
            for video in data.get("hits", []):
                results.append((f"pixabay-{video['id']}", video.get("videos", {}).get("medium", {}).get("url", "")))
        except Exception as exc:
            print(f"Pixabay search for '{query}' unavailable: {exc}")
    return results


def collect(
    queries: str | list[str], out_dir: Path, used_path: Path, limit: int = 5,
    target_height: int = 720, fallback_query: str | None = None,
) -> list[Path]:
    """Spec item #7. `queries` is normally several short, concrete search
    phrases (see visuals.suggest_queries()) rather than one big string --
    searching stock libraries with a full topic headline as a single literal
    query routinely returns few or irrelevant hits (stock libraries are
    indexed by concrete real-world subjects, not abstract news phrasing),
    which is how a whole video could previously end up stretched from a
    single, barely-related clip. Each query is searched independently and
    the results are pooled, so one weak query doesn't starve the video of
    footage. A single string is still accepted for backward compatibility.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    used = load_json(used_path, [])
    used_ids = {str(x) for x in used}
    query_list = [queries] if isinstance(queries, str) else list(queries)

    seen_ids: set[str] = set()
    results: list[tuple[str, str]] = []
    for q in query_list:
        for media_id, url in _search_one(q, per_page=max(3, limit), target_height=target_height):
            if media_id not in seen_ids:
                seen_ids.add(media_id)
                results.append((media_id, url))
        if len(results) >= limit * 3:  # enough candidates gathered across queries
            break

    paths: list[Path] = []
    for media_id, url in results:
        if not url or media_id in used_ids:
            continue
        path = out_dir / f"{media_id}.mp4"
        try:
            _download(url, path)
            paths.append(path)
            used.append(media_id)
        except Exception as exc:
            print(f"Download failed for {media_id}: {exc}")
        if len(paths) >= limit:
            break

    # Every query came up (nearly) empty -- fall back to a broader, generic
    # search (e.g. the channel's niche) so the video still gets more than
    # one clip instead of looping whatever the single weak match was.
    if len(paths) < 2 and fallback_query:
        for media_id, url in _search_one(fallback_query, per_page=limit, target_height=target_height):
            if media_id in used_ids or media_id in seen_ids or not url:
                continue
            path = out_dir / f"{media_id}.mp4"
            try:
                _download(url, path)
                paths.append(path)
                used.append(media_id)
                seen_ids.add(media_id)
            except Exception as exc:
                print(f"Fallback download failed for {media_id}: {exc}")
            if len(paths) >= limit:
                break

    save_json(used_path, used[-1000:])
    if not paths:
        raise RuntimeError("No unused footage found. Configure PEXELS_API_KEY or PIXABAY_API_KEY and retry.")
    return paths


def fetch_music(mood: str, out_dir: Path, used_path: Path) -> Path | None:
    """Spec item '+' Background Music via the Pixabay Music API. Pixabay's
    music endpoint is separate from its image/video search API and has
    changed shape before, so this is intentionally defensive: any failure
    (missing key, endpoint change, no results) just returns None and edit.py
    renders narration-only rather than failing the whole run."""
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    used = load_json(used_path, [])
    used_ids = {str(x) for x in used}
    try:
        data = requests.get("https://pixabay.com/api/music/", params={"key": key, "q": mood, "per_page": 10}, timeout=30).json()
        for track in data.get("hits", []):
            track_id = f"pixabay-music-{track.get('id')}"
            url = track.get("audio") or track.get("audio_url") or ""
            if not url or track_id in used_ids:
                continue
            path = out_dir / f"{track_id}.mp3"
            _download(url, path)
            used.append(track_id)
            save_json(used_path, used[-1000:])
            return path
    except Exception as exc:
        print(f"Pixabay Music unavailable, continuing without background music: {exc}")
    return None
