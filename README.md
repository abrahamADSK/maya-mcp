# maya-mcp

> **Image → 3D → Maya** — End-to-end pipeline that converts a 2D reference image into a fully textured, production-ready 3D mesh inside Autodesk Maya, powered by [Hunyuan3D-2](https://github.com/Tencent/Hunyuan3D-2) running on a remote GPU server.

---

## Overview

This project connects three systems:

1. **Local Mac** running Autodesk Maya — the final destination for the 3D asset
2. **Remote GPU server** (Linux, NVIDIA RTX 3090 or better) — runs Hunyuan3D-2 for shape generation and texture painting
3. **MCP Server** (`core/`) — exposes Maya tools as MCP tools so Claude and other LLMs can control Maya via natural language

```
[Reference image]
      │
      ▼
[Remote GPU server]
  Hunyuan3D-2 DiT   → shape generation  → mesh.glb
  Hunyuan3D-2 Paint → texture generation → mesh_uv.obj + texture_baked.png
      │
      ▼  (SCP transfer)
[Local Mac — Maya 2026]
  maya_import_hires.py     → import + scale + apply texture
  maya_fix_position_smooth.py → ground alignment + smooth subdivision
```

---

## Features

- **Full mode**: Reference image → shape generation (Hunyuan3D DiT) → texture painting (Hunyuan3D Paint) → Maya
- **Paint-only mode**: Existing mesh (`.glb`) → texture painting → Maya (faster, when geometry already exists)
- **Maya MCP server**: 13 tools to control Maya via Claude/LLM (create objects, assign materials, transform, render, shape generation, texturing, etc.)
- **Fully configurable via environment variables** — no hardcoded paths or hostnames
- **Clean Maya integration**: auto-scales imported mesh to match scene, applies baked texture, smooths normals

---

## Project Structure

```
maya-mcp/
├── core/                          # MCP Server (Claude ↔ Maya bridge)
│   ├── server.py                  # FastMCP server — 11 Maya tools
│   ├── maya_bridge.py             # TCP socket bridge → Maya Command Port :7001
│   └── requirements.txt           # fastmcp, pydantic
│
├── vision/                        # Image → 3D pipeline scripts
│   ├── pipeline_runner.py         # Orchestrator — SSH to GPU, runs shape + texture
│   ├── shape_remote.py            # Shape generation (Hunyuan3D DiT) — runs on GPU server
│   ├── texture_remote.py          # Texture painting (Hunyuan3D Paint) — runs on GPU server
│   ├── maya_import_hires.py       # Maya: import mesh_uv.obj, scale, apply texture
│   └── maya_fix_position_smooth.py # Maya: ground alignment + smooth subdivision
│
├── reference/                     # Input images and pipeline outputs (git-ignored)
│   ├── reference.jpg              # Default reference image
│   └── 3d_output/
│       └── 0/                     # Output subdir (configurable via OUTPUT_SUBDIR)
│           ├── mesh.glb           # Shape output (or pre-existing geometry)
│           ├── mesh_uv.obj        # Textured mesh with UVs
│           ├── texture_baked.png  # Baked texture map
│           └── textured.glb       # Preview GLB with embedded texture
│
├── .env.example                   # Configuration template — copy to .env
├── .gitignore
└── README.md
```

---

## Prerequisites

### Local machine (Mac)
- macOS Ventura or later (Apple Silicon supported)
- Autodesk Maya 2023+ (tested on 2026)
- Arnold (`mtoa` plugin, included with Maya)
- Python 3.10+ (for running `pipeline_runner.py`)
- SSH access to the GPU server

### Remote GPU server (Linux)
- NVIDIA GPU with ≥16 GB VRAM (RTX 3090 recommended; tested with 24 GB)
- CUDA 11.8+ and cuDNN
- Python 3.10+
- [Hunyuan3D-2](https://github.com/Tencent/Hunyuan3D-2) installed with model weights downloaded

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/abrahamADSK/maya-mcp.git
cd maya-mcp
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your actual values
```

Key variables to set in `.env`:

| Variable | Description | Example |
|----------|-------------|---------|
| `GPU_SSH_HOST` | SSH alias or `user@host` for the GPU server | `my-gpu` (alias) or `flame@192.168.1.50` |
| `GPU_REMOTE_BASE` | Root of ai-studio install on GPU server | `/opt/ai-studio` |
| `GPU_VENV` | Python venv on GPU server | `/opt/ai-studio/vision/.venv` |
| `GPU_MODELS_DIR` | Hunyuan3D model weights directory | `/opt/ai-studio/vision/hf_models` |
| `PROJECT_DIR` | Absolute path to this repo on your Mac | `/Users/you/projects/maya-mcp` |

#### SSH setup (required for GPU communication)

The MCP server connects to the GPU box via SSH. Passwordless SSH **must** work before anything else.

**1. Create a dedicated SSH key** (if you don't have one):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/maya_gpu -N "" -C "maya-mcp GPU access"
ssh-copy-id -i ~/.ssh/maya_gpu.pub flame@your-gpu-host
```

**2. Add an SSH config alias** (`~/.ssh/config`):

```
Host my-gpu
    HostName your-gpu-host      # hostname or IP of the Linux GPU machine
    User flame                  # Linux username on the GPU server
    IdentityFile ~/.ssh/maya_gpu
```

**3. Verify** (must print OK with no password prompt):

```bash
ssh my-gpu echo "OK"
```

**4. Set `GPU_SSH_HOST`** to the alias name:

```
GPU_SSH_HOST=my-gpu
```

> **Common mistake:** Using the raw hostname (e.g. `glorfindel`) instead of the SSH alias (e.g. `my-gpu`). If your `~/.ssh/config` defines `Host my-gpu` with `HostName glorfindel`, then `GPU_SSH_HOST` must be `my-gpu` — otherwise SSH won't find the right user, key, or hostname mapping.

### 3. Set up the MCP server (core/)

```bash
cd core
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Set up Maya Command Port

In Maya Script Editor → Python tab, run once (or add to `userSetup.py` for auto-start):

```python
import maya.cmds as cmds
import maya.utils

def open_command_port():
    if not cmds.commandPort(':7001', query=True):
        cmds.commandPort(name=':7001', sourceType='mel', echoOutput=False)
        print('Maya Command Port open on :7001')

maya.utils.executeDeferred(open_command_port)
```

To persist across sessions, add the above to:
```
~/Library/Preferences/Autodesk/maya/YEAR/scripts/userSetup.py
```

### 5. Set up the GPU server

On your remote GPU server:

```bash
# 1. Install Hunyuan3D-2 (follow the official repo)
git clone https://github.com/Tencent/Hunyuan3D-2.git
cd Hunyuan3D-2
pip install -e .

# 2. Download model weights (requires ~10 GB disk space)
python -c "
from huggingface_hub import snapshot_download
# Shape model (DiT)
snapshot_download('tencent/Hunyuan3D-2', allow_patterns='hunyuan3d-dit-v2-0-turbo/*')
# Paint model
snapshot_download('tencent/Hunyuan3D-2', allow_patterns='hunyuan3d-paint-v2-0-turbo/*')
"

# 3. Copy pipeline scripts to the server
scp vision/shape_remote.py vision/texture_remote.py user@gpu-server:/opt/ai-studio/vision/
```

---

## Usage

### Option A: From Maya (exec mode)

Run the full pipeline directly from Maya's Script Editor. The pipeline SSHes to the GPU server, generates shape + texture, and auto-imports the result:

```python
# In Maya Script Editor → Python tab
import os
os.environ['PROJECT_DIR'] = '/path/to/maya-mcp'
os.environ['GPU_SSH_HOST'] = 'user@your-gpu-server'
# ... set other env vars, or use a .env loader

exec(open(os.path.join(os.environ['PROJECT_DIR'], 'vision/pipeline_runner.py')).read())
```

Or for paint-only (if you already have a mesh):

```python
# Place your mesh.glb in reference/3d_output/0/ first
exec(open('/path/to/maya-mcp/vision/pipeline_runner.py').read())
# Runs in paint-only mode when exec()'d (no __main__ guard)
```

### Option B: From terminal

```bash
# Load env vars
source .env  # or: export $(cat .env | xargs)

# Full pipeline: image → shape → texture → (manual Maya import)
python vision/pipeline_runner.py --mode full --image reference/reference.jpg

# Paint-only: existing mesh → texture → (manual Maya import)
python vision/pipeline_runner.py --mode paint-only --mesh reference/3d_output/0/mesh.glb
```

### Import into Maya (manual)

After the pipeline completes, import the result into Maya:

```python
# In Maya Script Editor → Python tab
exec(open('/path/to/maya-mcp/vision/maya_import_hires.py').read())
```

Then apply position correction and smooth subdivision:

```python
exec(open('/path/to/maya-mcp/vision/maya_fix_position_smooth.py').read())
```

---

## MCP Server (core/)

The MCP server exposes Maya as a set of tools that Claude or any MCP-compatible LLM can call via natural language.

### Starting the server

```bash
source core/.venv/bin/activate
python core/server.py
```

### Available tools

| Tool | Description |
|------|-------------|
| `maya_ping` | Verify connection, returns Maya version |
| `maya_create_primitive` | Create cube / sphere / cylinder / cone / plane / torus |
| `maya_assign_material` | Create and assign material (lambert / blinn / phong / aiStandardSurface) |
| `maya_transform` | Move / rotate / scale objects |
| `maya_list_scene` | List scene objects with optional filters |
| `maya_delete` | Delete objects |
| `maya_create_light` | Create directional / point / spot / area lights |
| `maya_create_camera` | Create cameras |
| `maya_new_scene` | New empty scene |
| `maya_save_scene` | Save current scene |
| `maya_execute_python` | Execute arbitrary Python code in Maya |
| `shape_generate_remote` | Generate 3D mesh from image via Hunyuan3D-2 DiT on remote GPU |
| `shape_generate_text` | Generate 3D mesh from text prompt (text-to-3D) on remote GPU |
| `texture_mesh_remote` | Texture an existing mesh via Hunyuan3D-2 Paint on remote GPU |

### Architecture

```
Claude / LLM
    ↕  MCP protocol
FastMCP server (core/server.py)
    ↕  TCP socket
Maya Command Port (:7001)
    ↕
Autodesk Maya 2026
```

### Configuring Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "maya-mcp": {
      "command": "/path/to/maya-mcp/.venv/bin/python",
      "args": ["core/server.py"],
      "cwd": "/path/to/maya-mcp"
    }
  }
}
```

The `cwd` field ensures the server can resolve relative paths and find the `.env` file.

### Configuring Claude Code

Claude Code uses **two separate files** for MCP configuration:

**1. MCP server definitions** — `~/.claude.json` (note: file in home dir, not inside `~/.claude/`):

```bash
# Add the server via CLI (recommended):
claude mcp add maya-mcp -s user -- /path/to/maya-mcp/.venv/bin/python /path/to/maya-mcp/core/server.py

# Or edit ~/.claude.json manually:
```

```json
{
  "mcpServers": {
    "maya-mcp": {
      "command": "/path/to/maya-mcp/.venv/bin/python",
      "args": ["/path/to/maya-mcp/core/server.py"],
      "env": {
        "GPU_SSH_HOST": "my-gpu",
        "GPU_REMOTE_BASE": "/opt/ai-studio",
        "GPU_VENV": "/opt/ai-studio/vision/.venv",
        "GPU_VISION_DIR": "/opt/ai-studio/vision",
        "GPU_WORK_DIR": "/opt/ai-studio/reference/3d_output"
      }
    }
  }
}
```

**2. Tool permissions** — `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__maya-mcp__maya_launch",
      "mcp__maya-mcp__maya_ping",
      "mcp__maya-mcp__maya_create_primitive",
      "mcp__maya-mcp__maya_assign_material",
      "mcp__maya-mcp__maya_transform",
      "mcp__maya-mcp__maya_list_scene",
      "mcp__maya-mcp__maya_delete",
      "mcp__maya-mcp__maya_create_light",
      "mcp__maya-mcp__maya_create_camera",
      "mcp__maya-mcp__maya_new_scene",
      "mcp__maya-mcp__maya_save_scene",
      "mcp__maya-mcp__maya_execute_python",
      "mcp__maya-mcp__shape_generate_remote",
      "mcp__maya-mcp__shape_generate_text",
      "mcp__maya-mcp__texture_mesh_remote"
    ]
  }
}
```

> **Important:** `mcpServers` must be in `~/.claude.json`, NOT in `~/.claude/settings.json`. The `settings.json` file is only for permissions and other settings. If you put `mcpServers` in the wrong file, `claude mcp list` will not show the server.

### Maya Command Port (required)

maya-mcp communicates with Maya via TCP on port 7001. Create `~/Library/Preferences/Autodesk/maya/2026/scripts/userSetup.py`:

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
    print(f"[MCP] Command Port abierto en {port_name}")

cmds.evalDeferred(open_command_port)
```

Restart Maya after creating this file. Verify with: `echo 'print("OK")' | nc localhost 7001`

### Cross-MCP pipeline (maya-mcp + fpt-mcp)

When both maya-mcp and fpt-mcp are configured in the same Claude Code or Claude Desktop instance, Claude can orchestrate end-to-end VFX workflows in a single conversation. For example, Claude can query ShotGrid for an asset's reference image via fpt-mcp, download it, generate a 3D model via Hunyuan3D-2, import it into Maya, and register the publish back in ShotGrid — all from one natural language request.

To enable this, add both servers to `~/.claude.json` (via `claude mcp add -s user`) and include permissions for both in `~/.claude/settings.json`. See the fpt-mcp README for its server configuration.

---

## Configuration Reference

All configurable values are set via environment variables. Copy `.env.example` to `.env` and fill in your values. The `.env` file is git-ignored and never committed.

```bash
# Remote GPU server
GPU_SSH_HOST=my-gpu                 # SSH alias from ~/.ssh/config (recommended)
                                    # or user@host (e.g. flame@192.168.1.50)
GPU_REMOTE_BASE=/opt/ai-studio      # Root of ai-studio install on GPU server
GPU_VENV=/opt/ai-studio/vision/.venv
GPU_VISION_DIR=/opt/ai-studio/vision
GPU_WORK_DIR=/opt/ai-studio/reference/3d_output
GPU_MODELS_DIR=/opt/ai-studio/vision/hf_models

# Local project
PROJECT_DIR=/path/to/maya-mcp
OUTPUT_SUBDIR=0                     # Subdirectory under reference/3d_output/
REFERENCE_IMAGE=/path/to/ref.jpg    # Default reference image

# Timeouts (seconds)
SHAPE_TIMEOUT=900                   # 15 min for shape generation
TEXTURE_TIMEOUT=600                 # 10 min for texture generation

# Maya MCP server
MAYA_HOST=localhost
MAYA_PORT=7001
```

---

## Troubleshooting

**Shape inference fails immediately (< 10 seconds)**
The model weights may not be fully downloaded. Check that `hunyuan3d-dit-v2-0-turbo/model.fp16.safetensors` (~4.6 GB) exists in `GPU_MODELS_DIR`. Re-run `snapshot_download` if missing.

**`texture_baked.png` not found after pipeline**
The texture extraction fallback may not have triggered. Use `textured.glb` directly, or check GPU server logs for extraction errors.

**Scale factor looks wrong in Maya (giant or tiny mesh)**
Freeze transformations on your base mesh before running the pipeline: `Modify → Freeze Transformations` in Maya.

**SSH asks for password / "Connection refused"**
This almost always means `GPU_SSH_HOST` doesn't match your `~/.ssh/config` alias. Run `ssh -v $GPU_SSH_HOST echo OK` and check that it uses the correct user, key, and hostname. Common causes: using the raw hostname (`glorfindel`) instead of the alias (`my-gpu`), missing `ssh-copy-id`, or wrong permissions on `~/.ssh/` (must be 700) or `~/.ssh/authorized_keys` (must be 600) on the server.

**Maya Command Port not responding**
Confirm port 7001 is open: in Maya's Script Editor run `cmds.commandPort(':7001', query=True)`. If `False`, run the `open_command_port()` snippet above.

**`Import failed` when importing mesh_uv.obj**
Make sure the OBJ plugin is loaded: `Windows → Settings/Preferences → Plug-in Manager → objExport.bundle → Loaded`.

**Texture appears gray/black in viewport**
Press **6** in the Maya viewport to enable Textured mode. If still gray, check the file path in the `baked_tex_file` node's Attribute Editor.

---

## Requirements Summary

### core/requirements.txt
```
mcp>=1.26.0
pydantic>=2.0
```

### vision/ (GPU server)
```
torch>=2.0 (CUDA)
trimesh
Pillow
huggingface_hub
hy3dgen  (from Hunyuan3D-2 repo: pip install -e .)
```

---

## Security considerations

The current architecture uses **SSH + SCP** for all GPU communication. This works well for a single-user studio but has limitations:

**Current model (SSH):**
- SSH keys on disk (`~/.ssh/maya_gpu`) grant full shell access to the GPU server — if the key leaks, the attacker gets a shell, not just inference access.
- No per-tool authorization: anyone with SSH access can run any command, not just 3D generation.
- Not reusable from web interfaces (ComfyUI, FaceSwap/FaceFusion) without tunneling.
- Key distribution is manual — adding a new workstation means `ssh-copy-id` each time.

**Recommended hardening (current SSH setup):**
- Use `command=` restriction in `authorized_keys` to limit what the key can execute.
- Restrict the key to SCP + specific scripts only (no interactive shell).
- Set `PermitRootLogin no` and `AllowUsers flame` in `/etc/ssh/sshd_config`.
- Use `~/.ssh/config` aliases (not raw hostnames) to avoid user/key mismatches.

### Roadmap: REST API on GPU server

A future version will replace SSH with a **FastAPI service** running on the GPU server behind a **Caddy reverse proxy** (automatic HTTPS). This brings several advantages:

```
 Client (MCP / ComfyUI / Web UI / curl)
        │  HTTPS + API key
        ▼
 Caddy (TLS termination, auth, rate-limiting)
        │  HTTP (localhost)
        ▼
 FastAPI (GPU server, port 8000)
   POST /api/generate-shape     — image-to-3D
   POST /api/generate-text      — text-to-3D
   POST /api/texture-mesh       — texture painting
   GET  /api/jobs/{id}/status   — poll progress
   WS   /ws?client_id=...       — real-time progress
```

Benefits over SSH: scoped permissions (API key grants inference only, not shell access), reusable from any HTTP client (ComfyUI workflows, FaceSwap, browser), automatic TLS certificates via Caddy, WebSocket progress updates for long jobs, and no SSH key distribution.

See [issue #X] for tracking. Contributions welcome.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

Hunyuan3D-2 model weights are subject to [Tencent's license](https://github.com/Tencent/Hunyuan3D-2/blob/main/LICENSE).
