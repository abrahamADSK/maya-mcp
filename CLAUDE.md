# maya-mcp — Critical Context for Claude

> **Last updated**: 2026-03-31
> This document persists across Claude Code sessions. Consult here to understand the architecture, configuration, and workflows of maya-mcp.

---

## 1. Architecture

**maya-mcp** is a production-grade **MCP (Model Context Protocol)** server based on **FastMCP** with **27 tools** organized in three layers:

1. **Maya Control** (18 tools) — Scene manipulation, modeling, animation, I/O, rendering
   - Communicates with Maya via **TCP Command Port** (default port 7001)
   - Uses `maya_bridge.py` (socket bridge) to execute MEL/Python commands
   - All operations use undo chunks for safe rollback

2. **Vision3D Integration** (6 tools) — Optional addon for AI-powered 3D generation via [Vision3D](https://github.com/abrahamADSK/vision3d)
   - Communicates via **HTTP REST API** with Vision3D (port 8000)
   - Supports image-to-3D, text-to-3D, and texture painting
   - Non-blocking async pattern: submit → poll → download
   - **Not required** — maya-mcp works fully without Vision3D

3. **RAG & Intelligence** (3 tools) — Documentation search, self-learning, analytics
   - Hybrid search: ChromaDB semantic + BM25 lexical, fused via RRF
   - HyDE adaptive query expansion for 5 Maya API domains
   - Anti-hallucination safety layer (14+ dangerous patterns)
   - Model trust gates for self-learning patterns
   - Token tracking with efficiency reporting

```
┌──────────────────┐
│   Claude Code    │
└────────┬─────────┘
         │ (MCP Protocol — stdio)
┌────────▼──────────────────────────────────────────┐
│   maya-mcp FastMCP Server (27 tools)              │
│                                                    │
│  ┌─────────┐ ┌─────────┐ ┌──────────────────────┐│
│  │ RAG     │ │ Safety  │ │ Token Tracking       ││
│  │ Engine  │ │ Module  │ │ + Model Trust Gates  ││
│  └────┬────┘ └────┬────┘ └──────────────────────┘│
│       │           │                                │
├───────┼───────────┼────────────────────────────────┤
│  Maya Bridge (TCP)     Vision3D REST Client        │
└────┬───────────────────────┬──────────────────────┘
     │ :7001 Command Port    │ HTTP :8000
     │                       │
┌────▼──────────────┐   ┌───▼──────────────────┐
│ Autodesk Maya     │   │ Vision3D GPU Server  │
│ (local Mac)       │   │ Hunyuan3D-2          │
└───────────────────┘   └──────────────────────┘
```

---

## 2. Key Features

### RAG-Powered Documentation Search
`search_maya_docs` provides hybrid search across 5 curated corpora (maya.cmds, PyMEL, Arnold/mtoa, Maya-USD, anti-patterns). Uses ChromaDB for semantic similarity + BM25 for exact API name matching, fused via Reciprocal Rank Fusion. The LLM should call this BEFORE writing any unfamiliar Maya commands.

### HyDE (Hypothetical Document Embedding)
Short queries like "set keyframe tangent" are automatically expanded with domain-specific code templates before embedding. The system detects which Maya API domain the query targets (cmds, PyMEL, Arnold, USD, MEL) and uses the appropriate template.

### Anti-Hallucination Safety Layer
`safety.py` scans code for 14+ dangerous patterns before execution: bulk deletes, undo tampering, filesystem operations, plugin deregistration, namespace force-deletion, etc. Each pattern includes an explanation and safe alternative. Integrated into `maya_execute_python`, `maya_delete`, and other mutation tools.

### Self-Learning Patterns
`learn_pattern` saves validated working patterns to the docs corpus. Model trust gates: only Sonnet/Opus write directly; other models stage candidates for review in `rag/candidates.json`.

### Token Efficiency Tracking
`session_stats` reports tokens used vs saved by RAG, safety blocks, patterns learned, cache hits, and full-doc baseline comparison.

---

## 3. Execution Environment

### Location
- **Repository**: `~/Claude_projects/maya-mcp/` (local Mac)
- **MCP Server**: runs with `python core/server.py` (standard MCP stdio transport)
- **MCP Configuration**: `~/.claude.json` (via `claude mcp add -s user`)
- **Tool Permissions**: `~/.claude/settings.json`

### Environment Variables (`.env`)
```bash
MAYA_HOST=localhost          # Host where Maya is running
MAYA_PORT=7001              # Command Port
GPU_API_URL=http://gpu-host:8000  # Vision3D HTTP endpoint
GPU_API_KEY=                      # Leave empty for open LAN access
```

### Requirements
- **macOS Ventura+** with Apple Silicon (Intel support available)
- **Autodesk Maya 2023+** (tested on 2026)
- **Arnold** (`mtoa` plugin, included with Maya)
- **Python 3.10+** to run `core/server.py`
- **RAG dependencies**: `chromadb`, `sentence-transformers`, `rank-bm25` (optional but recommended)
- **Command Port enabled** in Maya's `userSetup.py`

---

## 4. Available Tools (27 total)

### Maya Tools (18 tools)

| Tool | Description |
|------|-------------|
| `maya_launch` | Opens Maya and waits for Command Port to respond (max 90s) |
| `maya_ping` | Verifies connection, returns version, current scene, renderer |
| `maya_create_primitive` | Creates 3D primitives (cube, sphere, cylinder, cone, plane, torus) |
| `maya_assign_material` | Creates and assigns material (lambert, blinn, phong, aiStandardSurface) |
| `maya_transform` | Moves, rotates, scales objects in world/object space |
| `maya_list_scene` | Lists scene objects with filters by type or name |
| `maya_delete` | Deletes objects (with safety checks on wildcards) |
| `maya_create_light` | Creates lights (directional, point, spot, area, ambient) |
| `maya_create_camera` | Creates camera with focal length and look-at target |
| `maya_execute_python` | Executes arbitrary Python in Maya (with safety scanning) |
| `maya_new_scene` | Creates new empty scene |
| `maya_save_scene` | Saves current scene |
| `maya_mesh_operation` | Extrude, bevel, boolean (union/diff/intersect), combine, separate, smooth |
| `maya_set_keyframe` | Keyframe any attribute with tangent control |
| `maya_import_file` | Import OBJ, FBX, GLB/GLTF, Alembic, MA/MB with namespace and scale |
| `maya_viewport_capture` | Playblast screenshot to PNG/JPG at any resolution |
| `maya_scene_snapshot` | Full scene state: file, renderer, counts, plugins, units |
| `maya_shelf_button` | Create shelf buttons with custom Python commands |

### Vision3D Tools (6 tools — optional addon, requires [Vision3D](https://github.com/abrahamADSK/vision3d))

| Tool | Description |
|------|-------------|
| `vision3d_health` | Checks GPU server availability, models, text-to-3D status |
| `shape_generate_remote` | Image-to-3D generation (non-blocking, returns job_id) |
| `shape_generate_text` | Text-to-3D generation (non-blocking, returns job_id) |
| `texture_mesh_remote` | Texture existing mesh (non-blocking, returns job_id) |
| `vision3d_poll` | Poll job status with incremental log lines |
| `vision3d_download` | Download completed results to local directory |

### RAG & Intelligence Tools (3 tools)

| Tool | Description |
|------|-------------|
| `search_maya_docs` | Hybrid RAG search across 5 Maya API corpora (semantic + BM25 + HyDE + RRF) |
| `learn_pattern` | Save validated patterns to docs (with model trust gates) |
| `session_stats` | Token efficiency report: RAG savings, safety blocks, patterns learned |

---

## 5. RAG System Architecture

### Documentation Corpora (core/docs/)
- `CMDS_API.md` — maya.cmds reference: 15+ sections covering scene management, primitives, transforms, selection, hierarchy, attributes, modeling, UVs, materials, lights, cameras, animation, rendering, plugins, deformers, constraints, joints, namespaces, undo, viewport
- `PYMEL_API.md` — PyMEL object-oriented API: nodes, attributes, connections, transforms, data types, mesh components, key differences from cmds
- `ARNOLD_API.md` — Arnold/mtoa: shaders (aiStandardSurface attributes), lights, render settings, AOVs, textures, PBR setup pattern
- `USD_API.md` — Maya-USD: import/export commands, proxy shapes, pxr Python API, layers, composition, workflow patterns
- `ANTI_PATTERNS.md` — Common hallucinations: wrong command names, wrong flag names, wrong setAttr syntax, wrong return value assumptions, deprecated commands, dangerous patterns, common misconceptions

### Search Pipeline
1. Query arrives at `search_maya_docs`
2. HyDE expands query with domain-specific code template (detects cmds/PyMEL/Arnold/USD/MEL)
3. ChromaDB semantic search with HyDE-expanded query (BGE-large-en-v1.5)
4. BM25 lexical search on same query (exact API name matching)
5. Reciprocal Rank Fusion combines both ranked lists (k=60)
6. Top-N results returned with relevance scores (0-100%)

### Building the Index
```bash
cd maya-mcp
python -m core.rag.build_index
```
First run downloads embedding model (~570 MB, cached). Index stored in `core/rag/index/`.

---

## 6. Safety Module

`core/safety.py` checks for 14+ dangerous patterns:
- Bulk deletes without specific targets
- Undo system tampering (stateWithoutFlush=False)
- Direct filesystem deletion (os.remove, shutil.rmtree)
- Path traversal (../)
- Plugin deregistration while nodes exist
- Namespace deletion with content
- Polygon reduction on referenced geometry
- MEL source injection from untrusted paths
- Critical node unlocking
- Reference removal without user confirmation
- Renderer changes in production scenes

Integrated into: `maya_execute_python`, `maya_delete`. Returns explanation + safe alternative.

---

## 7. Vision3D Flow (Optional Addon — Non-Blocking)

```
Step 1: vision3d_health() → verify GPU server
Step 2: shape_generate_remote(image_path=...) → returns job_id
Step 3: vision3d_poll(job_id=...) → poll until completed
Step 4: vision3d_download(job_id=...) → download GLB, OBJ, textures
Step 5: maya_execute_python(...) → import into Maya
```

Quality presets: `low` (~1 min), `medium` (~2 min), `high` (~8 min), `ultra` (~12 min).

---

## 8. Cross-MCP Pipeline (maya-mcp + fpt-mcp)

All three MCP servers (maya-mcp, fpt-mcp, flame-mcp) share the same architecture: hybrid RAG, HyDE, safety layer, self-learning, token tracking, model trust gates.

Typical publish workflow:
```
1. fpt-mcp: sg_find → search for Asset in ShotGrid
2. fpt-mcp: sg_download → download reference image
3. maya-mcp: shape_generate_remote → generate 3D on Vision3D
4. maya-mcp: vision3d_poll → monitor progress
5. maya-mcp: vision3d_download → download results
6. maya-mcp: maya_execute_python → import in Maya
7. maya-mcp: maya_save_scene → save scene
8. fpt-mcp: tk_publish → register PublishedFile in ShotGrid
```

---

## 9. MANDATORY WORKFLOW for Claude

1. **ALWAYS call `search_maya_docs` first** when unsure about Maya API syntax, flag names, return values, or command names. NEVER guess.
2. **Heed safety warnings** — the safety module blocks dangerous patterns for a reason.
3. **Common hallucinations to avoid**:
   - `cmds.polyCube()` returns a LIST, not a string → use `[0]` for transform name
   - `cmds.setAttr` for compound types REQUIRES `type=` parameter
   - `cmds.file(import=True)` is WRONG → use `i=True` (import is a Python keyword)
   - Flag names use SHORT form: `w=` not `width=`, `r=` not `radius=`
4. **Call `learn_pattern`** when search_maya_docs returned < 60% relevance but the operation worked.
5. **Call `session_stats`** at the end of multi-step tasks.
6. **Always wrap operations in undo chunks** for safe rollback.

---

## 10. Console Panel Architecture

### Maya Embedded Panel
The `console/` package provides a dockable panel inside Maya via `cmds.workspaceControl`.

**Key modules:**
- `qt_compat.py` — PySide2 (Maya 2023-2024) / PySide6 (Maya 2025+) compatibility shim
- `maya_panel.py` — workspaceControl wrapper, Maya callbacks (selection/scene), menu registration
- `chat_widget.py` — Reusable `MCPChatWidget` with context badge, server status dots, markdown rendering
- `claude_worker.py` — QThread that spawns `claude -p --output-format stream-json`
- `server_panel.py` — MCP server discovery from `~/.claude.json`, health checks, `ServerStatusBar`
- `userSetup_snippet.py` — Ready-to-paste snippet for Maya's `userSetup.py`

**How it works:**
1. **Auto-setup on first connect:** `maya_ping` / `maya_launch` call `_ensure_panel_installed()` which injects Python via Command Port to add `sys.path`, register the menu, and open the panel. No manual `userSetup.py` editing needed.
2. `install_menu()` creates "MCP Pipeline > Open Console" in Maya's menu bar
3. `show()` creates a `workspaceControl(retain=True)` docked next to AttributeEditor
4. `_build_panel()` is called by Maya's `uiScript` — wraps Qt pointer, creates `MCPChatWidget`
5. Maya callbacks push selection/scene context into the widget before each message
6. `claude_worker.py` spawns Claude CLI — all MCPs discovered via `~/.claude.json` automatically
7. Panel persists across Maya sessions (retain=True + uiScript auto-rebuilds on restore)

**Standalone consoles** (app.py, chat_window.py) are legacy — use fpt-mcp or flame-mcp consoles instead.

---

## 11. LLM Backend & Model Selection

maya-mcp supports multiple LLM backends via the model selector in the Console panel header.

### Recommended local model: Qwen3.5 9B (`qwen3.5-mcp`)
- **Tool calling**: 97.5% accuracy (1st of 13 models, eval J.D. Hodges)
- **Context window**: 262K tokens
- **Memory**: 6.6 GB (Q4_K_M)
- **Multimodal**: vision-capable (important for viewport_capture analysis)
- **Modelfile**: `qwen3.5-mcp` is a custom Modelfile derived from `qwen3.5:9b` with
  `num_ctx 8192`, `temperature 0.7`, `top_p 0.8`, `top_k 20`.
  Available on glorfindel and Mac M5 Pro.
- **Mac 24GB fallback**: `qwen3.5:4b` (direct, no custom Modelfile)
- **Ollama API note**: requires `"think": false` in each request to disable thinking mode.

### Available backends
| Backend | Label in combo | URL source | Notes |
|---|---|---|---|
| `anthropic` | Claude Sonnet/Opus | Anthropic API | Default, needs internet + API key |
| `ollama` | 🖥 models | `config.json → ollama_url` | glorfindel RTX 3090, LAN |
| `ollama_mac` | 🍎 models | `config.json → ollama_mac_url` | Mac-local, offline |

### Backend switching
The Console panel passes `--model` and env vars (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_API_KEY`) to the Claude Code CLI subprocess. For Ollama backends, the Anthropic
SDK is redirected to the Ollama Messages-compatible endpoint (Ollama v0.14+).

### Write-allowed models (RAG trust gates)
Only Claude models can write patterns via `learn_pattern`. Local models (Ollama) are
read-only — they can search docs but cannot persist new patterns. Configured via
`write_allowed_models` in `core/config.json` (default: `["claude-opus", "claude-sonnet"]`).

### viewport_capture fallback for non-vision models
`maya_viewport_capture` returns both the image (base64) and text metadata (path, resolution,
size). Models without vision capability (e.g. `qwen3.5:4b`, `glm-4.7-flash`) will receive
the text metadata but cannot analyze the image content. The screenshot file is still saved
to the specified `output_path` for manual inspection or later use. When using a non-vision
model, prefer `maya_scene_snapshot` (text-only scene state) over `maya_viewport_capture`.

### Prerequisites for local models
```bash
# Install Ollama (macOS)
brew install ollama
brew services start ollama

# Pull the model
ollama pull qwen3.5:9b
# On Mac 24GB (fallback):
ollama pull qwen3.5:4b
```

### Configuration
Copy `core/config.example.json` to `core/config.json` and adjust URLs.

### Full LLM strategy
See `MODEL_STRATEGY.md` in the ecosystem root for hardware configs, VRAM management,
update procedures, and architecture decisions.

---

**Keep this file updated when architecture, tools, or workflows change.**
