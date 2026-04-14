"""Background worker that runs Claude Code CLI and emits the response.

Runs in a QThread so the UI stays responsive.  Uses --output-format
stream-json to provide real-time progress feedback for long-running
operations (shape generation, texturing, ShotGrid queries, etc.).

Differences from fpt-mcp's worker:
  - Dynamic system prompt based on which MCP servers are available
  - Tool labels for the entire ecosystem (maya-mcp + fpt-mcp + flame-mcp)
  - Multi-context support (ShotGrid entity + Maya scene)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .qt_compat import QtCore

QThread = QtCore.QThread
Signal = QtCore.Signal


def _get_shell_env() -> dict:
    """Capture the user's full login-shell environment.

    Maya launches from Finder with a minimal env that lacks PATH entries,
    OAuth tokens, SSL cert paths, proxy settings, etc.  This function
    spawns a login shell (``zsh -l`` on macOS), sources the user's
    profile, and returns the resulting environment — identical to what
    the user gets in Terminal/iTerm.

    Falls back to os.environ with augmented PATH if the shell fails.
    """
    import glob as _glob

    # Try to get the real shell env.
    # Use ``-i`` (interactive) so .zshrc is sourced — that's where nvm,
    # NODE_EXTRA_CA_CERTS, proxy settings, and other critical vars live.
    # Fall through on failure to the manual augmentation below.
    for shell, flags in [
        ("/bin/zsh", ["-i", "-l", "-c"]),    # interactive + login
        ("/bin/zsh", ["-l", "-c"]),           # login only (fallback)
        ("/bin/bash", ["-l", "-c"]),
    ]:
        if not os.path.isfile(shell):
            continue
        try:
            result = subprocess.run(
                [shell] + flags + ["env"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,      # prevent interactive hang
            )
            if result.returncode == 0 and result.stdout.strip():
                env = {}
                for line in result.stdout.splitlines():
                    if "=" in line:
                        key, _, val = line.partition("=")
                        env[key] = val
                if "HOME" in env and "PATH" in env:
                    return env
        except Exception:
            continue

    # Fallback: augment Maya's limited env manually
    env = os.environ.copy()
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        os.path.expanduser("~/.volta/bin"),
        os.path.expanduser("~/.npm-global/bin"),
        os.path.expanduser("~/.local/bin"),
    ]
    nvm_dirs = sorted(
        _glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")),
        reverse=True,
    )
    if nvm_dirs:
        extra.insert(0, nvm_dirs[0])

    base = env.get("PATH", "/usr/bin:/bin")
    for p in extra:
        if os.path.isdir(p) and p not in base:
            base = p + ":" + base
    env["PATH"] = base
    return env


_SHELL_ENV = _get_shell_env()

# Maya injects SSL_CERT_FILE pointing to its bundled Python 2.7 cert
# bundle (inside Maya.app).  Node.js picks this up and fails SSL
# verification against modern APIs.  Remove it so Node uses its own
# built-in CA store.
for _poison_var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
    _SHELL_ENV.pop(_poison_var, None)


def _find_claude() -> str:
    """Locate the claude CLI binary."""
    # Search with shell PATH so we find it inside Maya too
    found = shutil.which("claude", path=_SHELL_ENV.get("PATH", ""))
    if found:
        return found
    candidates = [
        os.path.expanduser("~/.npm-global/bin/claude"),
        "/usr/local/bin/claude",
        os.path.expanduser("~/.local/bin/claude"),
        "/opt/homebrew/bin/claude",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


CLAUDE_BIN = _find_claude()

# Repo root — used as cwd for Claude CLI so it picks up project-level
# MCP config from .claude/settings.json instead of requiring global config.
# console/claude_worker.py → parent = console/ → parent.parent = repo root
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# Max time for a single invocation (shape gen can take ~15 min)
TIMEOUT_SECONDS = 900

# ---------------------------------------------------------------------------
# Multi-backend model configuration
# ---------------------------------------------------------------------------

# Each entry: (display_label, model_id, backend)
AVAILABLE_MODELS = [
    # ── Anthropic cloud (default — needs internet + API key) ─────────
    ("Claude Sonnet 4.6",     "claude-sonnet-4-6",         "anthropic"),
    ("Claude Opus 4.6",       "claude-opus-4-6",           "anthropic"),
    # ── Self-hosted Ollama (glorfindel RTX 3090, LAN) ────────────────
    ("Qwen3.5 9B 🖥",         "qwen3.5-mcp",               "ollama"),
    ("GLM-4.7 Flash 🖥",      "glm-4.7-flash",             "ollama"),
    # ── Mac-local Ollama (offline, no LAN) ───────────────────────────
    ("Qwen3.5 9B 🍎",         "qwen3.5-mcp",               "ollama_mac"),
    ("Qwen3.5 4B 🍎",         "qwen3.5:4b",                "ollama_mac"),
]

# Models allowed to write RAG patterns (learn_pattern). Local models are read-only.
WRITE_ALLOWED_MODELS = ["claude-opus", "claude-sonnet"]

# Default Ollama URLs — can be overridden by src/maya_mcp/config.json
DEFAULT_OLLAMA_URL = "http://glorfindel:11434"
DEFAULT_OLLAMA_MAC_URL = "http://localhost:11434"

# Models with vision capability (for viewport_capture analysis)
VISION_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6", "qwen3.5-mcp", "qwen3.5:9b"}


def _load_config() -> dict:
    """Load config.json from the src/maya_mcp/ directory."""
    cfg_path = Path(_REPO_ROOT) / "src" / "maya_mcp" / "config.json"
    try:
        return json.loads(cfg_path.read_text())
    except Exception:
        return {}


def build_backend_env(model_id: str, backend: str) -> dict:
    """Return env-var overrides for the selected backend.

    For Ollama backends, redirects the Anthropic SDK to the Ollama
    Messages-compatible endpoint (Ollama v0.14+).

    Also hardens reasoning quality on every claude subprocess spawned
    from the Maya console panel: adaptive thinking off, effort level
    max. Set unconditionally so the behavior is identical regardless
    of backend switch order (Ollama ignores the vars in practice).
    The user controls their own top-level claude session via /effort —
    these overrides apply to the MCP-spawned subprocess only.
    """
    cfg = _load_config()
    env = {
        "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
    }

    if backend == "ollama":
        base_url = cfg.get("ollama_url", DEFAULT_OLLAMA_URL)
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"] = ""
    elif backend == "ollama_mac":
        base_url = cfg.get("ollama_mac_url", DEFAULT_OLLAMA_MAC_URL)
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"] = ""
    # anthropic backend: no overrides needed, uses default env

    return env


def model_has_vision(model_id: str) -> bool:
    """Return True if the model supports image analysis (viewport_capture)."""
    return model_id in VISION_MODELS


# ---------------------------------------------------------------------------
# Dynamic system prompt builder
# ---------------------------------------------------------------------------

_WORKFLOW_SECTION = """\

