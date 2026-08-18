from __future__ import annotations
import base64
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .analytics import fetch_video_metrics
from .lib.config import all_channels, load_config, load_json, save_json
from .youtube import set_thumbnail, update_title

# ---------------------------------------------------------------------------
# Spec item #23, "A/B Testing": YouTube's Studio "Test & compare" feature has
# no public API, so there is no way to register a real native A/B test
# through YouTube Data API. This is the practical substitute: automatically
# rotate the live thumbnail through the generated variants on a fixed
# schedule, record the view count at each swap, and once every variant has
# had a turn, leave the best-performing one live.
#
# Generated media (including thumbnails) is never committed to git -- see
# .gitignore and spec section 8 -- and GitHub Actions runners are ephemeral,
# so a variant image from the upload job would not exist anymore by the time
# a later hourly trigger-window job needs to swap to it. To make rotation
# actually work across runs, the (small, resized) variant images themselves
# are embedded as base64 directly inside ab_tests.json, which IS committed.
# ---------------------------------------------------------------------------

ROTATE_AFTER_HOURS = 48  # how long each variant gets before the next swap
_MAX_STORAGE_DIM = 720  # longest side, aspect ratio preserved -- see _encode_variant()


def _encode_variant(path: Path) -> str:
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        w, h = img.size
        # Aspect ratio must be preserved here: thumb_variants can now be
        # either horizontal (long-form, 1280x720) or vertical (Shorts,
        # 1080x1920 -- see thumbnail.py/run_pipeline.py). A fixed 640x360
        # target used to squash every vertical Shorts thumbnail into a
        # stretched 16:9 shape when it was later re-applied during rotation.
        scale = min(1.0, _MAX_STORAGE_DIM / max(w, h))
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78)
        return base64.b64encode(buf.getvalue()).decode("ascii")


def register(folder: Path, video_id: str, thumb_variants: list[dict], titles: list[str] | None = None) -> None:
    """Call right after upload() with the thumbnail variants make_thumbnails()
    produced (each a {"path": Path, "style": str} dict). Fewer than 2
    variants means there's nothing to test.

    `titles`, when given (the primary title plus seo.py's title_variants),
    is paired one-to-one with the thumbnail variants (cycling if there are
    fewer titles than thumbnails) -- each rotation swap below then changes
    thumbnail AND title together, since both drive the same CTR metric and
    testing them independently would need twice the traffic to reach a
    result."""
    existing = [v for v in thumb_variants if Path(v["path"]).exists()]
    if len(existing) < 2:
        return
    try:
        variants = []
        for i, v in enumerate(existing):
            entry = {"style": v["style"], "image_b64": _encode_variant(Path(v["path"]))}
            if titles:
                entry["title"] = titles[i % len(titles)]
            variants.append(entry)
    except Exception as exc:
        print(f"Could not prepare thumbnail A/B test (continuing without it): {exc}")
        return

    path = folder / "ab_tests.json"
    tests = load_json(path, [])
    tests.append({
        "video_id": video_id,
        "variants": variants,
        "current_index": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_switch_at": datetime.now(timezone.utc).isoformat(),
        "history": [],
        "status": "running",
    })
    save_json(path, tests[-50:])


def _due(test: dict[str, Any]) -> bool:
    try:
        last = datetime.fromisoformat(test["last_switch_at"])
    except (KeyError, ValueError):
        return True
    return datetime.now(timezone.utc) - last >= timedelta(hours=ROTATE_AFTER_HOURS)


def _apply(video_id: str, channel: str, variant: dict[str, Any]) -> bool:
    ok = True
    try:
        raw = base64.b64decode(variant["image_b64"])
        tmp = Path(f"/tmp/ab_thumb_{video_id}.jpg")
        tmp.write_bytes(raw)
        set_thumbnail(video_id, channel, tmp)
        tmp.unlink(missing_ok=True)
    except Exception as exc:
        print(f"Could not apply A/B test thumbnail for {video_id}: {exc}")
        ok = False
    if variant.get("title") and not update_title(video_id, channel, variant["title"]):
        ok = False
    return ok


def run_rotation() -> None:
    """Called from the hourly trigger window (spec section 4): advance any
    thumbnail A/B test whose current variant has had its scheduled turn."""
    config = load_config()
    for channel, _channel_data, folder in all_channels(config):
        path = folder / "ab_tests.json"
        tests = load_json(path, [])
        if not tests:
            continue
        changed = False
        for test in tests:
            if test.get("status") != "running" or not _due(test):
                continue
            metrics = fetch_video_metrics(test["video_id"], channel) or {}
            test["history"].append({
                "variant_style": test["variants"][test["current_index"]]["style"],
                "views_at_switch": metrics.get("views"),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            })
            next_index = test["current_index"] + 1
            if next_index >= len(test["variants"]):
                best = max(test["history"], key=lambda h: h.get("views_at_switch") or 0)
                test["status"] = "complete"
                test["winner"] = best["variant_style"]
                winner = next((v for v in test["variants"] if v["style"] == best["variant_style"]), test["variants"][0])
                _apply(test["video_id"], channel, winner)
            else:
                test["current_index"] = next_index
                test["last_switch_at"] = datetime.now(timezone.utc).isoformat()
                _apply(test["video_id"], channel, test["variants"][next_index])
            changed = True
        if changed:
            save_json(path, tests)
