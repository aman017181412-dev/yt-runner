from __future__ import annotations
import asyncio
import os
import re
import subprocess
from pathlib import Path
import edge_tts
import requests
from .lib.config import load_config

# ---------------------------------------------------------------------------
# Spec section 10, "AI/Voice Provider Switch": edge-tts -> ElevenLabs ->
# Fish Speech, env/config-driven. edge-tts needs no key and is tried first
# by default; the paid providers only get used if their key is present (and,
# in "auto" mode, only as a fallback if edge-tts's endpoint is unreachable —
# which happens occasionally since it's an unofficial API).
# ---------------------------------------------------------------------------

# A safe per-request text size across all three providers -- long narration
# (e.g. duration_strategy.py decided this niche's competitors run well
# beyond a normal video, like a "sleeping story" channel's hour-long
# uploads) is split and synthesized chunk by chunk rather than trusting one
# very large request -- tens of thousands of characters for an hour of
# narration -- to succeed as a single call on any of these providers.
_CHUNK_CHARS = 1800


def _normalize(raw: Path, output: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-ar", "44100", "-ac", "2", str(output)], check=True, capture_output=True)
    raw.unlink(missing_ok=True)


def _edge_tts(text: str, voice: str, output: Path) -> bool:
    raw = output.with_suffix(".raw.mp3")

    async def run() -> None:
        await edge_tts.Communicate(text, voice).save(str(raw))

    try:
        asyncio.run(run())
        if not raw.exists() or raw.stat().st_size == 0:
            return False
        _normalize(raw, output)
        return True
    except Exception as exc:
        print(f"edge-tts unavailable, trying next voice provider: {exc}")
        raw.unlink(missing_ok=True)
        return False


def _elevenlabs(text: str, voice: str, output: Path) -> bool:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        return False
    voice_id = voice or "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs' default "Rachel" voice
    raw = output.with_suffix(".raw.mp3")
    try:
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": text, "model_id": "eleven_multilingual_v2"},
            timeout=120,
        )
        response.raise_for_status()
        raw.write_bytes(response.content)
        _normalize(raw, output)
        return True
    except Exception as exc:
        print(f"ElevenLabs unavailable, trying next voice provider: {exc}")
        raw.unlink(missing_ok=True)
        return False


def _fish_speech(text: str, voice: str, output: Path) -> bool:
    key = os.getenv("FISH_API_KEY")
    if not key:
        return False
    raw = output.with_suffix(".raw.mp3")
    try:
        payload: dict = {"text": text, "format": "mp3"}
        if voice:
            payload["reference_id"] = voice
        response = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        raw.write_bytes(response.content)
        _normalize(raw, output)
        return True
    except Exception as exc:
        print(f"Fish Speech unavailable: {exc}")
        raw.unlink(missing_ok=True)
        return False


_PROVIDERS = {"edge_tts": _edge_tts, "elevenlabs": _elevenlabs, "fish_speech": _fish_speech}
_ORDER = ["edge_tts", "elevenlabs", "fish_speech"]


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split on paragraph boundaries first, falling back to sentence
    boundaries within an oversized paragraph, so no chunk boundary ever
    lands mid-sentence (which would sound like an audible stumble at the
    seam once chunks are concatenated)."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [text.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(para) <= max_chars:
            current = para
        else:
            current = ""
            for sentence in re.split(r"(?<=[.!?])\s+", para):
                candidate = f"{current} {sentence}".strip()
                if len(candidate) <= max_chars:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = sentence
    if current:
        chunks.append(current)
    return chunks


def _generate_single(text: str, voice: str, output: Path) -> None:
    """One provider-fallback attempt for a single, already chunk-sized
    piece of text."""
    config = load_config()
    provider = config.get("voice_provider", "auto")
    order = [provider] if provider in _PROVIDERS else _ORDER
    for name in order:
        # edge-tts voice names ("en-US-AriaNeural") don't mean anything to
        # ElevenLabs/Fish Speech; only pass a voice id through to those two
        # if it looks like one was explicitly configured for them.
        voice_for_provider = voice if name == "edge_tts" else config.get(f"{name}_voice", "")
        if _PROVIDERS[name](text, voice_for_provider, output):
            return
    raise RuntimeError(
        "No voice provider succeeded. edge-tts should normally work with no key; "
        "otherwise add ELEVENLABS_API_KEY or FISH_API_KEY to yt-runner secrets."
    )


def generate(text: str, voice: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    chunks = _split_text(text, _CHUNK_CHARS)
    if len(chunks) <= 1:
        _generate_single(text, voice, output)
        return

    # Long-form narration: synthesize chunk by chunk and concatenate with
    # ffmpeg. Each chunk is already normalized to the same sample
    # rate/channels by _normalize() inside the provider functions, so a
    # plain stream copy concat (no re-encode) is safe here.
    tmp_dir = output.parent / f"{output.stem}_chunks"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    chunk_paths: list[Path] = []
    try:
        for i, chunk in enumerate(chunks):
            chunk_path = tmp_dir / f"chunk_{i:03d}{output.suffix or '.mp3'}"
            _generate_single(chunk, voice, chunk_path)
            chunk_paths.append(chunk_path)

        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in chunk_paths), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(output)],
            check=True, capture_output=True,
        )
    finally:
        for p in chunk_paths:
            p.unlink(missing_ok=True)
        concat_list_path = tmp_dir / "concat.txt"
        concat_list_path.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass  # non-empty (a chunk failed to clean up) -- not worth failing the run over a leftover temp dir
