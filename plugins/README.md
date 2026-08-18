# Plugins

Drop any `*.py` file directly in this folder (filenames not starting with
`_`) to extend the pipeline without touching `scripts/`. Every function
below is optional and best-effort — a plugin that raises an exception is
logged and skipped, it never fails the pipeline run.

```python
# plugins/my_plugin.py

def extra_research(niche: str) -> list[dict]:
    """Add extra topic candidates alongside the built-in research sources.
    Return rows shaped like {"title": str, "url": str, "source": str}."""
    return []

def post_process_script(script_data: dict, topic: dict) -> dict:
    """Runs after the fact-check pass. Return the (optionally modified)
    script_data dict."""
    return script_data

def post_process_seo(seo_metadata: dict, topic: dict) -> dict:
    """Return the (optionally modified) SEO metadata dict."""
    return seo_metadata

def before_upload(video_path: str, metadata: dict, channel: str) -> None:
    """Side-effect only, e.g. an extra validation or logging step."""
    pass
```

See `_example_plugin.py` for a working (but disabled — the leading
underscore keeps the loader from picking it up) reference implementation.
Rename it (drop the underscore) to activate it.