IMPORTANT: There may be a CONVERSATION HISTORY before the current message. \
Read it carefully — if the user already chose a reference or a method, DO NOT ask \
again. Continue from where the conversation left off.

═══════════════════════════════════════════════════════════════════════
3D CREATION WORKFLOW
═══════════════════════════════════════════════════════════════════════

When the user asks to create/generate/model something 3D, follow these steps in order. \
If a step was already resolved in the history, skip it.

1. CHECK VISION3D: BEFORE offering options, call vision3d_health() \
to verify if the Vision3D server is running and accessible.
   - If available=true → offer both options (AI generation + Maya modeling)
   - If available=false → inform the user and only offer Maya modeling.

2. IDENTIFY ENTITY: If there's ShotGrid context (fpt-mcp available) → \
use sg_find to search. If not → ask user or proceed with Maya directly.

3. SEARCH REFERENCES: If fpt-mcp is available, search Versions, \
PublishedFiles, Notes with attachments. ALL in parallel.

4. PRESENT OPTIONS: references + method + quality in a single response.
   Methods: Vision3D AI (image-to-3D or text-to-3D) or Maya direct modeling.
   Quality presets: low (~1 min), medium (~2 min), high (~8 min), ultra (~12 min).

5. EXECUTE — granular Vision3D flow (start → poll → download → import in Maya)
   or direct Maya modeling (create_primitive + transform + assign_material).

