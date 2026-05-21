# Deploy & operator guide for maya-mcp

Operator-facing setup and deploy instructions. **Not loaded into the
LLM system prompt.** For LLM behavioural rules see `CLAUDE.md` at the
repo root.

This file was extracted from `CLAUDE.md` in chat 51 phase F6a (PR
linked from the same commit) so the per-turn prompt no longer carries
~50 lines of bash/install workflow the LLM never acts on. The F3b adversarial
suite (`tests/golden/maya_queries.jsonl`) is the load-bearing defense
against API misuse; this document is reference for the human installer.

---

## Prerequisites for local models

```bash
# Install Ollama (macOS)
brew install ollama
brew services start ollama

# Pull the model
ollama pull qwen3.5:9b
# On Mac 24GB (fallback):
ollama pull qwen3.5:4b
```

The Qwen3.5 9B model is aliased as `qwen3.5-mcp` via a custom Ollama
Modelfile (num_ctx 16384, temperature 0.7, top_p 0.8, top_k 20) — a
single shared Ollama model for fpt-mcp and maya-mcp on the same machine.
See `MODEL_STRATEGY.md` in the ecosystem root for the full
`ollama create` command and rationale.

---

## Installation location & MCP setup

- **Repository**: `~/Claude_projects/maya-mcp/` (local Mac) or `~/Projects/maya-mcp/`
- **MCP Server**: registered via `claude mcp add -s user maya-mcp -- python -m maya_mcp.server`
- **MCP Configuration**: `~/.claude.json` (via `claude mcp add -s user`)
- **Tool Permissions**: `~/.claude/settings.json`

---

## System requirements

- **macOS Ventura+** with Apple Silicon (Intel supported)
- **Autodesk Maya 2023+** (tested on Maya 2026)
- **Arnold** (`mtoa` plugin, included with Maya 2023+)
- **Python 3.10+** in the system or venv to run `python -m maya_mcp.server`
- **RAG dependencies**: `chromadb`, `sentence-transformers`, `rank-bm25` (optional but recommended)
- **Command Port enabled** in Maya's `userSetup.py`:

```python
# userSetup.py (Maya's script folder)
import maya.cmds as cmds
cmds.commandPort(name=":8100", sourceType="python", echoOutput=False)
```

---

## Building the RAG index

First run downloads the embedding model (~570 MB, cached in `~/.cache/`).

```bash
cd maya-mcp
python -m maya_mcp.rag.build_index
```

Index stored in `src/maya_mcp/rag/index/`. Re-run after adding new docs to the corpus.

---

## Deploy workflow — after every code change

### `src/maya_mcp/server.py` only

```bash
git push
```

Claude Desktop / Claude Code respawns the server automatically on the
next tool call (stdio transport — no manual pkill needed).

### Console panel (`console/` package) changes

```bash
git push
# Reload the Maya panel: MCP Pipeline > Open Console (re-opens with new code)
```

### Configuration

1. Copy `src/maya_mcp/config.example.json` to `src/maya_mcp/config.json`
2. Fill in Ollama URLs (ollama_url for LAN GPU, ollama_mac_url for Mac-local)
3. Restart the MCP server

### Command Port setup (one-time per Maya install)

Add to Maya's `userSetup.py` (typically `~/Library/Preferences/Autodesk/maya/scripts/userSetup.py`):

```python
import maya.cmds as cmds
cmds.commandPort(name=":8100", sourceType="python", echoOutput=False)
```

The auto-setup in `maya_panel.py::_ensure_panel_installed()` injects this
via Command Port on first `maya_ping` / `maya_launch` — no manual editing
needed if using the Console panel.
