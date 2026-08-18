from __future__ import annotations
from .lib.config import KNOWLEDGE_DIR

# ---------------------------------------------------------------------------
# Spec item #25, "Knowledge Base (RAG) — Phase 2": index docs/blog posts
# dropped in knowledge/ into a local ChromaDB collection and retrieve the
# most relevant chunks for the day's topic, for script_writer.py to inject
# into the script prompt.
#
# GitHub Actions runners are ephemeral (a fresh checkout every run), so
# there is nothing to persist between runs anyway -- the index is rebuilt
# in-memory at the start of each run. If knowledge/ is empty, chromadb isn't
# installed, or indexing fails for any reason, query() just returns an empty
# string and script_writer.py proceeds exactly as it did before this
# feature existed.
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 1200
_CHUNK_OVERLAP = 150


def _chunks(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start + _CHUNK_SIZE
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end - _CHUNK_OVERLAP
    return pieces


_collection = None
_attempted = False


def _build_collection():
    global _collection, _attempted
    if _attempted:
        return _collection
    _attempted = True

    if not KNOWLEDGE_DIR.exists():
        return None
    files = sorted(KNOWLEDGE_DIR.glob("*.md")) + sorted(KNOWLEDGE_DIR.glob("*.txt"))
    if not files:
        return None

    try:
        import chromadb
    except ImportError:
        print("chromadb not installed; skipping knowledge-base retrieval.")
        return None

    try:
        client = chromadb.EphemeralClient() if hasattr(chromadb, "EphemeralClient") else chromadb.Client()
        collection = client.get_or_create_collection("yt-core-knowledge")
        documents: list[str] = []
        ids: list[str] = []
        metadatas: list[dict] = []
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for i, chunk in enumerate(_chunks(text)):
                documents.append(chunk)
                ids.append(f"{path.stem}-{i}")
                metadatas.append({"source": path.name})
        if not documents:
            return None
        collection.add(documents=documents, ids=ids, metadatas=metadatas)
        _collection = collection
        return collection
    except Exception as exc:
        print(f"Knowledge-base indexing failed, continuing without RAG context: {exc}")
        return None


def query(text: str, top_k: int = 3) -> str:
    """Return a short block of relevant knowledge-base excerpts for `text`,
    or an empty string if there's no knowledge base / nothing relevant."""
    collection = _build_collection()
    if collection is None:
        return ""
    try:
        result = collection.query(query_texts=[text], n_results=top_k)
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        if not docs:
            return ""
        lines = ["Reference material from the channel's knowledge base:"]
        for doc, meta in zip(docs, metas):
            source = (meta or {}).get("source", "knowledge base")
            snippet = doc.strip().replace("\n", " ")
            if len(snippet) > 600:
                snippet = snippet[:600] + "..."
            lines.append(f"- [{source}] {snippet}")
        return "\n".join(lines)
    except Exception as exc:
        print(f"Knowledge-base query failed, continuing without RAG context: {exc}")
        return ""
