from __future__ import annotations
import shutil
from pathlib import Path


def capture(url: str, out_dir: Path, duration_seconds: int = 20, resolution: tuple[int, int] = (1280, 720)) -> Path | None:
    """Spec item #8 Tutorial Screen Recording: record a short headless-browser
    walkthrough of `url` with Playwright for channels whose config sets
    "format_style": "tutorial". This is opt-in and best-effort — if Playwright
    or its browser binary isn't installed (it needs a separate
    `playwright install chromium` step in the workflow), the pipeline just
    falls back to stock footage instead of failing the run."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed; skipping tutorial screen recording.")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    video_dir = out_dir / "_playwright_raw"
    video_dir.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": resolution[0], "height": resolution[1]}, record_video_dir=str(video_dir), record_video_size={"width": resolution[0], "height": resolution[1]})
            page = context.new_page()
            page.goto(url, timeout=20000, wait_until="load")
            steps = max(1, duration_seconds // 4)
            for _ in range(steps):
                page.mouse.wheel(0, 400)
                page.wait_for_timeout(4000)
            video_path_obj = page.video
            context.close()
            browser.close()
            raw_path = Path(video_path_obj.path()) if video_path_obj else None
        if not raw_path or not raw_path.exists():
            return None
        final = out_dir / "tutorial_capture.mp4"
        shutil.move(str(raw_path), str(final))
        shutil.rmtree(video_dir, ignore_errors=True)
        return final
    except Exception as exc:
        print(f"Tutorial screen recording failed, continuing without it: {exc}")
        return None
