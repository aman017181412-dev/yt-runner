from __future__ import annotations
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageEnhance


def _extract_frame(video: Path, out: Path, timestamp: str = "00:00:01") -> Path:
    """Grab a single frame from a video file as a JPEG using ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-y", "-ss", timestamp, "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)],
        check=True, capture_output=True,
    )
    return out


def _draw_variant(base: Image.Image, title: str, style: str, output: Path) -> Path:
    image = base.copy()
    w, h = image.size
    # Coordinates below are ratios of the canvas, not fixed 1280x720 pixels
    # -- this is what makes the same three styles work for both horizontal
    # long-form thumbnails and vertical Shorts thumbnails (see make()).
    font_size = max(28, int(w * 62 / 1280))
    draw = ImageDraw.Draw(image)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()
    words = title[:90]
    if style == "bottom_band":
        od.rectangle((0, int(h * 0.60), w, h), fill=(0, 0, 0, 170))
        image = Image.alpha_composite(image.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(image)
        draw.multiline_text((int(w * 0.043), int(h * 0.674)), words, fill="white", font=font, spacing=8, stroke_width=2, stroke_fill="black")
    elif style == "top_band":
        od.rectangle((0, 0, w, int(h * 0.306)), fill=(0, 0, 0, 170))
        image = Image.alpha_composite(image.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(image)
        draw.multiline_text((int(w * 0.043), int(h * 0.042)), words, fill="white", font=font, spacing=8, stroke_width=2, stroke_fill="black")
    else:  # "punchy" — high-contrast boosted frame with a bold corner caption
        boosted = ImageEnhance.Contrast(image.convert("RGB")).enhance(1.15)
        boosted = ImageEnhance.Color(boosted).enhance(1.2)
        image = boosted.convert("RGBA")
        od.rectangle((0, int(h * 0.778), int(w * 0.594), h), fill=(0, 0, 0, 190))
        image = Image.alpha_composite(image, overlay)
        draw = ImageDraw.Draw(image)
        draw.multiline_text((int(w * 0.031), int(h * 0.813)), words, fill="white", font=font, spacing=6, stroke_width=2, stroke_fill="black")
    image.convert("RGB").save(output, quality=92)
    return output


def make(source: Path, title: str, output: Path, variants: int = 3, preferred_style: str | None = None, size: tuple[int, int] = (1280, 720)) -> list[dict]:
    """Build one or more thumbnail variants (spec item #10). `source` may be
    an image OR a video file (e.g. downloaded stock footage) — video frames
    are extracted with ffmpeg first since PIL cannot open video containers.

    `size` should match the video's own resolution (e.g. (1080, 1920) for a
    vertical Short) -- reusing a fixed 1280x720 horizontal canvas regardless
    of format used to make Shorts thumbnails come out stretched/letterboxed.

    `preferred_style`, when given (from learning.best_thumbnail_style(),
    Future Learning DB), is moved to the front so it becomes outputs[0] --
    the variant run_pipeline.py uploads as the primary thumbnail -- while
    every style still gets generated for the A/B test rotation in
    ab_test.py."""
    output.parent.mkdir(parents=True, exist_ok=True)
    video_suffixes = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
    if source.suffix.lower() in video_suffixes:
        frame_path = output.with_name(output.stem + "_frame.jpg")
        _extract_frame(source, frame_path)
        source = frame_path
    base = Image.open(source).convert("RGB").resize(size)
    all_styles = ["bottom_band", "top_band", "punchy"]
    if preferred_style in all_styles:
        all_styles = [preferred_style] + [s for s in all_styles if s != preferred_style]
    styles = all_styles[: max(1, variants)]
    outputs = []
    for i, style in enumerate(styles):
        name = output if i == 0 else output.with_name(f"{output.stem}_{style}{output.suffix}")
        path = _draw_variant(base, title, style, name)
        outputs.append({"path": path, "style": style})
    return outputs
