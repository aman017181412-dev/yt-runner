from __future__ import annotations
import os
import random
from typing import Any
import requests
from .lib.config import load_config

# ---------------------------------------------------------------------------
# Spec section 10, "AI/Voice Provider Switch": Groq -> OpenRouter -> Gemini,
# entirely env/config-driven so the provider can change without touching
# code. `provider` in config.json's "llm" block can force one specific
# provider ("groq" | "openrouter" | "gemini"); "auto" (the default) tries
# each provider in that order, for whichever a key is actually present, and
# falls through to the next one if a call fails.
#
# Multi-account key pool: Groq and OpenRouter both rate-limit at the
# account level (Groq: shared RPM/RPD across every key on one account;
# OpenRouter: a per-account daily cap), so several keys from the SAME
# account share one limit and give zero extra headroom. What actually
# multiplies capacity is several keys from SEPARATE accounts. Note: most
# providers' terms of service treat creating multiple accounts specifically
# to bypass rate limits as prohibited and grounds for suspension -- this is
# on the user to weigh, not something this code enforces or denies.
#
# GROQ_API_KEYS / OPENROUTER_API_KEYS (plural, comma-separated) hold that
# pool -- one GitHub secret each, e.g. "key_from_acct1,key_from_acct2,...".
# The singular GROQ_API_KEY / OPENROUTER_API_KEY still work unchanged for a
# single-key setup; plural wins if both are set. Each pipeline run (one
# per channel) picks a random starting point in its pool so parallel
# channels spread across different keys instead of every channel hammering
# key #1 first -- then rotates through the rest of that provider's pool
# before giving up on the provider entirely.
# ---------------------------------------------------------------------------


def _key_pool(env_plural: str, env_singular: str) -> list[str]:
    plural = os.getenv(env_plural, "")
    keys = [k.strip() for k in plural.split(",") if k.strip()]
    if keys:
        return keys
    single = os.getenv(env_singular)
    return [single] if single else []


def _is_rate_limit_error(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429


def _call_with_key_rotation(provider_name: str, keys: list[str], call) -> str | None:
    """Try each key in `keys` (starting from a random offset), calling
    `call(key)` for each. A 429 moves to the next key in the pool; any
    other error is logged and also moves on, since a single bad/expired
    key in a 10-account pool shouldn't take the whole provider down."""
    if not keys:
        return None
    start = random.randrange(len(keys))
    errors: list[str] = []
    for i in range(len(keys)):
        key = keys[(start + i) % len(keys)]
        try:
            return call(key)
        except Exception as exc:
            reason = "rate limited" if _is_rate_limit_error(exc) else str(exc)
            errors.append(f"key#{(start + i) % len(keys) + 1}: {reason}")
            print(f"{provider_name} key #{(start + i) % len(keys) + 1}/{len(keys)} failed, "
                  f"trying next in pool: {reason}")
    raise RuntimeError(f"All {len(keys)} {provider_name} key(s) failed. {'; '.join(errors)}")


def _groq_call(key: str, config: dict[str, Any], system: str, user: str, temperature: float) -> str:
    # Groq deprecated llama-3.1-8b-instant (announced 2026-06-17);
    # openai/gpt-oss-20b is Groq's own recommended replacement. Override via
    # config.json's "llm": {"model": "..."} if this one also gets retired --
    # check console.groq.com/docs/deprecations before assuming key trouble.
    model = config.get("model", "openai/gpt-oss-20b")
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "temperature": temperature, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=90,
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _groq(config: dict[str, Any], system: str, user: str, temperature: float) -> str | None:
    keys = _key_pool("GROQ_API_KEYS", "GROQ_API_KEY")
    return _call_with_key_rotation("Groq", keys, lambda k: _groq_call(k, config, system, user, temperature))


def _openrouter_call(key: str, config: dict[str, Any], system: str, user: str, temperature: float) -> str:
    # OpenRouter's free-model catalog churns weekly -- specific :free slugs
    # (like the old meta-llama/llama-3.1-8b-instruct:free) get delisted
    # without notice, which shows up as a 404 here, not an auth error.
    # "openrouter/free" is OpenRouter's own auto-router: it always resolves
    # to *some* currently-available free model, so it survives that churn
    # instead of breaking every time a specific slug disappears. Override
    # via config.json's "llm": {"openrouter_model": "..."} to pin a
    # specific model instead.
    model = config.get("openrouter_model", "openrouter/free")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "HTTP-Referer": "https://github.com/"},
        json={"model": model, "temperature": temperature, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=90,
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _openrouter(config: dict[str, Any], system: str, user: str, temperature: float) -> str | None:
    keys = _key_pool("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY")
    return _call_with_key_rotation("OpenRouter", keys, lambda k: _openrouter_call(k, config, system, user, temperature))


def _gemini_call(key: str, config: dict[str, Any], system: str, user: str, temperature: float) -> str:
    model = config.get("gemini_model", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(
        url,
        params={"key": key},
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature},
        },
        timeout=90,
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates (possible safety block): {data}")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def _gemini(config: dict[str, Any], system: str, user: str, temperature: float) -> str | None:
    # Gemini is the last-resort fallback and typically needs only one key;
    # GEMINI_API_KEYS (plural) is still honored for symmetry if ever needed.
    keys = _key_pool("GEMINI_API_KEYS", "GEMINI_API_KEY")
    return _call_with_key_rotation("Gemini", keys, lambda k: _gemini_call(k, config, system, user, temperature))


_PROVIDERS = {"groq": _groq, "openrouter": _openrouter, "gemini": _gemini}
_ORDER = ["groq", "openrouter", "gemini"]


def complete(system: str, user: str, *, temperature: float = 0.6) -> str:
    config = load_config().get("llm", {})
    provider = config.get("provider", "auto")
    order = [provider] if provider in _PROVIDERS else _ORDER

    errors: list[str] = []
    for name in order:
        try:
            result = _PROVIDERS[name](config, system, user, temperature)
            if result:
                return result
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            print(f"LLM provider '{name}' failed, trying next: {exc}")

    detail = f" Attempts: {'; '.join(errors)}" if errors else ""
    raise RuntimeError(
        "No LLM provider succeeded. Add GROQ_API_KEY(S), OPENROUTER_API_KEY(S), or GEMINI_API_KEY "
        f"to yt-runner secrets.{detail}"
    )
