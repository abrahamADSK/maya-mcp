# Maya MCP — RAG Implementation Plan

**Goal**: Bring maya-mcp to architectural parity with fpt-mcp and flame-mcp.
Consistent module structure, same patterns, adapted for the Maya domain.

## Architecture (mirrors fpt-mcp exactly)

```
core/
  rag/
    __init__.py        — Package docstring
    config.py          — Shared constants (embedding model, BM25, RRF, chunking, token tracking)
    build_index.py     — Chunk markdown docs → ChromaDB + corpus.json (BM25)
    search.py          — Hybrid search: BM25 + semantic (HyDE) + RRF fusion + cache
  docs/
    CMDS_API.md        — maya.cmds reference (commands, flags, common patterns)
    PYMEL_API.md       — PyMEL reference (nodes, attributes, common patterns)
    ARNOLD_API.md      — Arnold/mtoa reference (shaders, AOVs, render settings)
    USD_API.md         — Maya-USD reference (stages, prims, export/import)
    ANTI_PATTERNS.md   — Common hallucinations and wrong flag names
  safety.py            — Maya-specific dangerous pattern detection
  server.py            — + 3 RAG tools + token tracking + check_dangerous()
```

## Files to Create

### 1. core/rag/__init__.py
Package docstring only. Mirrors fpt-mcp/rag/__init__.py.

### 2. core/rag/config.py
- EMBEDDING_MODEL = "BAAI/bge-large-en-v1.5" (same as fpt-mcp)
- BM25_CANDIDATES = 20, RRF_K = 60
- COLLECTION_NAME = "maya_docs"
- METHOD_GROUP_SIZE = 4, METHOD_GROUP_THRESHOLD = 8
- CHUNK_SPLIT_THRESHOLD = 700, MIN_CHUNK_CHARS = 80
- FULL_DOC_TOKENS = estimated after docs written

### 3. core/rag/build_index.py
Identical structure to fpt-mcp. PRIMARY_DOCS maps to Maya docs.
API_TAG = {"CMDS_API.md": "maya_cmds", "PYMEL_API.md": "pymel", ...}

### 4. core/rag/search.py
- Lazy singletons for ChromaDB + BM25
- In-session cache (_search_cache)
- Adaptive HyDE with Maya-domain templates:
  - cmds domain: `import maya.cmds as cmds` template
  - pymel domain: `import pymel.core as pm` template
  - Arnold domain: `import mtoa.core` template
  - USD domain: `from pxr import Usd, UsdGeom` template
- RRF fusion (identical algorithm)
- search() → (text, max_relevance)

### 5. core/docs/ (Maya API corpus)
- CMDS_API.md: maya.cmds reference — polyCube, polyExtrude, xform, file, etc.
- PYMEL_API.md: PyMEL object-oriented API
- ARNOLD_API.md: Arnold mtoa shaders, AOVs, render settings
- USD_API.md: Maya-USD integration
- ANTI_PATTERNS.md: Common LLM hallucinations (wrong flag names, deprecated cmds)

### 6. core/safety.py
Maya-specific patterns:
- `cmds.file(new=True, force=True)` without save prompt
- `cmds.delete("*")` or unfiltered bulk delete
- `cmds.undoInfo(stateWithoutFlush=0)` disabling undo
- `os.remove` / `shutil.rmtree` on scene files
- `cmds.polyReduce` on referenced geometry
- `cmds.namespace(removeNamespace=...)` force
- `cmds.lockNode` on critical nodes
- `mel.eval("source ...")` from untrusted paths
- Path traversal in file operations
- Plugin deregistration (`cmds.unloadPlugin`)

### 7. server.py modifications
- Add imports: safety.check_dangerous, rag.search.search
- Add _stats dict + _tok() + _rating() (token tracking)
- Add _stats_reset_at, _last_rag_score, _rag_called_this_session
- Add model trust gates (WRITE_ALLOWED_MODELS, _model_can_write)
- Add 3 new tools: search_maya_docs, learn_pattern, session_stats
- Add check_dangerous() to maya_execute_python, maya_mesh_operation, maya_delete
- Add token tracking to ALL existing tools
- Update mcp = FastMCP("maya_mcp", instructions=...) with RAG workflow

## Translation (English)
After RAG implementation, translate ALL Spanish strings:
- server.py: docstrings, comments, error messages, field descriptions
- maya_bridge.py: docstrings, comments
- CLAUDE.md: entire file
- console/*.py: docstrings, comments
- README.md: remaining Spanish lines

## Sequence
1. Save this plan ← DONE
2. Create core/rag/ module (config, build_index, search)
3. Create core/docs/ corpus
4. Create core/safety.py
5. Integrate into server.py (3 tools + tracking + safety)
6. Translate everything to English
7. Verify consistency
