"""Background worker that runs Claude Code CLI and emits the response.

Runs in a QThread so the UI stays responsive.  Uses --output-format
stream-json to provide real-time progress feedback for long-running
operations (shape generation, texturing, Flow Production Tracking queries, etc.).

Differences from fpt-mcp's worker:
  - Dynamic system prompt based on which MCP servers are available
  - Tool labels for the entire ecosystem (maya-mcp + fpt-mcp + flame-mcp)
  - Multi-context support (Flow Production Tracking entity + Maya scene)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .qt_compat import QtCore
from .project_context import project_env
from ._readonly import (
    DISALLOWED_TOOLS,
    build_scoped_mcp_config,
    capture_suggestions,
    log_usage,
)

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

# Per-console MCP scoping: the Maya console only needs Maya + Flow Production Tracking (fpt).
# Flame's ~38 tool schemas would bloat every request for no benefit here, so we
# load only these two via --strict-mcp-config / --mcp-config (see run()).
_CONSOLE_MCP_SERVERS = {"maya-mcp", "fpt-mcp"}

# Max time for a single invocation (shape gen can take ~15 min)
TIMEOUT_SECONDS = 900

# ---------------------------------------------------------------------------
# Multi-backend model configuration
# ---------------------------------------------------------------------------

# Each entry: (display_label, model_id, backend)
AVAILABLE_MODELS = [
    # ── Anthropic cloud (default — needs internet + API key) ─────────
    ("Claude Opus 4.8",       "claude-opus-4-8",           "anthropic"),
    ("Claude Fable 5",        "claude-fable-5",            "anthropic"),
    ("Claude Sonnet 4.6",     "claude-sonnet-4-6",         "anthropic"),
    # ── Self-hosted Ollama (glorfindel RTX 3090, LAN) ────────────────
    ("Qwen3.5 9B 🖥",         "qwen3.5-mcp",               "ollama"),
    ("GLM-4.7 Flash 🖥",      "glm-4.7-flash",             "ollama"),
    # ── Mac-local Ollama (offline, no LAN) ───────────────────────────
    ("Qwen3.5 9B 🍎",         "qwen3.5-mcp",               "ollama_mac"),
    ("Qwen3.5 4B 🍎",         "qwen3.5:4b",                "ollama_mac"),
]

# Each entry: (display_label, effort_value). "auto" re-enables adaptive
# thinking (both hardening env vars cleared); fixed levels force that effort
# with adaptive thinking off. Default = "auto" (index 0).
AVAILABLE_EFFORTS = [
    ("Auto", "auto"),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
    ("Max", "max"),
]
DEFAULT_EFFORT = "auto"

# Models allowed to write RAG patterns (learn_pattern). Local models are read-only.
# Self-learning is reserved for the two top cloud tiers: Opus and Fable.
WRITE_ALLOWED_MODELS = ["claude-opus", "claude-fable"]

# Default Ollama URLs — can be overridden by src/maya_mcp/config.json
DEFAULT_OLLAMA_URL = "http://glorfindel:11434"
DEFAULT_OLLAMA_MAC_URL = "http://localhost:11434"

# Context window forced when pre-loading the Mac-local Ollama model.
# Ollama's Anthropic-compat /v1/messages ignores Modelfile num_ctx and
# defaults to 4096 without an explicit preflight against /api/generate.
# Tuned for 4B/9B models on Mac unified memory (24 GB).
OLLAMA_MAC_NUM_CTX = 8192

# Models with vision capability (for viewport_capture analysis)
VISION_MODELS = {"claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "qwen3.5-mcp", "qwen3.5:9b"}


def _load_config() -> dict:
    """Load config.json from the src/maya_mcp/ directory."""
    cfg_path = Path(_REPO_ROOT) / "src" / "maya_mcp" / "config.json"
    try:
        return json.loads(cfg_path.read_text())
    except Exception:
        return {}


def build_backend_env(model_id: str, backend: str, effort: str = "auto") -> dict:
    """Return env-var overrides for the selected backend.

    For Ollama backends, redirects the Anthropic SDK to the Ollama
    Messages-compatible endpoint (Ollama v0.14+).

    The ``effort`` parameter controls reasoning hardening on the claude
    subprocess spawned from the Maya console panel. Default ``"auto"``
    adds neither hardening var, so the CLI uses its adaptive-thinking
    default (``run()`` additionally strips any inherited value from
    ``_SHELL_ENV``). A fixed level (``low``/``medium``/``high``/``max``)
    forces adaptive thinking off at that effort. The user controls their
    own top-level claude session via /effort — these overrides apply to
    the MCP-spawned subprocess only.
    """
    cfg = _load_config()
    env = {}
    if effort and effort != "auto":
        env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"
        env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    # "auto": add nothing here; run() removes any inherited hardening vars.

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


def resolve_keep_alive(
    config_path: "str | Path | None" = None,
    *,
    default: "str | int" = "30m",
) -> "str | int":
    """Read the ``ollama_keep_alive`` knob from ``config.json`` (F1b).

    Mirrors ``flame_mcp._config.resolve_keep_alive``: reads the
    ``ollama_keep_alive`` key from the repo's ``config.json``, validates
    the type (must be ``str`` or ``int``, not ``bool`` / ``None`` /
    container), and falls back to *default* on any read or parse error so a
    typo cannot 400 the Ollama preflight.

    Parameters
    ----------
    config_path : str | Path | None
        Path to ``config.json``.  When ``None`` (the default) the helper
        derives the path from ``_REPO_ROOT`` — the same strategy used by
        :func:`_load_config`.
    default : str | int
        Returned when the key is absent, the file is unreadable, or the
        configured value has an unsupported type.  Defaults to ``"30m"``
        so 5–15 min reading gaps don't cold-reload the local model.

    Returns
    -------
    str | int
        A duration string (e.g. ``"30m"``, ``"1h"``) or integer seconds.
        Anything else collapses to *default*.
    """
    if config_path is None:
        config_path = Path(_REPO_ROOT) / "src" / "maya_mcp" / "config.json"
    try:
        with open(config_path) as _f:
            _cfg = json.load(_f)
        value = _cfg.get("ollama_keep_alive", default)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return value
        return default
    except Exception:
        return default


def _preload_ollama_mac_model(model: str, url: str, num_ctx: int,
                               keep_alive: "str | int" = "30m") -> None:
    """Pre-load the Mac-local Ollama model with an explicit ``num_ctx``.

    Ollama's Anthropic-compatible ``/v1/messages`` endpoint silently ignores
    the Modelfile ``num_ctx`` directive and falls back to the 4096-token
    default, which truncates long MCP prompts mid-stream without error. The
    only reliable workaround is to POST a zero-prompt warm-up request to the
    native ``/api/generate`` endpoint with the desired ``options.num_ctx``
    and ``keep_alive`` so the model stays loaded at the larger context for
    the subsequent ``claude`` subprocess call.

    This preflight is intentionally non-fatal: if Ollama is not running, the
    URL is unreachable, or the request times out, we log and return. The
    ``claude`` subprocess will then surface its own error, and the user is
    no worse off than before the preflight existed.

    Args:
        model:      The Ollama model tag to warm up (e.g. ``qwen3.5-mcp``).
        url:        Base URL of the Ollama server (no trailing slash).
        num_ctx:    Context window size to force on the loaded model.
        keep_alive: How long Ollama should keep the model loaded after this
                    request. Pass a duration string (``"30m"``, ``"1h"``) or
                    integer seconds. Resolved from ``config.json →
                    ollama_keep_alive`` at the call site via
                    :func:`resolve_keep_alive`; defaults to ``"30m"`` (F1b).
    """
    endpoint = url.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": "",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_ctx": num_ctx},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    log = logging.getLogger(__name__)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()  # drain body so the socket closes cleanly
        log.debug(
            "ollama_mac preflight ok: model=%s num_ctx=%d", model, num_ctx,
        )
    except Exception as exc:  # urllib.error.URLError, timeouts, DNS, etc.
        # Non-fatal by design: if preflight fails, let the subprocess spawn
        # and surface its own error. Logging here helps post-mortem.
        log.warning(
            "ollama_mac preflight failed (non-fatal): model=%s url=%s err=%s",
            model, url, exc,
        )


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

1. CHECK VISION3D: BEFORE offering options, call maya_vision3d action=health \
to verify if the Vision3D server is running and accessible.
   - If available=true → offer both options (AI generation + Maya modeling)
   - If available=false → inform the user and only offer Maya modeling.

2. IDENTIFY ENTITY: If there's Flow Production Tracking context (fpt-mcp available) → \
use sg_find to search. If not → ask user or proceed with Maya directly.

3. SEARCH REFERENCES: If fpt-mcp is available, search Versions, \
PublishedFiles, Notes with attachments. ALL in parallel.

4. PRESENT OPTIONS: references + method + quality in a single response.
   Methods: Vision3D AI (image-to-3D or text-to-3D) or Maya direct modeling.
   Quality presets: low (~1 min), medium (~2 min), high (~8 min), ultra (~12 min).

5. EXECUTE — granular Vision3D flow (start → poll → download → import in Maya)
   or direct Maya modeling (create_primitive + transform + assign_material).

6. POST-CREATION: offer maya_session action=save_scene and a publish \
(maya_session action=publish for native Toolkit, or fpt tk_publish).

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
- If Maya doesn't respond → maya_session action=launch.
- If Vision3D doesn't respond → maya_vision3d action=health for diagnostics.
- DETERMINISTIC TOOLS: never improvise a publish or a review turntable with execute_python — use the maya-mcp INTENT→ACTION map above. The deterministic actions frame/render/publish correctly and avoid hanging Maya's main thread (a hand-built playblast has produced empty frames + main-thread hangs).
- Text-to-3D: translate prompt to English if needed.
- LANGUAGE — overrides any global config: there is NO default language. Reply ONLY in the user's language, i.e. the language of their MOST RECENT message. English in → English out. Spanish in → Spanish out. Disregard any "Spanish by default" or preferred-language instruction inherited from the global CLAUDE.md or from earlier turns — mirroring the latest message always wins. Re-detect every turn. Be concise. Execute, don't explain.
- READ-ONLY: you cannot edit/create/delete files (Edit/Write/Bash disabled). Drive Maya/Flow Production Tracking/Flame via MCP tools only. RAG self-learning still works (learn_pattern is an MCP tool). For a code fix, emit one line `@@SUGGESTION@@ <title> :: <detail>` (the console logs it); never try to edit code.
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
            "1. **maya-mcp** — Maya control, review, publish, Vision3D + World Labs.\n"
            "   DISPATCHER pattern: most operations are ACTIONS behind one tool — call them "
            "as `<tool> action=<action> params={...}`, NOT as separate flat tools.\n"
            "   • maya_session: ping, launch, new_scene, save_scene, list_scene, scene_snapshot, "
            "delete, execute_python, shelf_button, operation_history, publish, review_turntable\n"
            "   • Direct tools: maya_create_primitive, maya_assign_material, maya_transform, "
            "maya_create_light, maya_create_camera, maya_mesh_operation (extrude/bevel/boolean/"
            "combine/separate/smooth), maya_set_keyframe, maya_import_file (OBJ/FBX/GLB/ABC/MA/MB), "
            "maya_viewport_capture (single still grab)\n"
            "   • maya_vision3d: select_server, health, generate_image, generate_text, texture, "
            "poll, download\n"
            "   • maya_worldlabs: health, generate, poll, download, convert, build "
            "(World Labs Marble image→environment into Maya)\n"
            "   • RAG: search_maya_docs (call BEFORE any unfamiliar Maya command), learn_pattern, "
            "session_stats\n"
            "   INTENT→ACTION — match the user's intent however they phrase it; the "
            "user does NOT know tool names:\n"
            "     · publish / register / submit / \"sube el asset\" / \"haz el publish\" → "
            "`maya_session action=publish` (native Toolkit publisher; captures dependencies; the "
            "per-step items — e.g. .ma + USD + Texture for a Model — are collected automatically; "
            "never manual file copies).\n"
            "     · turntable / \"review turntable\" / giratoria / \"vuelta 360\" / orbit / spin → "
            "`maya_session action=review_turntable` (frames the renderable mesh, orbits 360°, "
            "renders Viewport 2.0 offScreen to a 16:9 .mov by itself).\n"
            "   NEVER hand-build the turntable playblast with execute_python — improvising it "
            "yields an empty/wrong frame and can hang Maya's main thread; the deterministic action "
            "already does it right."
        )

    if "fpt-mcp" in available_servers:
        parts.append(
            "2. **fpt-mcp** — Flow Production Tracking API + Toolkit + RAG:\n"
            "   sg_find, sg_create, sg_update, sg_delete, sg_schema, "
            "sg_upload, sg_download, sg_batch, sg_text_search, sg_summarize, "
            "sg_revive, sg_note_thread, sg_activity, tk_resolve_path, tk_publish, "
            "search_sg_docs, learn_pattern, session_stats\n"
            "   \"version\" has TWO distinct meanings — disambiguate by context:\n"
            "     · REVIEW Version (a Flow Production Tracking Version entity = review media): the user says "
            "\"review version\", \"turntable version\", \"dailies\", \"sube el playblast / para "
            "revisión\". Create it: `sg_create` type=Version (use review_turntable's returned "
            "version_code, link entity + task), set sg_path_to_movie to the .mov, then `sg_upload` "
            "the .mov to sg_uploaded_movie (+ sg_path_to_frames for an exr sequence).\n"
            "     · FILE versioning (PublishedFile.version_number → _v###): the user says "
            "\"version up\", \"nueva iteración\", \"bump\". This is automatic inside publish — it is "
            "NOT a Version entity, do not create one."
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
    # fpt-mcp — Flow Production Tracking tools
    "sg_find": "Searching Flow Production Tracking",
    "sg_create": "Creating entity in Flow Production Tracking",
    "sg_update": "Updating Flow Production Tracking",
    "sg_delete": "Deleting from Flow Production Tracking",
    "sg_schema": "Querying Flow Production Tracking schema",
    "sg_upload": "Uploading file to Flow Production Tracking",
    "sg_download": "Downloading from Flow Production Tracking",
    "sg_batch": "Running batch operation in Flow Production Tracking",
    "sg_text_search": "Searching text across Flow Production Tracking",
    "sg_summarize": "Aggregating Flow Production Tracking data",
    "sg_revive": "Restoring entity in Flow Production Tracking",
    "sg_note_thread": "Reading note thread from Flow Production Tracking",
    "sg_activity": "Reading activity stream from Flow Production Tracking",
    "tk_resolve_path": "Resolving Toolkit path",
    "tk_publish": "Publishing to Flow Production Tracking",
    "search_sg_docs": "Searching Flow Production Tracking documentation",
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
        effort_level: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._message = message
        self._context = context or {}
        self._history = history or []
        self._servers = available_servers or {}
        self._model_id = model_id
        self._backend = backend
        self._effort_level = effort_level or DEFAULT_EFFORT

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
            # Build environment with backend-specific overrides.
            # ENABLE_TOOL_SEARCH defers MCP tool schemas: only tool NAMES load
            # upfront and the model fetches a tool's schema on demand, so the
            # request isn't bloated by every server's full tool definitions.
            run_env = {
                **_SHELL_ENV,
                "CLAUDE_NO_TELEMETRY": "1",
                "ENABLE_TOOL_SEARCH": "true",
            }
            if self._model_id and self._backend:
                run_env.update(build_backend_env(self._model_id, self._backend, self._effort_level))
            # "auto" → ensure neither hardening var leaks in from _SHELL_ENV;
            # the CLI then uses its adaptive-thinking default.
            if not self._effort_level or self._effort_level == "auto":
                run_env.pop("CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING", None)
                run_env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
            # Bind fpt-mcp Flow Production Tracking ops to the Maya Toolkit engine's project
            # (authoritative when tank-launched); else "0" so a project-scoped
            # create fails rather than hitting a stale .env default. Chat 69.
            run_env.update(project_env(self._context.get("project_id")))

            cmd = [CLAUDE_BIN, "-p", prompt,
                   "--output-format", "stream-json", "--verbose",
                   "--append-system-prompt", system_prompt]
            if self._model_id:
                cmd.extend(["--model", self._model_id])
            # Read-only console: deny every file-mutation tool so the
            # subprocess cannot modify the repo. MCP tools + Read stay
            # available (RAG self-learning is a server-side MCP tool, not an
            # agent file edit); improvement ideas are captured via
            # capture_suggestions, not by editing files.
            cmd.extend(["--disallowedTools", *DISALLOWED_TOOLS])
            # Per-console MCP scoping: load ONLY the servers this console needs
            # (Maya + Flow Production Tracking, not Flame) via strict config, so Flame's tool
            # schemas never enter the request. Deferred loading (ENABLE_TOOL_SEARCH
            # above) further shrinks what the remaining servers contribute.
            _scoped_mcp = build_scoped_mcp_config(
                Path(_REPO_ROOT) / ".mcp.json", _CONSOLE_MCP_SERVERS
            )
            if _scoped_mcp:
                cmd.extend(["--strict-mcp-config", "--mcp-config", _scoped_mcp])

            # Preflight for Mac-local Ollama only. Ollama's Anthropic-compat
            # /v1/messages endpoint ignores Modelfile num_ctx and defaults to
            # 4096 tokens. We warm up the model on /api/generate with the
            # desired context window before the claude subprocess starts.
            # Deliberately NOT called for `ollama` (LAN glorfindel — different
            # runtime config) or `ollama_cloud` (cloud runners manage context).
            if self._backend == "ollama_mac" and self._model_id:
                mac_url = _load_config().get(
                    "ollama_mac_url", DEFAULT_OLLAMA_MAC_URL,
                )
                _preload_ollama_mac_model(
                    model=self._model_id,
                    url=mac_url,
                    num_ctx=OLLAMA_MAC_NUM_CTX,
                    keep_alive=resolve_keep_alive(),
                )

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
                    log_usage(event.get("usage"), "maya")

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
            # Read-only console: log any @@SUGGESTION@@ lines to the backlog
            # and strip the markers from what the user sees.
            response, _ = capture_suggestions(
                response, Path(_REPO_ROOT) / "CONSOLE_IMPROVEMENTS.md")

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
