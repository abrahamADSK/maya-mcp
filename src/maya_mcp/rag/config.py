"""
rag/config.py
=============
Shared constants for the RAG pipeline.

IMPORTANT: EMBEDDING_MODEL must be consistent across build_index.py (write)
and search.py (read). If you change it here, delete rag/index/ and rebuild.
"""

# -- Embedding model selection ------------------------------------------------
# IMPORTANT: build and query MUST use the same model. If you change this,
# delete rag/index/ and run python -m maya_mcp.rag.build_index to rebuild.
#
# Option A — bge-small-en-v1.5 (~130 MB): fast, good for semantic queries
# Option B — bge-large-en-v1.5 (~570 MB): higher accuracy on technical code queries
# Option C — nomic-embed-text-v1.5 (~270 MB): strong code + natural language mix
#
# bge-large for better recall on exact Maya API method/flag names.
EMBEDDING_MODEL = "BAAI/bge-large-en-v1.5"

# -- Hybrid search parameters ------------------------------------------------
# BM25_CANDIDATES: how many candidates each retriever fetches before RRF fusion
# RRF_K: RRF damping constant (higher = less aggressive rank compression; 60 is standard)
BM25_CANDIDATES = 20
RRF_K = 60

# -- Collections --------------------------------------------------------------
# Single collection for all Maya API docs.
# Metadata includes 'api' field to identify the source.
COLLECTION_NAME = "maya_docs"

# -- Chunking -----------------------------------------------------------------
METHOD_GROUP_SIZE = 4       # methods per sub-chunk in API docs
METHOD_GROUP_THRESHOLD = 8  # min methods to trigger sub-chunking
CHUNK_SPLIT_THRESHOLD = 700 # min section chars to trigger sub-chunking
MIN_CHUNK_CHARS = 80        # skip chunks shorter than this

# -- Token tracking -----------------------------------------------------------
# Combined size of all indexed docs in tokens (baseline for RAG savings display).
# CMDS_API ~5000 + PYMEL_API ~3000 + ARNOLD_API ~2500 + USD_API ~2000 + ANTI_PATTERNS ~1500 = ~14000
FULL_DOC_TOKENS = 14000