6. POST-CREATION: offer maya_save_scene and tk_publish (if fpt-mcp available).

═══════════════════════════════════════════════════════════════════════
RENDERING WITH FLAME (if flame-mcp is available)
═══════════════════════════════════════════════════════════════════════

If the user asks to render, composite, or grade:
- Use flame-mcp tools to send the render job directly to Flame.
- Flame can import Maya scenes, OpenEXR sequences, and MOV files.

═══════════════════════════════════════════════════════════════════════
RULES
═══════════════════════════════════════════════════════════════════════
- NEVER repeat a question already answered in the history.
- ALWAYS use MCP tools. NEVER tell the user to do it manually.
- If Maya doesn't respond → maya_launch.
- If Vision3D doesn't respond → vision3d_health() for diagnostics.
- Text-to-3D: translate prompt to English if needed.
- Respond in the user's language. Be concise. Execute, don't explain.
"""


def build_system_prompt(available_servers: dict) -> str:
    """Generate system prompt based on which MCP servers are configured.

    Args:
        available_servers: dict from detect_mcp_servers() — keys are server names.

    Returns:
        Complete system prompt string for Claude Code CLI.
    """
    parts = [
        "You are a VFX pipeline assistant integrated into a multi-MCP ecosystem. "
        "You have access to these MCP servers:\n"
    ]

    if "maya-mcp" in available_servers:
        parts.append(
            "1. **maya-mcp** — Maya control + Vision3D GPU:\n"
            "   Maya basics: maya_launch, maya_ping, maya_create_primitive, maya_assign_material, "
            "maya_transform, maya_list_scene, maya_delete, maya_execute_python, "
            "maya_new_scene, maya_save_scene, maya_create_light, maya_create_camera\n"
            "   Mesh ops: maya_mesh_operation (extrude, bevel, boolean, combine, separate, smooth)\n"
            "   Animation: maya_set_keyframe (translate, rotate, scale, visibility per frame)\n"
            "   I/O: maya_import_file (OBJ, FBX, GLB, ABC, MA, MB with namespace/scale)\n"
            "   Capture: maya_viewport_capture (PNG/JPG grab), maya_scene_snapshot (full scene state)\n"
            "   UI: maya_shelf_button (create reusable shelf buttons in Maya)\n"
            "   Vision3D: vision3d_health, shape_generate_remote, shape_generate_text, "
            "texture_mesh_remote, vision3d_poll, vision3d_download"
        )

    if "fpt-mcp" in available_servers:
        parts.append(
            "2. **fpt-mcp** — ShotGrid API + Toolkit + RAG:\n"
            "   sg_find, sg_create, sg_update, sg_delete, sg_schema, "
            "sg_upload, sg_download, sg_batch, sg_text_search, sg_summarize, "
            "sg_revive, sg_note_thread, sg_activity, tk_resolve_path, tk_publish, "
            "search_sg_docs, learn_pattern, session_stats"
        )

    if "flame-mcp" in available_servers:
        parts.append(
            "3. **flame-mcp** — Autodesk Flame control + RAG:\n"
            "   execute_python (run Python inside Flame), search_flame_docs (RAG search),\n"
            "   list_libraries, list_reels, get_project_info, get_flame_version,\n"
            "   learn_pattern, session_stats"
        )

    parts.append(_WORKFLOW_SECTION)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool labels for UI progress
# ---------------------------------------------------------------------------

_TOOL_LABELS = {
    # maya-mcp — Maya tools
    "maya_ping": "Checking Maya connection",
    "maya_launch": "Launching Maya",
    "maya_create_primitive": "Creating primitive in Maya",
    "maya_assign_material": "Assigning material in Maya",
    "maya_transform": "Transforming object in Maya",
    "maya_list_scene": "Querying Maya scene",
    "maya_delete": "Deleting object in Maya",
    "maya_execute_python": "Running Python in Maya",
    "maya_new_scene": "Creating new Maya scene",
    "maya_save_scene": "Saving Maya scene",
    "maya_create_light": "Creating light in Maya",
    "maya_create_camera": "Creating camera in Maya",
    # maya-mcp — New tools (mesh, animation, I/O, capture, UI)
    "maya_mesh_operation": "Performing mesh operation in Maya",
    "maya_set_keyframe": "Setting keyframe in Maya",
    "maya_import_file": "Importing file into Maya",
    "maya_viewport_capture": "Capturing Maya viewport",
    "maya_scene_snapshot": "Taking Maya scene snapshot",
    "maya_shelf_button": "Creating shelf button in Maya",
    # maya-mcp — Vision3D tools
    "vision3d_health": "Checking Vision3D availability",
    "shape_generate_remote": "Starting image-to-3D generation (Vision3D)",
    "shape_generate_text": "Starting text-to-3D generation (Vision3D)",
    "texture_mesh_remote": "Starting texturing (Vision3D)",
    "vision3d_poll": "Polling Vision3D progress",
    "vision3d_download": "Downloading Vision3D results",
    # fpt-mcp — ShotGrid tools
    "sg_find": "Searching ShotGrid",
    "sg_create": "Creating entity in ShotGrid",
    "sg_update": "Updating ShotGrid",
    "sg_delete": "Deleting from ShotGrid",
    "sg_schema": "Querying ShotGrid schema",
    "sg_upload": "Uploading file to ShotGrid",
    "sg_download": "Downloading from ShotGrid",
    "sg_batch": "Running batch operation in ShotGrid",
    "sg_text_search": "Searching text across ShotGrid",
    "sg_summarize": "Aggregating ShotGrid data",
    "sg_revive": "Restoring entity in ShotGrid",
    "sg_note_thread": "Reading note thread from ShotGrid",
    "sg_activity": "Reading activity stream from ShotGrid",
    "tk_resolve_path": "Resolving Toolkit path",
    "tk_publish": "Publishing to ShotGrid",
    "search_sg_docs": "Searching ShotGrid documentation",
    "learn_pattern": "Learning validated pattern",
    "session_stats": "Fetching session statistics",
    # flame-mcp tools (real tool names from flame_mcp_server.py)
    "search_flame_docs": "Searching Flame documentation",
    "execute_python": "Executing Python in Flame",
    "list_libraries": "Listing Flame libraries",
    "list_reels": "Listing Flame reels",
    "get_project_info": "Getting Flame project info",
    "get_flame_version": "Getting Flame version",
    # Note: learn_pattern and session_stats are shared names across MCPs.
    # The prefix stripping resolves which MCP they belong to.
}


class ClaudeWorker(QThread):
    """Runs ``claude -p "prompt" --output-format stream-json --verbose``
    and emits progress events plus the final result.

    Signals:
        progress(str)          — status updates for the UI
        finished(str, bool)    — (final_text, is_error)
    """

    progress = Signal(str)
    finished = Signal(str, bool)

    def __init__(
        self,
        message: str,
        context: dict | None = None,
        history: list | None = None,
        available_servers: dict | None = None,
        model_id: str | None = None,
        backend: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._message = message
        self._context = context or {}
        self._history = history or []
        self._servers = available_servers or {}
        self._model_id = model_id
        self._backend = backend

    def _label_for_tool(self, tool_name: str) -> str:
        """Return a human-friendly label for an MCP tool name."""
        short = tool_name
        for prefix in ("mcp__fpt-mcp__", "mcp__maya-mcp__", "mcp__flame-mcp__"):
            if tool_name.startswith(prefix):
                short = tool_name[len(prefix):]
                break
        return _TOOL_LABELS.get(short, f"Running {short}")

    def run(self):  # noqa: D102
        if not CLAUDE_BIN or not os.path.isfile(CLAUDE_BIN):
            self.finished.emit(
                "Claude Code CLI not found.\n"
                "Install with:  npm install -g @anthropic-ai/claude-code",
                True,
            )
            return

        # Build prompt with conversation history
        parts = []

        if self._history:
            parts.append("=== CONVERSATION HISTORY ===")
            for msg in self._history:
                prefix = "USER" if msg["role"] == "user" else "ASSISTANT"
                text = msg["text"]
                if msg["role"] == "assistant" and len(text) > 500:
                    text = text[:500] + "..."
                parts.append(f"[{prefix}]: {text}")
            parts.append("=== END OF HISTORY ===\n")

        parts.append(self._message)

        if self._context:
            parts.append(f"[Pipeline context: {json.dumps(self._context)}]")

        prompt = "\n".join(parts)
        system_prompt = build_system_prompt(self._servers)

        try:
            # Build environment with backend-specific overrides
            run_env = {**_SHELL_ENV, "CLAUDE_NO_TELEMETRY": "1"}
            if self._model_id and self._backend:
                run_env.update(build_backend_env(self._model_id, self._backend))

            cmd = [CLAUDE_BIN, "-p", prompt,
                   "--output-format", "stream-json", "--verbose",
                   "--append-system-prompt", system_prompt]
            if self._model_id:
                cmd.extend(["--model", self._model_id])

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                text=True,
                env=run_env,
                cwd=_REPO_ROOT,
            )

            text_parts: list[str] = []
            active_tools: dict[int, str] = {}
            result_text = ""
            _text_buffer = ""

            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    text_parts.append(line)
                    continue

                ev_type = event.get("type", "")

                if ev_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        idx = event.get("index", 0)
                        tool_name = block.get("name", "unknown")
                        active_tools[idx] = tool_name
                        label = self._label_for_tool(tool_name)
                        self.progress.emit(f"{label}...")

                elif ev_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        chunk = delta.get("text", "")
                        text_parts.append(chunk)
                        _text_buffer += chunk
                        while "\n" in _text_buffer:
                            line_text, _text_buffer = _text_buffer.split("\n", 1)
                            line_text = line_text.strip()
                            if line_text:
                                self.progress.emit(line_text)

                elif ev_type == "content_block_stop":
                    idx = event.get("index", 0)
                    if idx in active_tools:
                        del active_tools[idx]
                        if active_tools:
                            remaining = list(active_tools.values())
                            self.progress.emit(
                                f"{self._label_for_tool(remaining[0])}..."
                            )
                        else:
                            self.progress.emit("Processing response...")

                elif ev_type == "result":
                    r = event.get("result", "")
                    if r:
                        result_text = r

                elif ev_type == "message":
                    content = event.get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))

                elif ev_type == "assistant":
                    msg = event.get("message", event.get("text", ""))
                    if msg:
                        text_parts.append(msg)

            proc.wait(timeout=TIMEOUT_SECONDS)

            response = result_text or "".join(text_parts).strip()

            if not response:
                stderr_out = proc.stderr.read().strip()
                if stderr_out:
                    response = stderr_out

            if not response:
                response = "No response from Claude."

            is_error = proc.returncode != 0
            self.finished.emit(response, is_error)

        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
            self.finished.emit(
                "Timeout: Claude did not respond within 15 min.", True
            )
        except Exception as exc:
            self.finished.emit(f"Error: {exc}", True)
