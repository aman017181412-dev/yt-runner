from __future__ import annotations
from pathlib import Path


def _format_srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(audio: Path, output: Path, max_chars_per_caption: int = 42) -> Path | None:
    """Spec item #9 subtitle timing: transcribe the generated narration with
    faster-whisper (local, free, word-level timestamps) and group words into
    short on-screen captions. Returns None (no subtitles burned in) if
    faster-whisper isn't available or transcription fails — the video still
    renders fine without captions."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper not installed; skipping subtitles.")
        return None
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(audio), word_timestamps=True)
        words = [w for segment in segments for w in (segment.words or [])]
        if not words:
            return None
        captions: list[tuple[float, float, str]] = []
        buf_words: list = []
        for word in words:
            buf_words.append(word)
            text = " ".join(w.word for w in buf_words).strip()
            if len(text) >= max_chars_per_caption or word is words[-1]:
                captions.append((buf_words[0].start, buf_words[-1].end, text))
                buf_words = []
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as fh:
            for i, (start, end, text) in enumerate(captions, start=1):
                fh.write(f"{i}\n{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n{text.strip()}\n\n")
        return output
    except Exception as exc:
        print(f"Subtitle generation failed, continuing without captions: {exc}")
        return None
