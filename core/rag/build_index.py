"""
build_index.py
==============
Indexes Maya API documentation into a local ChromaDB vector database.
Run once after installation, and again whenever docs change.

Usage:
    cd maya-mcp-project
    python -m core.rag.build_index

What it indexes:
    - docs/CMDS_API.md         (maya.cmds reference + common patterns)
    - docs/PYMEL_API.md        (PyMEL object-oriented API reference)
    - docs/ARNOLD_API.md       (Arnold/mtoa shaders, AOVs, render settings)
    - docs/USD_API.md          (Maya-USD integration — stages, prims, export)
    - docs/ANTI_PATTERNS.md    (Common LLM hallucinations + wrong flag names)
    - Any additional .md in docs/

The index is stored in rag/index/ and committed to git so users
get a ready-to-use index without rebuilding.

Chunking strategy
-----------------
Split on ## headers — one chunk per section. For API reference sections
that list many methods/flags, further split into groups of
METHOD_GROUP_SIZE to prevent large sections from burying specific names
in retrieval noise.

First run downloads the embedding model (~570 MB from HuggingFace, once).
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# -- Paths --------------------------------------------------------------------
_RAG_DIR = Path(__file__).parent
_CORE_DIR = _RAG_DIR.parent
INDEX_DIR = str(_RAG_DIR / "index")
DOCS_DIR = str(_CORE_DIR / "docs")

# Documents to index — processed in order
PRIMARY_DOCS = [
    os.path.join(DOCS_DIR, "CMDS_API.md"),
    os.path.join(DOCS_DIR, "PYMEL_API.md"),
    os.path.join(DOCS_DIR, "ARNOLD_API.md"),
    os.path.join(DOCS_DIR, "USD_API.md"),
    os.path.join(DOCS_DIR, "ANTI_PATTERNS.md"),
]

# Map doc filename to API category for metadata
_API_TAG = {
    "CMDS_API.md": "maya_cmds",
    "PYMEL_API.md": "pymel",
    "ARNOLD_API.md": "arnold",
    "USD_API.md": "usd",
    "ANTI_PATTERNS.md": "anti_patterns",
}

# -- Chunking config (from config.py) ----------------------------------------
METHOD_BULLET = re.compile(r"^- `\w", re.MULTILINE)


def _load_config():
    from core.rag.config import (
        METHOD_GROUP_SIZE,
        METHOD_GROUP_THRESHOLD,
        CHUNK_SPLIT_THRESHOLD,
        MIN_CHUNK_CHARS,
    )
    return METHOD_GROUP_SIZE, METHOD_GROUP_THRESHOLD, CHUNK_SPLIT_THRESHOLD, MIN_CHUNK_CHARS


# -- Chunking -----------------------------------------------------------------

def _method_group_chunks(
    section: str, source: str, api: str, section_idx: int,
    group_size: int, min_chars: int,
) -> list[dict]:
    """Split an API section listing many methods into groups."""
    header_match = re.match(r"^#{1,3} (.+)", section)
    header = header_match.group(1).strip() if header_match else f"section_{section_idx}"
    section_name = section.split("\n")[0]

    first_method = METHOD_BULLET.search(section)
    if not first_method:
        return []

    intro = section[: first_method.start()].rstrip()
    methods_text = section[first_method.start() :]

    entries = re.split(r"(?m)(?=^- `\w)", methods_text)
    entries = [e.strip() for e in entries if e.strip()]

    groups = [entries[i : i + group_size] for i in range(0, len(entries), group_size)]
    chunks = []

    if groups:
        if intro.strip() and len(intro.strip()) < 150:
            first_text = intro.strip() + "\n\n" + "".join(groups[0]).strip()
            chunks.append({
                "id": f"{source}::{section_idx}::{header[:40]}::g0",
                "text": first_text,
                "metadata": {"source": source, "section": header, "api": api},
            })
            groups = groups[1:]
        elif intro.strip():
            chunks.append({
                "id": f"{source}::{section_idx}::{header[:40]}::intro",
                "text": intro.strip(),
                "metadata": {"source": source, "section": header, "api": api},
            })

    for g_idx, group in enumerate(groups):
        group_text = section_name + "\n\n" + "".join(group).strip()
        if len(group_text.strip()) >= min_chars:
            chunks.append({
                "id": f"{source}::{section_idx}::{header[:40]}::g{g_idx + 1}",
                "text": group_text,
                "metadata": {"source": source, "section": header, "api": api},
            })

    return chunks


def chunk_markdown(text: str, source: str, api: str = "") -> list[dict]:
    """Split a markdown file into chunks by ## headers, with method sub-chunking."""
    group_size, group_threshold, split_threshold, min_chars = _load_config()

    chunks = []
    sections = re.split(r"\n(?=#{1,3} )", text)

    for i, section in enumerate(sections):
        section = section.strip()
        if len(section) < min_chars:
            continue

        header_match = re.match(r"^#{1,3} (.+)", section)
        header = header_match.group(1).strip() if header_match else f"section_{i}"

        method_count = len(METHOD_BULLET.findall(section))
        should_split = (
            method_count >= group_threshold and len(section) >= split_threshold
        )

        if should_split:
            sub = _method_group_chunks(section, source, api, i, group_size, min_chars)
            if sub:
                chunks.extend(sub)
                continue

        chunks.append({
            "id": f"{source}::{i}::{header[:40]}",
            "text": section,
            "metadata": {"source": source, "section": header, "api": api},
        })

    return chunks


