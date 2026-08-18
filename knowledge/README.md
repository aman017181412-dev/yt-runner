# Knowledge base (RAG)

Drop `.md` or `.txt` reference documents (style guides, past blog posts,
niche fact sheets, brand voice notes) directly in this folder. `rag.py`
indexes everything here into a local, in-memory ChromaDB collection at the
start of each pipeline run and retrieves the most relevant chunks for the
day's topic, which `script_writer.py` injects into the script prompt.

This folder is empty by default — the pipeline runs fine with nothing in
it; `rag.query()` just returns no context and script generation proceeds
as before. Nothing here needs to be structured in any special way; plain
prose files work.

Large files are automatically split into ~1200-character chunks before
indexing. Since the index is rebuilt fresh every run (GitHub Actions
runners are ephemeral, so nothing would persist between runs anyway), keep
this folder to a reasonable size — a few dozen documents is fine, hundreds
of large files will slow every run down.
