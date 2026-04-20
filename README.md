# maya-mcp

> Control Autodesk Maya with natural language using Claude and the Model Context Protocol (MCP)

> [!WARNING]
> **Experimental project — use at your own risk.**
> This is an independent, unofficial experiment created with [Claude Code](https://claude.com/claude-code). It is **not** affiliated with, endorsed by, or officially supported by Autodesk in any way. The Maya name and trademarks belong to Autodesk, Inc.
>
> Executing AI-generated code inside a live Maya session carries real risks: **unexpected crashes, loss of unsaved work, unintended modifications to scenes, rigs, or assets.** Always work on a duplicate or test scene. Never run this on production material without a full backup. The author(s) accept no responsibility for data loss, corruption, or any other damage resulting from its use.

> MCP server for Autodesk Maya — 14 MCP tools with RAG-powered documentation search, anti-hallucination safety, self-learning patterns, and optional AI-driven 3D generation via the [Vision3D](https://github.com/abrahamADSK/vision3d) addon.

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
FastMCP server (src/maya_mcp/server.py) — 14 MCP tools
    ├── RAG engine (src/maya_mcp/rag/)
    │     ├── ChromaDB + BM25 hybrid search
    │     ├── HyDE adaptive query expansion
    │     └── In-session cache + RRF fusion
    ├── Safety module (src/maya_mcp/safety.py)
    │     └── 14+ dangerous pattern detectors
    ├── Maya bridge (src/maya_mcp/maya_bridge.py)
    │     └── TCP socket → Command Port :8100
    └── Vision3D client (HTTP)
          └── GPU server for 3D generation
```

---

<!-- concept:mcp_tool_count start -->
## Tools (14 MCP tools)
<!-- concept:mcp_tool_count end -->

<!-- concept:mcp_tool_table start -->
### Maya Direct Tools (9 tools)
| Tool | Description |
|------|-------------|
| `maya_create_primitive` | Create cube / sphere / cylinder / cone / plane / torus |
| `maya_assign_material` | Create and assign material (lambert / blinn / phong / aiStandardSurface) |
| `maya_transform` | Move / rotate / scale with world/object space and relative mode |
| `maya_create_light` | Create directional / point / spot / area / ambient lights |
| `maya_create_camera` | Create cameras with focal length and look-at target |
| `maya_mesh_operation` | Extrude, bevel, boolean (union/diff/intersect), combine, separate, smooth |
| `maya_set_keyframe` | Keyframe any attribute with tangent control (auto/linear/flat/spline/step) |
| `maya_import_file` | Import OBJ, FBX, GLB/GLTF, Alembic, MA/MB with namespace and scale |
| `maya_viewport_capture` | Playblast screenshot to PNG/JPG at any resolution |

### Dispatcher Tools (2 tools)
| Tool | Description |
|------|-------------|
| `maya_session` | Session lifecycle + generic Maya ops (see action table below) |
| `maya_vision3d` | Optional Vision3D addon (see action table below) |

### RAG & Intelligence (3 tools)
| Tool | Description |
|------|-------------|
| `search_maya_docs` | Hybrid RAG search across 5 Maya API corpora with relevance scores |
| `learn_pattern` | Save validated working patterns for future sessions (with trust gates) |
| `session_stats` | Token efficiency report: RAG savings, safety blocks, patterns learned |
<!-- concept:mcp_tool_table end -->

### Maya Session Dispatcher (`maya_session` — 9 actions)
<!-- concept:maya_session_actions start -->
| Action | Description |
|--------|-------------|
| `ping` | Verify connection, returns Maya version and scene info |
| `launch` | Open Maya and wait for Command Port to respond |
| `new_scene` | New empty scene |
| `save_scene` | Save current scene |
| `list_scene` | List scene objects with type and name filters |
| `scene_snapshot` | Full scene state: file, renderer, object counts, plugins, units |
| `delete` | Delete objects with safety checks on wildcards |
| `execute_python` | Execute arbitrary Python in Maya (with safety scanning) |
| `shelf_button` | Create reusable shelf buttons with custom Python commands |
<!-- concept:maya_session_actions end -->

### Vision3D Dispatcher (`maya_vision3d` — 7 actions, optional, requires [Vision3D](https://github.com/abrahamADSK/vision3d))
<!-- concept:maya_vision3d_actions start -->
| Action | Description |
|--------|-------------|
| `select_server` | Set the Vision3D server URL for the rest of this MCP session (runtime-only, asked from the user in the chat) |
| `health` | Check availability and model status of the selected server |
| `generate_image` | Image-to-3D generation (full pipeline) |
| `generate_text` | Text-to-3D generation |
| `texture` | Texture an existing mesh with AI |
| `poll` | Poll async job status |
| `download` | Download completed results to local disk |
<!-- concept:maya_vision3d_actions end -->

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
│       ├── server.py              # FastMCP server — 14 MCP tools
│       ├── maya_bridge.py         # TCP bridge → Maya Command Port :8100
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
# Optional: set GPU_API_URL as a *suggested default* for Vision3D.
# It is never auto-selected — Claude will surface it when asking you
# which Vision3D URL to use, and you have to confirm it explicitly.
# Example: GPU_API_URL=http://<your-gpu-host>:8000
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

### 4. Set up Maya Command Port — automatic

**`install.sh` does this for you.** Step 7 of the installer detects every Maya version installed on the host, locates each version's user scripts dir, and writes an idempotent guarded block into `userSetup.py` that:

- Adds the maya-mcp repo root to `sys.path`
- Opens the Command Port on `MAYA_PORT` (from `.env`, default `8100`) using `sourceType='mel'` with the `name=` kwarg form (Maya 2027 silently ignores the positional form when `sourceType` is specified)
- Registers the MCP Pipeline menu via `executeDeferred`

The block is bounded by sentinel markers and reruns of `install.sh` are safe — the installer replaces the whole region when the markers are found.

Run `./install.sh --doctor` after install to verify `userSetup.py` was written for every detected version (plus 4 other install-completeness checks).

> **Port 8100 default rationale**: maya-mcp historically used port 7001, the Maya commandPort convention. On hosts with Autodesk Flame installed, port 7001 is already held by Flame's S+W Service Discovery and S+W Probe Server, and connections silently succeed against Flame instead of Maya — producing empty responses that the bridge (prior to v1.4.2) misinterpreted as successful no-ops. The default was moved to 8100 to coexist with Flame. Override via `MAYA_PORT` in `.env` if your environment still uses 7001.

**The MCP Pipeline Console panel installs itself automatically.** The first time Claude connects to Maya (via `maya_ping` or `maya_launch`), the server injects the panel menu and UI through the Command Port. The panel docks next to the Attribute Editor and persists across sessions.

<details>
<summary><b>Manual fallback</b> (if Step 7 fails or you have an exotic Maya layout)</summary>

Add to your Maya `userSetup.py`:

- **Windows**: `%USERPROFILE%/Documents/maya/<version>/scripts/userSetup.py`
- **macOS**: `~/Library/Preferences/Autodesk/maya/<version>/scripts/userSetup.py`
- **Linux**: `~/maya/<version>/scripts/userSetup.py`

```python
# --- MCP Pipeline Console auto-setup ---
import sys as _mcp_sys

_mcp_root = r"/path/to/maya-mcp"  # replace with your clone path
if _mcp_root not in _mcp_sys.path:
    _mcp_sys.path.insert(0, _mcp_root)

import maya.utils as _mcp_utils


def _mcp_open_command_port():
    try:
        import maya.cmds as _mc
        if not _mc.commandPort(":8100", query=True):
            _mc.commandPort(name=":8100", sourceType="mel")
    except Exception:
        pass


def _mcp_menu_startup():
    try:
        from console.maya_panel import install_menu
        import maya.cmds as _mc
        if not _mc.menu("mcpPipelineMenu", exists=True):
            install_menu()
    except Exception:
        pass


_mcp_utils.executeDeferred(_mcp_open_command_port)
_mcp_utils.executeDeferred(_mcp_menu_startup)
# --- end MCP Pipeline Console ---
```

</details>

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

maya-mcp can optionally integrate with [Vision3D](https://github.com/abrahamADSK/vision3d) for image-to-3D and text-to-3D generation. This is **not required** for core Maya functionality.

**Vision3D URL is never stored.** maya-mcp does not hold any Vision3D endpoint in config files, environment presets, or hardcoded defaults. On the first Vision3D call of each MCP session:

1. The dispatch returns `vision3d_url_required`.
2. Claude asks you in the chat which Vision3D URL to use.
3. You type the URL (e.g. a local Mac MPS instance or a remote CUDA host).
4. Claude calls `maya_vision3d(action="select_server", params={"url": "<the-url>"})`.
5. The URL is cached in the MCP process memory until restart and used for every subsequent call of the session.

You can switch servers mid-session by calling `select_server` again with a different URL. No restart required.

**Suggested default via `GPU_API_URL`** — if the environment variable `GPU_API_URL` is set, Claude surfaces it to you as a suggested default when asking for the URL. You still have to confirm or override it explicitly; it is never auto-selected. This is the only escape hatch for pre-selector installs. `GPU_API_KEY` is only needed if your Vision3D server has an API key configured.

---

## Usage

Once installed, maya-mcp is available through Claude Code or any MCP-compatible client. Open Maya, ensure the Command Port is running, and start a conversation:

```
You: "Create a 2x2 grid of spheres with 0.5 unit spacing"
Claude → maya_create_primitive (sphere) × 4 → maya_transform (position each) → Result
```

```
You: "Generate a 3D model from this reference image and import it"
Claude → maya_vision3d(action='generate_image', ...) → maya_vision3d(action='poll', ...) → maya_vision3d(action='download', ...) → maya_import_file → Result
```

All operations go through the safety scanner before reaching Maya. Dangerous patterns are blocked with an explanation and a safe alternative.

---

## Configuration Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `MAYA_HOST` | Maya host | `localhost` |
| `MAYA_PORT` | Maya Command Port | `8100` |
| `GPU_API_URL` | **Optional** suggested default for the Vision3D URL prompt. Never auto-selected — Claude asks the user to confirm or override at the first Vision3D call of each session. | — |
| `GPU_API_KEY` | Vision3D API key | — |
| `SHAPE_TIMEOUT` | Shape generation timeout (seconds) | `900` |
| `TEXTURE_TIMEOUT` | Texture generation timeout (seconds) | `600` |

---

## Cross-MCP Pipeline

When both maya-mcp and [fpt-mcp](https://github.com/abrahamADSK/fpt-mcp) are configured, Claude can orchestrate end-to-end VFX workflows: query ShotGrid for an asset → download reference image → generate 3D via Vision3D → import into Maya → register the publish back in ShotGrid. All from one conversation.

All three MCP servers (maya-mcp, fpt-mcp, flame-mcp) share the same architecture: hybrid RAG, HyDE, safety layer, self-learning, token tracking, and model trust gates.

---

## Troubleshooting

**Maya Command Port not responding** — Verify in Maya's Script Editor: `cmds.commandPort('localhost:8100', query=True)`. If `False`, run the `open_command_port()` snippet.

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
