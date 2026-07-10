# maya-mcp — Critical Context for Claude

> **Last updated**: 2026-04-12 — per-session Vision3D URL is now fully runtime-only: Claude asks the user for the URL on the first Vision3D call, caches it in memory for the rest of the session, and forgets it on MCP restart. No config file field, no pool, no persisted URLs anywhere.
> This document persists across Claude Code sessions. Consult here to understand the architecture, configuration, and workflows of maya-mcp.

---

## 1. Architecture

**maya-mcp** is a production-grade **MCP (Model Context Protocol)** server based on **FastMCP** with **16 MCP tools** organized in three layers (plus the optional WorldLabs environment addon):

1. **Maya Control** (9 direct tools + 1 dispatch tool with 12 actions) — Scene manipulation, modeling, animation, I/O, rendering
   - Communicates with Maya via **TCP Command Port** (default port 8100; moved from the historical 7001 because that port is held by Flame's S+W services on hosts with Autodesk Flame installed)
   - Uses `maya_bridge.py` (socket bridge) to execute MEL/Python commands
   - All operations use undo chunks for safe rollback

2. **Vision3D Integration** (7 actions behind the `maya_vision3d` dispatch tool) — Optional addon for AI-powered 3D generation via [Vision3D](https://github.com/abrahamADSK/vision3d)
   - Communicates via **HTTP REST API** with Vision3D (port 8000)
   - Supports image-to-3D, text-to-3D, and texture painting
   - Non-blocking async pattern: submit → poll → download
   - **Fully per-session URL**: the first call that needs a GPU returns `vision3d_url_required`. The LLM asks the user for the Vision3D URL in the chat, the user types it, the LLM calls `select_server` with that URL, and it is cached in memory for the rest of the session. Nothing is written to disk — no config field, no pool, no whitelist. Restarting the MCP server clears the selection.
   - **`GPU_API_URL` env var** is honored as a *suggested default* the LLM can surface in the prompt, but it is never auto-selected. The user still has to confirm it explicitly via `select_server`.
   - **Not required** — maya-mcp works fully without Vision3D

3. **RAG & Intelligence** (4 tools) — Documentation search, self-learning, analytics
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
│   maya-mcp FastMCP Server (16 tools)              │
│                                                    │
│  ┌─────────┐ ┌─────────┐ ┌──────────────────────┐│
│  │ RAG     │ │ Safety  │ │ Token Tracking       ││
│  │ Engine  │ │ Module  │ │ + Model Trust Gates  ││
│  └────┬────┘ └────┬────┘ └──────────────────────┘│
│       │           │                                │
├───────┼───────────┼────────────────────────────────┤
│  Maya Bridge (TCP)     Vision3D REST Client        │
└────┬───────────────────────┬──────────────────────┘
     │ :8100 Command Port    │ HTTP :8000
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
`learn_pattern` saves validated working patterns to the docs corpus. Model trust gates: only Opus/Fable write directly; other models stage candidates for review in `rag/candidates.json`.

### Token Efficiency Tracking
`session_stats` reports tokens used vs saved by RAG, safety blocks, patterns learned, cache hits, and full-doc baseline comparison.

---

## 3. Execution Environment

For installation location, system requirements, and Command Port setup,
see [`docs/DEPLOY.md`](docs/DEPLOY.md).

### Environment Variables (`.env`)
```bash
MAYA_HOST=localhost          # Host where Maya is running
MAYA_PORT=8100              # Command Port (historically 7001; moved to avoid Flame S+W port collision)
GPU_API_URL=                 # Optional: suggested default for Vision3D URL prompt (never auto-selected)
GPU_API_KEY=                 # Optional: API key for Vision3D server, leave empty for open LAN
GPU_VERIFY_TLS=true          # Verify TLS for https Vision3D targets (default true; ignored for http; set false to opt out for self-signed https)
```

**Vision3D URL is NOT stored anywhere.** There is no `vision3d_servers` config field, no pool, no whitelist. On the first Vision3D call of the session, the dispatch returns `vision3d_url_required`; Claude asks the user for the URL in the chat, the user types it, Claude calls `select_server` with that URL, and it is cached in the MCP process memory until restart. If `GPU_API_URL` is set in the environment, Claude surfaces it as a *suggested default* in the prompt — the user still has to confirm it explicitly. `config.json` only holds backend/model/Ollama settings; it does NOT hold Vision3D endpoints.

### Review colour management (`color_policy.py`)
The Maya projects in this pipeline inherit Maya's **built-in default OCIO config**
(nothing sets an `OCIO` env var, `colorManagementPrefs`, or an ACES config — Chat
79). `src/maya_mcp/color_policy.py` is the single source of truth for the
preview/review **view transform**: the VP2.0 capture paths (`review_turntable`,
`maya_viewport_capture`) pin it before the playblast and restore it after, so a
version preview is colour-correct and deterministic rather than riding the
session's current view. The view is read from `config.json -> review_view_transform`
(default `"Un-tone-mapped (sRGB)"`); set it per project if a project moves to ACES
(e.g. `"sRGB (ACES)"`). All blocks are best-effort/guarded — degrade to a no-op
when a flag is absent. `color_policy.py` also emits the Arnold **output transform**
recipe (preview = bake it, EXR = force OFF) documented in `docs/ARNOLD_API.md`;
note that VP2.0 playblasts do NOT pass through an Arnold driver, so the view
transform (not the Arnold output transform) is what governs them.

---

## 4. Available Tools (<!-- concept:mcp_tool_count start -->16<!-- concept:mcp_tool_count end --> MCP tools)

### Maya Direct Tools (9 MCP tools)

| Tool | Description |
|------|-------------|
| `maya_create_primitive` | Creates 3D primitives (cube, sphere, cylinder, cone, plane, torus) |
| `maya_assign_material` | Creates and assigns material (lambert, blinn, phong, aiStandardSurface) |
| `maya_transform` | Moves, rotates, scales objects in world/object space |
| `maya_create_light` | Creates lights (directional, point, spot, area, ambient) |
| `maya_create_camera` | Creates camera with focal length and look-at target |
| `maya_mesh_operation` | Extrude, bevel, boolean (union/diff/intersect), combine, separate, smooth |
| `maya_set_keyframe` | Keyframe any attribute with tangent control |
| `maya_import_file` | Import OBJ, FBX, GLB/GLTF, Alembic, MA/MB, BVH mocap with namespace and scale (streams progress; 120s bridge budget, 240s for BVH via `bvh_import`) |
| `maya_viewport_capture` | Fast Viewport-2.0 screenshot to PNG/JPG at any resolution (NOT an Arnold render). Playblasts a throw-away VP2.0 window (never the user's focused panel), so it can never capture a live Arnold IPR / render-override and hang Maya. For a ray-traced still use `maya_session action=render_still`. |

### Maya Session Actions (13 actions behind `maya_session` dispatch tool)

<!-- concept:maya_session_actions start -->
| Action | Description |
|--------|-------------|
| `ping` | Verifies connection, returns version, current scene, renderer |
| `launch` | Opens Maya and waits for Command Port to respond (max 90s; streams progress + info lines while waiting) |
| `new_scene` | Creates new empty scene (refuses if the scene has unsaved changes; pass confirm=true in params to discard them) |
| `save_scene` | Saves current scene |
| `list_scene` | Lists scene objects with filters by type or name |
| `scene_snapshot` | Full scene state: file, renderer, counts, plugins, units |
| `delete` | Deletes objects (with safety checks on wildcards) |
| `execute_python` | Executes arbitrary Python in Maya (safety scanning; optional timeout param 1-600s for long ops — Command Port default wait is 10s — with progress heartbeats every 10s) |
| `shelf_button` | Create shelf buttons with custom Python commands |
| `operation_history` | Read recent durable-audit records (read-only; requires MAYA_AUDIT_LOG=1). Optional filters: limit, tool, action, status |
| `publish` | Drive the native Toolkit publisher (tk-multi-publish2) inside an engine'd Maya launched via tank. params: mode (preview/publish), include/exclude intent tokens, comment, timeout. Preview reads the collected publish tree; publish activates matching tasks then validate→publish→finalize. Dependencies captured automatically by the plugins. |
| `review_turntable` | Deterministic Viewport 2.0 turntable playblast to a .mov (long op, runs in Maya). Frames the model, orbits 360° over start–end at the given fps, 16:9 / square pixels / overscan, offscreen (never Arnold). Needs out_path (resolve via fpt tk_resolve_path, template movie_asset_publish); returns the mov plus a Version code Asset_Task to name the review Version after its task. |
| `render_still` | Single-frame **Arnold** ray-traced still → PNG at out_path (long op, runs in Maya). This is what "a still" means — a real render, not the VP2.0 grab of `maya_viewport_capture`. Renders one frame to the Render View and writes it to the exact out_path; never playblasts a viewport, so it can't hang the main thread. Params: out_path (required), camera, frame, width, height, aa_samples, view_transform, timeout. |
<!-- concept:maya_session_actions end -->

### Vision3D Actions (7 actions behind `maya_vision3d` dispatch — optional addon, requires [Vision3D](https://github.com/abrahamADSK/vision3d))

<!-- concept:maya_vision3d_actions start -->
| Action | Description |
|--------|-------------|
| `select_server` | Set the Vision3D server URL for the rest of this MCP session. Accepts any valid http/https URL — the LLM must have asked the user for it in the chat first. Cached in memory until process restart. |
| `health` | Check availability, GPU info, models, and text-to-3D status of the selected server |
| `generate_image` | Image-to-3D generation (non-blocking, returns job_id) |
| `generate_text` | Text-to-3D generation (non-blocking, returns job_id) |
| `texture` | Texture existing mesh (non-blocking, returns job_id) |
| `poll` | Poll job status with incremental log lines |
| `download` | Download completed results to local directory |
<!-- concept:maya_vision3d_actions end -->

### RAG & Intelligence Tools (4 tools)

| Tool | Description |
|------|-------------|
| `search_maya_docs` | Hybrid RAG search across 5 Maya API corpora (semantic + BM25 + HyDE + RRF) |
| `learn_pattern` | Save validated patterns to docs (with model trust gates) |
| `session_stats` | Token efficiency report: RAG savings, safety blocks, patterns learned, p_fallo |
| `reset_session_stats` | Zero the session counters immediately (manual companion to the 30-min idle auto-reset) |

---

## 5. RAG System Architecture

### Documentation Corpora (src/maya_mcp/docs/)
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

Operator-only. See [`docs/DEPLOY.md`](docs/DEPLOY.md) for the
build-index command and prerequisites.

---

## 6. Safety Module

`src/maya_mcp/safety.py` checks for 14+ dangerous patterns:
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

### F4b — AST dry-run command validation (3C Wave 4)

`src/maya_mcp/_ast_validate.py` statically walks the AST of every
`maya_session(action="execute_python")` snippet, finds each
`cmds.<command>` / `maya.cmds.<command>` reference, and checks the command name
against `src/maya_mcp/rag/api_graph.json` — the set of **real** `maya.cmds`
commands generated by `scripts/introspect_maya_api.py` from a live Maya (run it
with `mayapy`). A hallucinated command (`cmds.polyCubez(...)`) is rejected with
an `ast_warning` and a `did you mean` suggestion **before** the Command Port
round-trip; the call never reaches Maya and is not counted as a turn.

It validates command **existence only** (not flag names or usage — that stays
the job of the RAG corpus / `ANTI_PATTERNS.md`), so it never false-positives on
a real command. It degrades to a no-op when `api_graph.json` is missing.

- **Regenerate per Maya major release**: `mayapy scripts/introspect_maya_api.py`,
  then commit the updated `api_graph.json` alongside the version bump. The
  introspector best-effort loads the pipeline I/O + USD + Arnold plugins
  (`mayaUsdPlugin`, `AbcImport`, `AbcExport`, `fbxmaya`, `objExport`, `mtoa`)
  before walking `dir(cmds)`, so plugin-registered commands (`mayaUSDImport`,
  `AbcImport`, `FBXImport`, `arnoldRender` …) are in the graph and F4b does not
  false-positive on them. The loaded/failed plugin sets are recorded in the
  graph's `_meta`; add still more via the `MAYA_INTROSPECT_PLUGINS` env var.
  **NB**: glTF/OBJ are file translators
  invoked through `cmds.file(type=…)` (a core command) and register no
  `cmds.<command>` of their own, so they need no entry. Graph as of Chat 57:
  Maya **2027**, 4831 commands.
- **Arnold stopgap**: the *committed* 4831-command graph predates the `mtoa`
  wiring above (its `_meta.plugins_loaded` has no `mtoa`), so until it is
  regenerated on an Arnold-licensed box the documented `cmds.arnoldRender(…)` /
  `cmds.arnoldExportAss(…)` calls are kept valid by a curated `_MTOA_COMMANDS`
  allowlist in `_ast_validate.py` (folded into the valid set only when a real
  graph is loaded). The allowlist becomes redundant once the graph carries the
  Arnold command surface natively. It does NOT mask hallucinations
  (`cmds.arnoldRenderz(…)` is still rejected) and is not an anti-pattern escape
  hatch (`arnoldRenderSettings`, flagged WRONG in `ANTI_PATTERNS.md`, stays
  rejected).
- **Bypass**: set `ast_dry_run: false` in `config.json` (e.g. if you are on a
  newer Maya than the committed graph and hit a false rejection).

### Audit log (opt-in, OFF by default)

`src/maya_mcp/_audit.py` writes a durable, append-only JSONL record of tool
executions to `src/maya_mcp/logs/audit.jsonl` — the accountability/forensics
stream, distinct from the F0 efficiency telemetry in `logs/timings.jsonl`.

- **Toggle**: `MAYA_AUDIT_LOG` env var. OFF unless set to `1`/`true`/`yes`/`on`;
  when unset the whole path is a no-op (no file, no perf/disk/privacy impact, no
  behaviour change).
- **Each entry**: `ts`, `tool`, `action`, sanitised `params`, `status`
  (`ok` / `error` / `safety_blocked` / `ast_rejected`), plus `model`/`backend`.
  For `execute_python` the code is truncated to ~2000 chars **plus a SHA-256 of
  the full code** + its length; Maya result payloads are **never** stored.
- **Coverage**: wired at the `maya_session` dispatcher, on the standalone
  mutation tools (one-line `@_audited(...)` decorator under `@mcp.tool`), and
  inside `_do_execute_python` / `_do_delete` so safety/AST **blocked** attempts
  are captured. Read-only actions (`ping`, `list_scene`, `scene_snapshot`,
  `operation_history`) are excluded.
- **Substrate**: reuses `_session_stats.persist_timing` (5 MB + `.1` rotation,
  best-effort — an audit failure never breaks a tool call). Writes add **no new
  MCP tool**; the records are read back via `maya_session(action="operation_history")`
  (`_audit.read_records`, newest-first, optional limit/tool/action/status filters,
  same best-effort contract) — a dispatcher action, so the tool count is still 15.
  `jq`/`grep` on `logs/audit.jsonl` remain available too.

---

## 7. Vision3D Flow (Optional Addon — Non-Blocking)

```
Step 0: (first Vision3D call of the session only)
        Any action → returns vision3d_url_required
        Claude asks the user: "Which Vision3D URL should I use?"
        User types the URL in the chat.
        Claude → maya_vision3d(action='select_server', params={'url': '<the-url>'})
Step 1: maya_vision3d(action='health')                    → verify the selected GPU server
Step 2: maya_vision3d(action='generate_image', params={'image_path': ...}) → returns job_id
Step 3: maya_vision3d(action='poll', params={'job_id': ...}) → poll until completed
Step 4: maya_vision3d(action='download', params={'job_id': ...}) → download GLB, OBJ, textures
Step 5: maya_execute_python(...) → import into Maya
```

**Step 0 is mandatory on the first Vision3D call of the session.** Any Vision3D action called before `select_server` returns `vision3d_url_required`. The LLM must:

1. **Ask the user** which Vision3D URL to use. The user types the URL into the chat.
2. If `GPU_API_URL` is set in the environment, the error payload includes a `suggested_default` field. Surface that default to the user as a hint — but it is NOT auto-selected, the user still has to confirm or override.
3. Call `select_server` with the URL the user provided.
4. Retry the original action.

The URL is cached in memory for the rest of the session. Restarting the MCP server clears it and the cycle begins again.

Quality presets: `low` (~1 min), `medium` (~2 min), `high` (~8 min), `ultra` (~12 min).

---

## 8. Cross-MCP Pipeline (maya-mcp + fpt-mcp)

All three MCP servers (maya-mcp, fpt-mcp, flame-mcp) share the same architecture: hybrid RAG, HyDE, safety layer, self-learning, token tracking, model trust gates.

Typical publish workflow:
```
1. fpt-mcp: sg_find → search for Asset in ShotGrid
2. fpt-mcp: sg_download → download reference image
3. maya-mcp: maya_vision3d(action='generate_image') → generate 3D on Vision3D
4. maya-mcp: maya_vision3d(action='poll') → monitor progress
5. maya-mcp: maya_vision3d(action='download') → download results
6. maya-mcp: maya_session(action='execute_python') → import in Maya
7. maya-mcp: maya_session(action='save_scene') → save scene
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
- `claude_worker.py` — QThread that spawns `claude -p --output-format stream-json`. **Read-only lockdown** (recording-safe): launched with `--disallowedTools Edit Write MultiEdit NotebookEdit Bash` (`console._readonly.DISALLOWED_TOOLS`), so the agent CANNOT modify the repo. MCP tools + Read stay available; RAG self-learning is unaffected (`learn_pattern` writes server-side). Improvement ideas are not applied — the agent emits `@@SUGGESTION@@ <title> :: <detail>` lines, which `capture_suggestions` logs to the git-ignored `CONSOLE_IMPROVEMENTS.md` and strips from the reply.
- `_readonly.py` — Qt-free helper: `DISALLOWED_TOOLS` deny-list + `capture_suggestions` (backlog writer). Unit-tested in `tests/test_suggestion_capture.py`.
- `server_panel.py` — MCP server discovery from `~/.claude.json`, health checks, `ServerStatusBar`
- `userSetup_snippet.py` — Ready-to-paste snippet for Maya's `userSetup.py`
- `project_context.py` — resolves the ShotGrid project from the `tk-maya` engine context at launch (Chat 69)

**How it works:**
1. **Auto-setup on first connect:** `maya_ping` / `maya_launch` call `_ensure_panel_installed()` which injects Python via Command Port to add `sys.path`, register the menu, and open the panel. No manual `userSetup.py` editing needed.
2. `install_menu()` creates "MCP Pipeline > Open Console" in Maya's menu bar
3. `show()` creates a `workspaceControl(retain=True)` docked next to AttributeEditor
4. `_build_panel()` is called by Maya's `uiScript` — wraps Qt pointer, creates `MCPChatWidget`
5. Maya callbacks push selection/scene context into the widget before each message
6. `claude_worker.py` spawns Claude CLI — all MCPs discovered via `~/.claude.json` automatically. It injects `SHOTGRID_PROJECT_ID` from the `tk-maya` engine's project (`project_context.resolve_engine_project`, captured at widget init) so fpt-mcp ShotGrid ops (e.g. `tk_publish`) target the launched project; absent (plain Maya) → `"0"` so a create fails rather than hitting a stale `.env` default. Chat 69.
7. Panel persists across Maya sessions (retain=True + uiScript auto-rebuilds on restore)

**System prompt (`build_system_prompt`)** is assembled conditionally per available
MCP server (`available_servers` from `~/.claude.json`). Two prompt conventions live
here and are guarded by `tests/test_system_prompt.py`:
- **INTENT→ACTION map** (maya server block): natural-language intents route to the
  deterministic dispatcher actions without the user knowing tool names — *publish /
  "sube el asset"* → `maya_session action=publish`; *turntable / giratoria / "review
  turntable"* → `maya_session action=review_turntable`. The matcher is the LLM, so
  robustness comes from this intent vocabulary, not a keyword router; the determinism
  lives in the tools. The "NEVER hand-build the playblast with execute_python" guard
  (Chat 72) is part of this block.
- **"version" disambiguation** (fpt server block, only when fpt-mcp is present): a
  ShotGrid **Version** entity = *review media* (`sg_uploaded_movie`, `sg_path_to_movie`,
  `sg_path_to_frames`) vs **file versioning** (`PublishedFile.version_number` → `_v###`,
  automatic inside publish). "Create a turntable review version" → `sg_create`
  type=Version + `sg_upload`→`sg_uploaded_movie` using `review_turntable`'s returned
  `version_code`.

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
  `num_ctx 16384` (bumped from 8192 in fpt-mcp Bucket D — ecosystem-wide value,
  since `qwen3.5-mcp` is a single Ollama model shared across fpt-mcp and maya-mcp
  on the same machine), `temperature 0.7`, `top_p 0.8`, `top_k 20`.
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

### Effort selector
A second combo in the Console panel header (mirroring the model selector) controls the
reasoning effort of the spawned `claude` subprocess: **Auto / Low / Medium / High / Max**,
default **Auto** (index 0). It only affects the MCP-spawned subprocess, never the user's
own top-level `claude` session.

| Selection | Subprocess env | Effect |
|---|---|---|
| **Auto** (default) | both `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING` and `CLAUDE_CODE_EFFORT_LEVEL` ABSENT | CLI uses its adaptive-thinking default |
| **Low / Medium / High / Max** | `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING="1"` + `CLAUDE_CODE_EFFORT_LEVEL=<level>` | adaptive thinking off, forced at that effort |

`Auto` clears both vars explicitly in `ClaudeWorker.run()` — Maya builds `run_env` from the
captured login-shell env (`_SHELL_ENV`), which could otherwise carry an inherited hardening
value into the child. The fixed levels are wired in `build_backend_env(model_id, backend, effort)`.

### Write-allowed models (RAG trust gates)
Only Claude models can write patterns via `learn_pattern`. Local models (Ollama) are
read-only — they can search docs but cannot persist new patterns. Configured via
`write_allowed_models` in `src/maya_mcp/config.json` (default: `["claude-opus", "claude-fable"]`). Self-learning is reserved for the two top cloud tiers; Sonnet and local models are read-only.

### viewport_capture fallback for non-vision models
`maya_viewport_capture` returns both the image (base64) and text metadata (path, resolution,
size). Models without vision capability (e.g. `qwen3.5:4b`, `glm-4.7-flash`) will receive
the text metadata but cannot analyze the image content. The screenshot file is still saved
to the specified `output_path` for manual inspection or later use. When using a non-vision
model, prefer `maya_scene_snapshot` (text-only scene state) over `maya_viewport_capture`.

### Prerequisites for local models

Operator-only setup. See [`docs/DEPLOY.md`](docs/DEPLOY.md) for
Ollama install and `qwen3.5-mcp` Modelfile setup.

### Full LLM strategy
See `MODEL_STRATEGY.md` in the ecosystem root for hardware configs, VRAM management,
update procedures, and architecture decisions.

---

**Keep this file updated when architecture, tools, or workflows change.**

---

## 12. MANDATORY: Update install.sh on tool changes

**RULE — NON-NEGOTIABLE:**
Whenever a tool is added, removed, or renamed in `src/maya_mcp/server.py`:
1. Update the tools list in `install.sh` (Step 6 — Pre-approve MCP tools)
2. The tool name format is `mcp__maya-mcp__<function_name>`
3. Run `bash -n install.sh` to verify syntax
4. Commit install.sh together with the server.py change — never separately

Forgetting this step means users get permission prompts on first use of the new tool.
