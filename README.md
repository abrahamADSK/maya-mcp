# maya-mcp

> Control Autodesk Maya with natural language using Claude and the Model Context Protocol (MCP)

> [!WARNING]
> **Experimental project — use at your own risk.**
> This is an independent, unofficial experiment created with [Claude Code](https://claude.com/claude-code). It is **not** affiliated with, endorsed by, or officially supported by Autodesk in any way. The Maya name and trademarks belong to Autodesk, Inc.
>
> Executing AI-generated code inside a live Maya session carries real risks: **unexpected crashes, loss of unsaved work, unintended modifications to scenes, rigs, or assets.** Always work on a duplicate or test scene. Never run this on production material without a full backup. The author(s) accept no responsibility for data loss, corruption, or any other damage resulting from its use.

> MCP server for Autodesk Maya — 27 tools with RAG-powered documentation search, anti-hallucination safety, self-learning patterns, and optional AI-driven 3D generation via the [Vision3D](https://github.com/abrahamADSK/vision3d) addon.

---

## Features

### Production Maya Operations
Beyond primitives, maya-mcp handles real production tasks: polygon modeling (extrude, bevel, boolean, combine, smooth), animation keyframing with tangent control, multi-format I/O (OBJ, FBX, GLTF, Alembic, USD, MA/MB), viewport capture/playblast, full scene snapshots, and shelf button creation. All operations use undo chunks for safe rollback.

### AI-Powered 3D Generation (optional addon)
Optionally integrates with [Vision3D](https://github.com/abrahamADSK/vision3d) for image-to-3D and text-to-3D generation. Non-blocking async workflow: submit job → poll status → download results → import into Maya. Runs on a remote GPU server so your local machine stays responsive. Vision3D is **not required** — maya-mcp works fully without it.

### Embedded Maya Console Panel
A dockable Qt panel that lives inside Maya as a `workspaceControl` tab (next to the Attribute Editor). Provides a chat interface to Claude with live Maya context (scene name, object count, selection, renderer). **Installs automatically** — the first time Claude connects to Maya (via `maya_ping` or `maya_launch`), the MCP Pipeline menu and panel are injected into the running Maya session. No manual `userSetup.py` editing required. The panel persists across Maya sessions — if left open, it auto-restores on next launch. Uses PySide2 (Maya 2023–2024) or PySide6 (Maya 2025+) automatically via a compatibility shim. All MCP servers (maya-mcp, fpt-mcp, flame-mcp) are accessible from the panel through Claude Code CLI.

### Cross-MCP Orchestration
Works alongside [fpt-mcp](https://github.com/abrahamADSK/fpt-mcp) (ShotGrid/Flow Production Tracking) and [flame-mcp](https://github.com/abrahamADSK/flame-mcp) (Autodesk Flame). Each DCC has its own embedded console, and all consoles access all MCP servers via Claude Code CLI. When multiple servers are configured, Claude can orchestrate end-to-end VFX workflows across applications. Consistent architecture across all three servers.

---

## Architecture

```
Claude / LLM
    ↕  MCP protocol (stdio)
FastMCP server (src/maya_mcp/server.py) — 27 tools
    ├── RAG engine (src/maya_mcp/rag/)
    │     ├── ChromaDB + BM25 hybrid search
    │     ├── HyDE adaptive query expansion
    │     └── In-session cache + RRF fusion
    ├── Safety module (src/maya_mcp/safety.py)
    │     └── 14+ dangerous pattern detectors
    ├── Maya bridge (src/maya_mcp/maya_bridge.py)
    │     └── TCP socket → Command Port :7001
    └── Vision3D client (HTTP)
          └── GPU server for 3D generation
```

---

## Tools (27 total)

### Maya Scene Operations (18 tools)
| Tool | Description |
|------|-------------|
| `maya_ping` | Verify connection, returns Maya version and scene info |
| `maya_launch` | Open Maya and wait for Command Port to respond |
| `maya_create_primitive` | Create cube / sphere / cylinder / cone / plane / torus |
| `maya_assign_material` | Create and assign material (lambert / blinn / phong / aiStandardSurface) |
| `maya_transform` | Move / rotate / scale with world/object space and relative mode |
| `maya_list_scene` | List scene objects with type and name filters |
| `maya_delete` | Delete objects with safety checks on wildcards |
| `maya_create_light` | Create directional / point / spot / area / ambient lights |
| `maya_create_camera` | Create cameras with focal length and look-at target |
| `maya_execute_python` | Execute arbitrary Python in Maya (with safety scanning) |
| `maya_new_scene` | New empty scene |
| `maya_save_scene` | Save current scene |
| `maya_mesh_operation` | Extrude, bevel, boolean (union/diff/intersect), combine, separate, smooth |
| `maya_set_keyframe` | Keyframe any attribute with tangent control (auto/linear/flat/spline/step) |
| `maya_import_file` | Import OBJ, FBX, GLB/GLTF, Alembic, MA/MB with namespace and scale |
| `maya_viewport_capture` | Playblast screenshot to PNG/JPG at any resolution |
| `maya_scene_snapshot` | Full scene state: file, renderer, object counts, plugins, units |
| `maya_shelf_button` | Create reusable shelf buttons with custom Python commands |

### Vision3D Integration (6 tools — optional, requires [Vision3D](https://github.com/abrahamADSK/vision3d))
| Tool | Description |
|------|-------------|
| `vision3d_health` | Check GPU server availability and model status |
| `shape_generate_remote` | Image-to-3D generation (full pipeline) |
| `shape_generate_text` | Text-to-3D generation |
| `texture_mesh_remote` | Texture an existing mesh with AI |
| `vision3d_poll` | Poll async job status |
| `vision3d_download` | Download completed results to local disk |

### RAG & Intelligence (3 tools)
| Tool | Description |
|------|-------------|
| `search_maya_docs` | Hybrid RAG search across 5 Maya API corpora with relevance scores |
| `learn_pattern` | Save validated working patterns for future sessions (with trust gates) |
| `session_stats` | Token efficiency report: RAG savings, safety blocks, patterns learned |

---

## RAG — Knowledge Engine

### Architecture

LLMs hallucinate Maya API details constantly — wrong flag names (`width=` instead of `w=`), nonexistent commands (`cmds.usdExport` instead of `cmds.mayaUSDExport`), incorrect return types. maya-mcp includes a **hybrid RAG engine** (ChromaDB semantic + BM25 lexical, fused via Reciprocal Rank Fusion) with 5 curated documentation corpora covering maya.cmds, PyMEL, Arnold/mtoa, Maya-USD, and a comprehensive anti-patterns database. The LLM calls `search_maya_docs` before writing any unfamiliar code, getting verified syntax with relevance scores.

### HyDE Query Expansion

Short queries like "set keyframe tangent" don't match code-heavy documentation well. maya-mcp uses **Hypothetical Document Embedding (HyDE)** — it detects which Maya API domain the query targets (cmds, PyMEL, Arnold, USD, MEL) and wraps the query in a domain-specific code template before embedding. This bridges the gap between natural-language questions and code documentation.

### Dangerous Pattern Detection

Before any code reaches Maya, the **safety module** scans for 14+ dangerous patterns: bulk deletes without filters, undo system tampering, filesystem operations on scene files, plugin deregistration with active nodes, namespace deletions, referenced geometry modification, and more. Each pattern includes an explanation of WHY it is dangerous and a SAFE alternative.

---

## Self-Learning

When the RAG returns low-relevance results (< 60%) but the operation succeeds, the LLM can call `learn_pattern` to save the working pattern for future sessions. **Model trust gates** ensure only Sonnet/Opus can write directly to docs — other models stage candidates for human review. Knowledge grows over time without manual curation.

---

## Token Tracking

Every tool call tracks tokens in/out. The `session_stats` tool reports how much context was saved by RAG vs loading full documentation, making the efficiency gains measurable and visible.

---

## Project Structure

```
maya-mcp/
├── src/
│   └── maya_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       ├── server.py              # FastMCP server — all 27 tools
│       ├── maya_bridge.py         # TCP bridge → Maya Command Port :7001
│       ├── safety.py              # Dangerous pattern detection (14+ patterns)
│       ├── config.example.json
│       ├── rag/
│       │   ├── config.py          # Embedding model, search params, token tracking
│       │   ├── build_index.py     # Chunk docs → ChromaDB + BM25 corpus
│       │   ├── search.py          # Hybrid search: BM25 + semantic + HyDE + RRF
│       │   ├── index/             # ChromaDB persistent index (auto-generated)
│       │   └── corpus.json        # BM25 corpus (auto-generated)
│       └── docs/
│           ├── CMDS_API.md        # maya.cmds reference (commands, flags, patterns)
│           ├── PYMEL_API.md       # PyMEL object-oriented API reference
│           ├── ARNOLD_API.md      # Arnold/mtoa shaders, AOVs, render settings
│           ├── USD_API.md         # Maya-USD import/export, proxy shapes, pxr API
│           └── ANTI_PATTERNS.md   # Common LLM hallucinations + wrong flag names
│
├── console/                    # Qt console — Maya panel + legacy standalone
│   ├── qt_compat.py            # PySide2 (Maya 2023-2024) / PySide6 (2025+) shim
│   ├── maya_panel.py           # Dockable workspaceControl panel for Maya
│   ├── chat_widget.py          # Reusable MCPChatWidget (shared by panel & standalone)
│   ├── claude_worker.py        # QThread worker — Claude CLI subprocess bridge
│   ├── server_panel.py         # MCP server discovery, health checks, ServerStatusBar
│   ├── userSetup_snippet.py    # Paste into Maya's userSetup.py for auto-setup
│   ├── app.py                  # Legacy standalone entry point (use fpt-mcp console)
│   ├── chat_window.py          # Legacy standalone chat window
│   └── build_app_bundle.py     # Legacy macOS .app bundle generator
│
├── tests/
├── pyproject.toml              # Build config, entry point: python -m maya_mcp.server
├── reference/                  # Pipeline I/O (git-ignored)
├── CLAUDE.md                   # Project documentation for Claude
├── WORKFLOW_GUIDE.md           # Workflow guide
├── .env.example                # Configuration template
└── README.md
```

---

## Requirements

- macOS
- Autodesk Maya 2023 or later
- Python 3.9 or higher (ships with Maya)
- Node.js v22 or higher (required by Claude Code)
- Claude Code 2.x
- A Claude account — Pro, Max, or API key

### Optional

- [Ollama](https://ollama.com) >= 0.17.6 — for local / free inference instead of Anthropic cloud
  - macOS: `brew install ollama && brew services start ollama`
  - Linux: https://ollama.com/download/linux (systemd)
  - Verify: `ollama --version`
  - Create the `qwen3.5-mcp` model (required for Ollama backends):
    ```bash
    ollama pull qwen3.5:9b
    ollama create qwen3.5-mcp -f Modelfile.qwen35mcp
    ```
  - See [MODEL_STRATEGY.md](MODEL_STRATEGY.md) for Modelfile details, `think: false` requirement, and KEEP_ALIVE tuning
- [Vision3D](https://github.com/abrahamADSK/vision3d) server for AI-powered 3D generation

---

## Installation

### Automatic Installation

```bash
git clone https://github.com/abrahamADSK/maya-mcp.git
cd maya-mcp
chmod +x install.sh
./install.sh
```

The installer creates a virtual environment, installs dependencies, builds the RAG index, and registers the MCP server with Claude Code.

### 1. Clone and configure

```bash
git clone https://github.com/abrahamADSK/maya-mcp.git
cd maya-mcp
cp .env.example .env
# Edit .env and set GPU_API_URL to your Vision3D server address
# Example: GPU_API_URL=http://glorfindel:8000
```

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# RAG dependencies (optional but recommended)
pip install chromadb sentence-transformers rank-bm25
```

### 3. Build the RAG index

```bash
python -m maya_mcp.rag.build_index
```

First run downloads the embedding model (~570 MB, cached afterwards). The index is stored in `src/maya_mcp/rag/index/` and can be committed to git.

### 4. Set up Maya Command Port

Add to your Maya `userSetup.py` (create it if it doesn't exist):
- **Windows**: `%USERPROFILE%/Documents/maya/<version>/scripts/userSetup.py`
- **macOS**: `~/Library/Preferences/Autodesk/maya/<version>/scripts/userSetup.py`
- **Linux**: `~/maya/<version>/scripts/userSetup.py`

```python
import maya.cmds as cmds

def open_command_port():
    port_name = ":7001"
    try:
        if cmds.commandPort(port_name, query=True):
            cmds.commandPort(name=port_name, close=True)
    except RuntimeError:
        pass
    cmds.commandPort(name=port_name, sourceType="mel")
    print(f"[MCP] Command Port open on {port_name}")

cmds.evalDeferred(open_command_port)
```

**The MCP Pipeline Console panel installs itself automatically.** The first time Claude connects to Maya (via `maya_ping` or `maya_launch`), the server injects the panel menu and UI through the Command Port — no additional `userSetup.py` configuration needed. The panel docks next to the Attribute Editor and persists across sessions.

> **Optional:** If you want the menu to be available even before Claude connects (e.g., on every Maya startup), see `console/userSetup_snippet.py` for additional `userSetup.py` entries.

### 5. Configure Claude Code

```bash
claude mcp add maya-mcp -s user -- /path/to/maya-mcp/.venv/bin/python -m maya_mcp.server
```

Or for Claude Desktop, add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "maya-mcp": {
      "command": "/path/to/maya-mcp/.venv/bin/python",
      "args": ["-m", "maya_mcp.server"],
      "cwd": "/path/to/maya-mcp",
      "env": {
        "GPU_API_URL": "http://your-gpu-host:8000",
        "GPU_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### 6. Vision3D addon (optional — for AI 3D generation)

maya-mcp can optionally integrate with [Vision3D](https://github.com/abrahamADSK/vision3d) for image-to-3D and text-to-3D generation. This is **not required** for core Maya functionality. If you want it, follow the Vision3D README to install and run, then set `GPU_API_URL` and `GPU_API_KEY` in your environment.

---

## Usage

Once installed, maya-mcp is available through Claude Code or any MCP-compatible client. Open Maya, ensure the Command Port is running, and start a conversation:

```
You: "Create a 2x2 grid of spheres with 0.5 unit spacing"
Claude → maya_create_primitive (sphere) × 4 → maya_transform (position each) → Result
```

```
You: "Generate a 3D model from this reference image and import it"
Claude → shape_generate_remote (submit to Vision3D) → vision3d_poll (wait) → vision3d_download → maya_import_file → Result
```

All operations go through the safety scanner before reaching Maya. Dangerous patterns are blocked with an explanation and a safe alternative.

---

## Configuration Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `MAYA_HOST` | Maya host | `localhost` |
| `MAYA_PORT` | Maya Command Port | `7001` |
| `GPU_API_URL` | Vision3D server URL | — |
| `GPU_API_KEY` | Vision3D API key | — |
| `SHAPE_TIMEOUT` | Shape generation timeout (seconds) | `900` |
| `TEXTURE_TIMEOUT` | Texture generation timeout (seconds) | `600` |

---

## Cross-MCP Pipeline

When both maya-mcp and [fpt-mcp](https://github.com/abrahamADSK/fpt-mcp) are configured, Claude can orchestrate end-to-end VFX workflows: query ShotGrid for an asset → download reference image → generate 3D via Vision3D → import into Maya → register the publish back in ShotGrid. All from one conversation.

All three MCP servers (maya-mcp, fpt-mcp, flame-mcp) share the same architecture: hybrid RAG, HyDE, safety layer, self-learning, token tracking, and model trust gates.

---

## Troubleshooting

**Maya Command Port not responding** — Verify in Maya's Script Editor: `cmds.commandPort(':7001', query=True)`. If `False`, run the `open_command_port()` snippet.

**RAG search returns "index not found"** — Run `python -m maya_mcp.rag.build_index` to build the index.

**Shape inference fails immediately** — Model weights may be incomplete. Check that `hunyuan3d-dit-v2-0-turbo/model.fp16.safetensors` (~4.6 GB) exists on the GPU server.

**GPU API connection refused** — Verify Vision3D is running: `curl $GPU_API_URL/api/health`.

---

## Ecosystem

`maya-mcp` is part of a four-component VFX pipeline. Each component has a defined role:

| Repo | Role |
|------|------|
| [flame-mcp](https://github.com/abrahamADSK/flame-mcp) | Controls Autodesk Flame for compositing, conform, and finishing |
| [maya-mcp](https://github.com/abrahamADSK/maya-mcp) | Controls Autodesk Maya for 3D modeling, animation, and rendering |
| [fpt-mcp](https://github.com/abrahamADSK/fpt-mcp) | Connects to Autodesk Flow Production Tracking (ShotGrid) for production tracking, asset management, and publishes |
| [vision3d](https://github.com/abrahamADSK/vision3d) | GPU inference server for AI-powered 3D generation — the remote backend for maya-mcp's image-to-3D and text-to-3D tools |

`maya-mcp` sits at the 3D creation stage of the pipeline. It consumes `vision3d` via HTTP — submitting image-to-3D or text-to-3D jobs and importing the resulting `.glb` files into Maya. It works alongside `fpt-mcp` for end-to-end workflows: query ShotGrid for an asset, generate or load reference, build the 3D asset in Maya, and register the publish back in ShotGrid. `flame-mcp` typically operates downstream, receiving rendered outputs from Maya for finishing.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
