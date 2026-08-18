from __future__ import annotations
import importlib.util
from typing import Any, Callable
from .config import PLUGINS_DIR

# ---------------------------------------------------------------------------
# Spec section 10, "Plugin Architecture": let niche/channel-specific behavior
# live as small, dropped-in Python files instead of being hard-coded into the
# pipeline scripts. A plugin is any *.py file directly under PLUGINS_DIR
# (not starting with "_") that defines one or more of the hook functions
# below. Nothing here is required -- an empty plugins/ folder is the default
# and every hook call is a no-op in that case.
#
# Supported hooks (all optional, all best-effort):
#   extra_research(niche: str) -> list[dict]
#       Return additional topic candidates in the same shape as
#       research.collect() rows: {"title", "url", "source"}.
#   post_process_script(script_data: dict, topic: dict) -> dict
#       Return a (possibly modified) script_data dict, run right after the
#       fact-check pass.
#   post_process_seo(seo_metadata: dict, topic: dict) -> dict
#       Return a (possibly modified) seo_metadata dict.
#   before_upload(video_path: str, metadata: dict, channel: str) -> None
#       Side-effect only (e.g. an extra validation/log step); return value is
#       ignored.
# ---------------------------------------------------------------------------

_HOOK_NAMES = ("extra_research", "post_process_script", "post_process_seo", "before_upload")


def _discover() -> dict[str, list[Callable[..., Any]]]:
    registry: dict[str, list[Callable[..., Any]]] = {name: [] for name in _HOOK_NAMES}
    if not PLUGINS_DIR.exists():
        return registry
    for path in sorted(PLUGINS_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"yt_core_plugin_{path.stem}", path)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            print(f"Plugin '{path.name}' failed to load, skipping: {exc}")
            continue
        for hook in _HOOK_NAMES:
            fn = getattr(module, hook, None)
            if callable(fn):
                registry[hook].append(fn)
    return registry


_REGISTRY = None


def _registry() -> dict[str, list[Callable[..., Any]]]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _discover()
    return _REGISTRY


def run_collecting(hook: str, *args: Any, **kwargs: Any) -> list[Any]:
    """Call every plugin registered for `hook`, collecting each successful
    return value into a list (used for extra_research, which contributes
    additional rows rather than replacing anything)."""
    results: list[Any] = []
    for fn in _registry().get(hook, []):
        try:
            value = fn(*args, **kwargs)
            if value:
                results.append(value)
        except Exception as exc:
            print(f"Plugin hook '{hook}' ({fn.__module__}) failed, ignoring: {exc}")
    return results


def run_chained(hook: str, value: Any, *args: Any, **kwargs: Any) -> Any:
    """Call every plugin registered for `hook` in sequence, passing each
    plugin's return value as the input `value` to the next one (used for
    post_process_script / post_process_seo)."""
    for fn in _registry().get(hook, []):
        try:
            result = fn(value, *args, **kwargs)
            if result is not None:
                value = result
        except Exception as exc:
            print(f"Plugin hook '{hook}' ({fn.__module__}) failed, ignoring: {exc}")
    return value


def run_side_effect(hook: str, *args: Any, **kwargs: Any) -> None:
    """Call every plugin registered for `hook`, ignoring return values."""
    for fn in _registry().get(hook, []):
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            print(f"Plugin hook '{hook}' ({fn.__module__}) failed, ignoring: {exc}")