def collect_docs() -> list[tuple[str, str]]:
    """Return all (path, api_tag) tuples to index."""
    docs = []
    for p in PRIMARY_DOCS:
        if os.path.isfile(p):
            fname = os.path.basename(p)
            api = _API_TAG.get(fname, "general")
            docs.append((p, api))
        else:
            print(f"  [warn] not found: {p}")

    # Index any extra .md in docs/
    if os.path.isdir(DOCS_DIR):
        known = {os.path.basename(p) for p, _ in docs}
        for fname in sorted(os.listdir(DOCS_DIR)):
            if not fname.endswith(".md") or fname in known:
                continue
            api = _API_TAG.get(fname, "general")
            docs.append((os.path.join(DOCS_DIR, fname), api))

    return docs


# -- Embedding ----------------------------------------------------------------

def _make_embedding_fn() -> Any:
    """Returns a ChromaDB-compatible embedding function using BGE model."""
    from core.rag.config import EMBEDDING_MODEL

    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        print(f"  Embedding model : {EMBEDDING_MODEL}")
        print(f"  (downloads ~570 MB from HuggingFace on first run — cached afterwards)")
        fn = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
        fn(["probe"])  # warm-up
        print(f"  Embedding model : ready")
        return fn
    except ImportError:
        print("  ERROR: sentence-transformers not installed.")
        print("  Run:   pip install sentence-transformers chromadb")
        sys.exit(1)


# -- Main ---------------------------------------------------------------------

def build() -> None:
    try:
        import chromadb
    except ImportError:
        print("ERROR: chromadb not installed.\nRun: pip install chromadb")
        sys.exit(1)

    from core.rag.config import COLLECTION_NAME

    print(f"Building RAG index in: {INDEX_DIR}")
    os.makedirs(INDEX_DIR, exist_ok=True)

    embedding_fn = _make_embedding_fn()

    client = chromadb.PersistentClient(path=INDEX_DIR)

    # Fresh rebuild
    try:
        client.delete_collection(COLLECTION_NAME)
        print("  Deleted existing collection.")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )

    all_chunks: list[dict] = []
    for doc_path, api_tag in collect_docs():
        with open(doc_path, "r", encoding="utf-8") as f:
            text = f.read()
        source = os.path.basename(doc_path)
        chunks = chunk_markdown(text, source, api=api_tag)
        all_chunks.extend(chunks)

        method_chunks = sum(1 for c in chunks if "::g" in c["id"])
        api_label = f" [{api_tag}]" if api_tag else ""
        if method_chunks:
            print(f"  {source}{api_label}: {len(chunks)} chunks ({method_chunks} method-group)")
        else:
            print(f"  {source}{api_label}: {len(chunks)} chunks")

    if not all_chunks:
        print("No chunks to index — ensure docs/ directory has .md files.")
        return

    # Deduplicate
    seen = set()
    deduped = []
    for c in all_chunks:
        if c["id"] not in seen:
            seen.add(c["id"])
            deduped.append(c)
    if len(deduped) < len(all_chunks):
        print(f"  [warn] Removed {len(all_chunks) - len(deduped)} duplicate chunk ids.")
    all_chunks = deduped

    collection.add(
        ids=[c["id"] for c in all_chunks],
        documents=[c["text"] for c in all_chunks],
        metadatas=[c["metadata"] for c in all_chunks],
    )

    # BM25 corpus
    corpus_path = str(_RAG_DIR / "corpus.json")
    corpus = [{"id": c["id"], "text": c["text"], "metadata": c["metadata"]} for c in all_chunks]
    with open(corpus_path, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  BM25 corpus saved: {len(corpus)} chunks -> rag/corpus.json")

    # Stats
    avg_chars = sum(len(c["text"]) for c in all_chunks) // len(all_chunks)
    max_chunk = max(all_chunks, key=lambda c: len(c["text"]))
    print(f"\nDone. {len(all_chunks)} chunks indexed.")
    print(f"  avg chunk size : {avg_chars} chars")
    print(f"  largest chunk  : {len(max_chunk['text'])} chars  ({max_chunk['id'][:60]})")
    print(f"Index location: {INDEX_DIR}")

    # Per-API breakdown
    api_counts: dict[str, int] = {}
    for c in all_chunks:
        api = c["metadata"].get("api", "unknown")
        api_counts[api] = api_counts.get(api, 0) + 1
    print("\nChunks per API:")
    for api, count in sorted(api_counts.items()):
        print(f"  {api}: {count}")


if __name__ == "__main__":
    build()
