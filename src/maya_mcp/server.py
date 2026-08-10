#!/usr/bin/env python3
"""Maya MCP Core Server — MCP server for controlling Autodesk Maya.

Core module: scene operations, objects, transforms, materials, modeling,
animation, I/O, viewport capture, and Vision3D integration.
Communicates with Maya via Command Port (TCP) using maya_bridge.

Features:
    - 9 Tier-1 Maya tools (always visible)
    - 11 session actions (behind maya_session dispatch)
    - 7 Vision3D tools (behind maya_vision3d dispatch)
    - 4 RAG tools (search_maya_docs, learn_pattern, session_stats, reset_session_stats)
    - Dangerous pattern detection (safety.py)
    - Hybrid search: ChromaDB + BM25 + HyDE + RRF fusion
    - Token tracking with RAG savings measurement
    - Model trust gates for self-learning

Usage:
    python server.py                    # stdio transport (MCP standard)
    python server.py --transport http   # HTTP transport (dev/debug)

Environment variables (see .env.example):
    MAYA_HOST          — host where Maya is running (default: localhost)
    MAYA_PORT          — Maya Command Port (default: 8100)
    GPU_API_URL        — GPU API server URL (e.g. http://your-gpu-host:8000)
    GPU_API_KEY        — API key for authentication (empty for open LAN access)
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import json
import os
import re
import time
from typing import Optional, List
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import Context, FastMCP, Image

from maya_mcp.maya_bridge import MayaBridge, MayaBridgeError
from maya_mcp.safety import check_dangerous
from maya_mcp._session_stats import (
    apply_idle_reset,
    make_empty_stats,
    persist_timing as _persist_timing,
    reset_stats as _reset_stats_helper,
)
from maya_mcp._ast_validate import validate_python, format_issues
from maya_mcp import _audit
from maya_mcp import color_policy
from maya_mcp import _review_encode
from maya_mcp.error_scrub import safe_error_message
from maya_mcp.publish import (
    PUBLISH_EXECUTE_CODE as _PUBLISH_EXECUTE_CODE,
    PUBLISH_PREVIEW_CODE as _PUBLISH_PREVIEW_CODE,
    expand_tokens as _expand_tokens,
)

_SERVER_DIR = Path(__file__).parent          # src/maya_mcp/
_PROJECT_ROOT = _SERVER_DIR.parent.parent    # maya-mcp/

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAYA_HOST = os.environ.get("MAYA_HOST", "localhost")
MAYA_PORT = int(os.environ.get("MAYA_PORT", "8100"))
# Which Maya to launch. NEVER a bare app name — see _resolve_maya_app().
# Accepts an absolute .app bundle path, or a selector matching exactly one
# installed bundle (e.g. "2027"). Empty → discovered from MAYA_APP_GLOB.
MAYA_APP = os.environ.get("MAYA_APP", "").strip()
MAYA_APP_GLOB = os.environ.get("MAYA_APP_GLOB", "/Applications/Autodesk/maya*/Maya.app")

# ---------------------------------------------------------------------------
# Token tracking (mirrors fpt-mcp / flame-mcp architecture)
# ---------------------------------------------------------------------------

_FULL_DOC_TOKENS = 14000  # combined size of all indexed docs

# Canonical stats dict. Schema lives in maya_mcp._session_stats.make_empty_stats
# so the initialiser and the reset path cannot drift (invariant: stats_keys_schema_shared).
_stats = make_empty_stats()
# Records when _stats was last reset (server start, idle-gap auto-reset, or explicit reset).
_stats_reset_at = datetime.datetime.now()
# Timestamp of the previous MCP tool call — drives the idle-gap auto-reset.
_last_call_at: Optional[datetime.datetime] = None

# F0 baseline telemetry: persistent JSONL stream that survives server restarts
# (the in-memory ring buffer in _stats['timings'] holds only the last 20 entries).
# Written best-effort; failures never propagate.
_TIMINGS_LOG = _SERVER_DIR / "logs" / "timings.jsonl"

# Durable, append-only audit log of tool executions (forensics/accountability —
# distinct from the F0 efficiency stream above). Opt-in via the MAYA_AUDIT_LOG
# env var: OFF by default, so when unset the audit path is a no-op (no file, no
# perf/disk/privacy impact, no behaviour change). Sibling of timings.jsonl under
# the git-ignored logs/ dir. Record shape + sanitisation live in maya_mcp._audit;
# persistence reuses _session_stats.persist_timing (5 MB + .1 rotation).
_AUDIT_LOG = _SERVER_DIR / "logs" / "audit.jsonl"

# RAG state
_last_rag_score: int = 100
_rag_called_this_session: bool = False


def _tok(text: str) -> int:
    """Rough token estimate: 1 token ~ 3 characters."""
    return max(1, len(text) // 3)


def _rating(tokens: int) -> str:
    if tokens < 500:
        return "low"
    elif tokens < 2000:
        return "medium"
    return "high"



# ---------------------------------------------------------------------------
# Model trust gates (C5 — from fpt-mcp / flame-mcp)
# ---------------------------------------------------------------------------

WRITE_ALLOWED_MODELS = {
    # Self-learning is reserved for the two top cloud tiers: Opus and Fable.
    # Sonnet and local models (Qwen/GLM) are read-only.
    "claude-opus", "claude-fable",
}


def _get_config() -> dict:
    try:
        return json.loads((_SERVER_DIR / "config.json").read_text())
    except Exception:
        return {}


def _get_current_model() -> str:
    return _get_config().get("model", "unknown")


def _model_can_write() -> bool:
    model = _get_current_model().lower()
    cfg_list = _get_config().get("write_allowed_models")
    if cfg_list:
        return any(allowed.lower() in model for allowed in cfg_list)
    return any(allowed in model for allowed in WRITE_ALLOWED_MODELS)


# Idle window (seconds) after which _stats is auto-zeroed on the next call.
# Overridable via config.json -> stats_idle_reset_seconds (default 30 min).
_STATS_IDLE_RESET_SECONDS = int(
    _get_config().get("stats_idle_reset_seconds", 30 * 60)
)


def _track_call() -> None:
    """Update last-call timestamp; auto-reset _stats if the idle gap exceeded.

    Called at the top of the dispatcher and RAG/stats tool entry points (the
    realistic session entry points — every session pings or searches first).
    Idle threshold is _STATS_IDLE_RESET_SECONDS (default 30 min).
    """
    global _last_call_at, _stats_reset_at
    now = datetime.datetime.now()
    did_reset, reset_at = apply_idle_reset(
        _stats, now, _last_call_at,
        idle_reset_seconds=_STATS_IDLE_RESET_SECONDS,
    )
    if did_reset:
        _stats_reset_at = reset_at
    _last_call_at = now


def _track_timing(entry: dict) -> None:
    """F0: append a timing entry to the in-memory ring buffer (max 20) AND
    persist an enriched copy as a JSON line in logs/timings.jsonl.

    The ring buffer keeps `session_stats()` cheap; the JSONL stream gives
    cross-session baselines so future improvements can be measured against the
    F0 baseline without re-instrumenting. Persistence is best-effort: any I/O
    error is swallowed by `_persist_timing`. Enrichment adds timestamp, model,
    and backend; caller-passed keys win on collision.
    """
    _stats["timings"].append(entry)
    if len(_stats["timings"]) > 20:
        _stats["timings"].pop(0)

    cfg = _get_config()
    enriched = {
        "ts":        datetime.datetime.now().isoformat(timespec="seconds"),
        "model":     _get_current_model(),
        "backend":   cfg.get("backend", "anthropic"),
        "tool_name": "execute_python" if entry.get("op") == "exec"
                     else entry.get("op", "unknown"),
        **entry,
    }
    _persist_timing(_TIMINGS_LOG, enriched)


def _audit_record(tool: str, action: str, params, status: str) -> None:
    """Emit one best-effort durable audit entry for a tool execution.

    No-op unless the MAYA_AUDIT_LOG toggle is set (default OFF). Wrapped in a
    blanket try/except so an audit failure — serialisation, full disk, bad
    permissions — can NEVER break or slow down the tool call it records. The
    enrichment (model/backend) mirrors `_track_timing`; persistence reuses the
    rotating, best-effort `_session_stats.persist_timing` via `_audit.write_record`.

    Parameters
    ----------
    tool : str
        MCP tool name (e.g. "maya_session", "maya_transform").
    action : str
        Sub-action for dispatcher tools ("execute_python", "delete", "launch")
        or "-" for direct standalone tools.
    params :
        Raw params (dict or Pydantic model) — sanitised by `_audit.build_record`
        (execute_python code is truncated + hashed; payloads are never stored).
    status : str
        One of `_audit.VALID_STATUSES`.
    """
    try:
        if not _audit.audit_enabled():
            return
        cfg = _get_config()
        record = _audit.build_record(
            tool, action, params, status,
            model=_get_current_model(),
            backend=cfg.get("backend", "anthropic"),
        )
        _audit.write_record(_AUDIT_LOG, record)
    except Exception:
        pass  # Audit is best-effort — it must never break the tool call.


def _audited(tool_name: str):
    """Decorator: emit a best-effort audit entry for a standalone @mcp.tool
    mutation, deriving the status from the returned payload.

    Applied UNDER `@mcp.tool(...)` so FastMCP still introspects the real wrapped
    signature (preserved by `functools.wraps`). The wrapped handlers catch their
    own exceptions and always return a string, so the status is derived from
    that string via `_audit.status_from_output`. The audit path never alters the
    returned result and never raises out of the call.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(params, *args, **kwargs):
            result = await func(params, *args, **kwargs)
            _audit_record(tool_name, "-", params, _audit.status_from_output(result))
            return result
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "maya_mcp",
    instructions="""You are controlling Autodesk Maya via the maya-mcp server.

## MANDATORY WORKFLOW

1. For any Maya Python command you're unsure about — flag names, return values,
   correct syntax — call search_maya_docs FIRST.
   NEVER guess flag names, command syntax, or return value types.

2. The safety module will warn you about dangerous patterns. Heed its warnings.

3. Common hallucinations to avoid:
   - cmds.polyCube() returns a LIST [transform, shape], NOT a string
   - cmds.setAttr for compound types REQUIRES type= parameter
   - cmds.file(import=True) is WRONG — use i=True (import is a Python keyword)
   - Flag names use SHORT form: w= not width=, r= not radius=

4. When a working pattern succeeds and search_maya_docs returned < 60% relevance,
   call learn_pattern to save the validated pattern for future sessions.

5. Call session_stats at the end of multi-step tasks to report token efficiency.

6. Always wrap operations in undo chunks for safe rollback.
""",
)
bridge = MayaBridge(host=MAYA_HOST, port=MAYA_PORT)


# ─────────────────────────────────────────────
# Input Models (Pydantic)
# ─────────────────────────────────────────────

class PrimitiveType(str, Enum):
    CUBE = "cube"
    SPHERE = "sphere"
    CYLINDER = "cylinder"
    CONE = "cone"
    PLANE = "plane"
    TORUS = "torus"


class CreatePrimitiveInput(BaseModel):
    """Parameters for creating a 3D primitive."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    primitive_type: PrimitiveType = Field(..., description="Primitive type: cube, sphere, cylinder, cone, plane, torus")
    name: Optional[str] = Field(default=None, description="Object name (Maya generates one if omitted)")
    position: Optional[List[float]] = Field(default=None, description="Position [x, y, z] in world space", min_length=3, max_length=3)
    scale: Optional[List[float]] = Field(default=None, description="Scale [x, y, z]", min_length=3, max_length=3)
    rotation: Optional[List[float]] = Field(default=None, description="Rotation [x, y, z] in degrees", min_length=3, max_length=3)


class MaterialInput(BaseModel):
    """Parameters for creating and assigning a material."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    object_name: str = Field(..., description="Name of the object to assign the material to")
    material_name: Optional[str] = Field(default=None, description="Material name (generated if omitted)")
    color: List[float] = Field(..., description="Normalized RGB color [r, g, b] (0.0-1.0)", min_length=3, max_length=3)
    material_type: str = Field(default="lambert", description="Shader type: lambert, blinn, phong, aiStandardSurface")


class TransformInput(BaseModel):
    """Parameters for transforming an object."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    object_name: str = Field(..., description="Name of the object to transform")
    position: Optional[List[float]] = Field(default=None, description="New position [x, y, z]", min_length=3, max_length=3)
    rotation: Optional[List[float]] = Field(default=None, description="New rotation [x, y, z] in degrees", min_length=3, max_length=3)
    scale: Optional[List[float]] = Field(default=None, description="New scale [x, y, z]", min_length=3, max_length=3)
    relative: bool = Field(default=False, description="If True, transform relative to current position")


class SceneQueryInput(BaseModel):
    """Parameters for querying the scene."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    object_type: Optional[str] = Field(default=None, description="Filter by type: mesh, light, camera, transform, etc.")
    name_filter: Optional[str] = Field(default=None, description="Filter by name (supports wildcards: *sphere*)")


class ExecutePythonInput(BaseModel):
    """Execute arbitrary Python code in Maya."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    code: str = Field(..., description="Python code to execute in Maya. Assign result to variable 'result'.")
    timeout: Optional[float] = Field(
        default=None, ge=1, le=600,
        description=(
            "Max seconds to wait for Maya to finish (default 10). The Command "
            "Port runs code synchronously on Maya's main thread — raise this "
            "for long operations; progress heartbeats stream every 10s while "
            "waiting."
        ),
    )


class DeleteObjectInput(BaseModel):
    """Parameters for deleting objects."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    object_name: str = Field(..., description="Name of the object to delete (supports wildcards)")


class LightInput(BaseModel):
    """Parameters for creating a light."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    light_type: str = Field(default="directional", description="Type: directional, point, spot, area, ambient")
    name: Optional[str] = Field(default=None, description="Light name")
    intensity: float = Field(default=1.0, description="Light intensity", ge=0.0)
    color: Optional[List[float]] = Field(default=None, description="RGB color [r, g, b] (0.0-1.0)", min_length=3, max_length=3)
    position: Optional[List[float]] = Field(default=None, description="Position [x, y, z]", min_length=3, max_length=3)


class CameraInput(BaseModel):
    """Parameters for creating a camera."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: Optional[str] = Field(default=None, description="Camera name")
    position: Optional[List[float]] = Field(default=None, description="Position [x, y, z]", min_length=3, max_length=3)
    look_at: Optional[List[float]] = Field(default=None, description="Look at point [x, y, z]", min_length=3, max_length=3)
    focal_length: float = Field(default=35.0, description="Focal length in mm", ge=1.0, le=500.0)


# ─────────────────────────────────────────────
# Dispatch Models
# ─────────────────────────────────────────────

class SessionAction(str, Enum):
    """Actions available in the maya_session dispatch tool."""
    PING = "ping"
    LAUNCH = "launch"
    NEW_SCENE = "new_scene"
    SAVE_SCENE = "save_scene"
    LIST_SCENE = "list_scene"
    SCENE_SNAPSHOT = "scene_snapshot"
    DELETE = "delete"
    EXECUTE_PYTHON = "execute_python"
    SHELF_BUTTON = "shelf_button"
    OPERATION_HISTORY = "operation_history"
    PUBLISH = "publish"
    REVIEW_TURNTABLE = "review_turntable"
    RENDER_STILL = "render_still"


class SessionDispatchInput(BaseModel):
    """Input for the maya_session dispatch tool."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    action: SessionAction = Field(..., description="Which session action to run")
    params: Optional[dict] = Field(default=None, description="Parameters for the chosen action (see tool description)")


class PublishMode(str, Enum):
    """Mode for maya_session(action='publish')."""
    PREVIEW = "preview"
    PUBLISH = "publish"


class PublishInput(BaseModel):
    """Parameters for maya_session(action='publish').

    Drives the NATIVE tk-multi-publish2 PublishManager inside the engine'd Maya.
    'preview' collects the session and returns the publish tree (read-only).
    'publish' activates tasks per include/exclude, then validate->publish->finalize.
    """
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    mode: PublishMode = Field(
        default=PublishMode.PREVIEW,
        description="'preview' (read the tree, no side effects) or 'publish' (run it).")
    include: Optional[List[str]] = Field(
        default=None,
        description="Whitelist tokens (step/output/plugin/type) -- only matching "
                    "tasks publish. e.g. ['rig'], ['model','usd'].")
    exclude: Optional[List[str]] = Field(
        default=None,
        description="Blacklist tokens over the config defaults. e.g. ['render'] "
                    "('no render'/'sin render').")
    comment: Optional[str] = Field(
        default=None,
        description="Publish comment/description stamped on each active item.")
    timeout: Optional[float] = Field(
        default=None, ge=1, le=600,
        description="Command Port wait in seconds (default 120 preview / 600 publish).")


class Vision3DAction(str, Enum):
    """Actions available in the maya_vision3d dispatch tool."""
    SELECT_SERVER = "select_server"
    HEALTH = "health"
    GENERATE_IMAGE = "generate_image"
    GENERATE_TEXT = "generate_text"
    TEXTURE = "texture"
    POLL = "poll"
    DOWNLOAD = "download"


class Vision3DDispatchInput(BaseModel):
    """Input for the maya_vision3d dispatch tool."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    action: Vision3DAction = Field(..., description="Which Vision3D action to run")
    params: Optional[dict] = Field(default=None, description="Parameters for the chosen action (see tool description)")


class WorldLabsAction(str, Enum):
    """Actions available in the maya_worldlabs dispatch tool."""
    HEALTH = "health"
    GENERATE = "generate"
    POLL = "poll"
    DOWNLOAD = "download"
    CONVERT = "convert"
    BUILD = "build"
    STATUS = "status"


class WorldLabsDispatchInput(BaseModel):
    """Input for the maya_worldlabs dispatch tool."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    action: WorldLabsAction = Field(..., description="Which WorldLabs action to run")
    params: Optional[dict] = Field(default=None, description="Parameters for the chosen action (see tool description)")


# ─────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────

async def _run_cmd(cmd: List[str], timeout: int = 60) -> tuple:
    """Execute a local async command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"Timeout after {timeout}s"
    return proc.returncode, stdout.decode(), stderr.decode()


_MAYA_VERSION_RE = re.compile(r"maya(\d{4})")


def _discover_maya_apps() -> List[str]:
    """Installed Maya ``.app`` bundles, **newest first**.

    Deliberately mirrors fpt-mcp's ``software_resolver._os_scan_maya`` ordering
    (parse the year out of the path, sort descending, unparseable entries last)
    so both launchers agree on what "the default Maya" means.
    """
    import glob as _glob

    def _key(path: str) -> tuple:
        match = _MAYA_VERSION_RE.search(path)
        return (1, int(match.group(1))) if match else (0, 0)

    return sorted(_glob.glob(MAYA_APP_GLOB), key=_key, reverse=True)


def _resolve_maya_app() -> tuple:
    """Resolve WHICH Maya to open — the newest install, and SAY which one.

    Version authority (Chat 65/68 user rule): the version that should open for
    pipeline work is the one **ShotGrid Desktop marks as default**, which
    fpt-mcp's ``resolve_app`` reads off the SG ``Software`` entity —
    ``fpt_launch_app`` is the pipeline entry point and consults it. maya-mcp has
    no ShotGrid access, so ``launch`` mirrors that resolver's *fallback* layer:
    newest install, chosen deterministically, with a warning naming the others
    and pointing at the authoritative launcher. Never "newest" silently.

    What it must never do is ``open -a "Maya"``: that hands the choice to
    LaunchServices, which picks an arbitrary version and reports nothing
    (Chat 94) — the same trap as launching Flame by app name via AppleScript
    (``feedback_osascript_flame_version_trap``). Everything downstream is
    version-specific: the Command Port, the panel bootstrap, ``api_graph.json``,
    the publish templates.

    ``MAYA_APP`` stays available as an OPTIONAL pin (absolute bundle path, or a
    selector matching exactly one install) for a box that must not follow
    "newest" — it is not required for normal operation.

    :returns: ``(bundle, warnings, error)`` — exactly one of bundle/error set.
    """
    candidates = _discover_maya_apps()
    warnings: List[str] = []

    if MAYA_APP:
        if os.path.isabs(MAYA_APP):
            if os.path.exists(MAYA_APP):
                return MAYA_APP, warnings, None
            return None, warnings, {
                "error": f"MAYA_APP points at a bundle that does not exist: {MAYA_APP}",
                "candidates": candidates,
                "hint": "Fix MAYA_APP, or unset it to fall back to the newest install.",
            }
        matched = [c for c in candidates if MAYA_APP in c]
        if len(matched) == 1:
            return matched[0], warnings, None
        return None, warnings, {
            "error": (f"MAYA_APP={MAYA_APP!r} matches {len(matched)} installed Maya "
                      f"bundles — it must identify exactly one."),
            "candidates": candidates,
            "hint": "Use the absolute path of the Maya.app, or unset MAYA_APP.",
        }

    if not candidates:
        return None, warnings, {
            "error": "No Maya installation found.",
            "searched": MAYA_APP_GLOB,
            "hint": "Set MAYA_APP_GLOB if Maya lives outside /Applications/Autodesk.",
        }

    bundle = candidates[0]
    if len(candidates) > 1:
        warnings.append(
            f"{len(candidates)} Maya installs found; opened the newest ({bundle}). "
            f"Others: {', '.join(candidates[1:])}. For pipeline work the version to "
            f"open is the one ShotGrid Desktop marks as default — launch via "
            f"fpt_launch_app, which resolves it from the SG Software entity."
        )
    return bundle, warnings, None


def _handle_error(e: Exception) -> str:
    """Consistent error formatting.

    The exception text is scrubbed of credential-shaped tokens and
    length-bounded (300 chars) by the shared OPSEC helper
    (``error_scrub.safe_error_message``) before it reaches the model. The
    ``Maya error`` / ``Unexpected error`` prefixes are preserved so
    ``_audit.status_from_output`` still classifies the result as an error.
    """
    msg = safe_error_message(e)
    if isinstance(e, MayaBridgeError):
        return f"Maya error: {msg}"
    return f"Unexpected error: {type(e).__name__}: {msg}"


def _py_str(value: object) -> str:
    """Return a safe Python string literal for embedding *value* in generated
    Maya Python.

    The dedicated Maya tools (``maya_import_file``, ``maya_viewport_capture``,
    ``maya_create_primitive`` …) build Maya Python by f-string interpolation and
    — unlike ``maya_session(execute_python)`` / ``delete`` — do NOT run
    ``check_dangerous`` on the result. A free-form path or object name
    containing a quote, a backslash or a newline (e.g. a legitimate
    ``Director's_cut/char.obj``) would otherwise break out of the
    single-quoted literal: at best a ``SyntaxError`` on a valid asset, at worst
    an injection that skips the safety layer entirely.

    ``repr()`` emits a fully-escaped literal (choosing the quote style that
    keeps the value intact), so the interpolated value is always inert *data*,
    never executable code. Callers MUST drop the surrounding quotes they used
    to wrap the old ``'{value}'`` form — ``repr`` supplies them.
    """
    return repr("" if value is None else str(value))


async def _do_ping(params: dict) -> str:
    """Check connection to Maya and return environment info (version, current scene, renderer)."""
    try:
        info = await asyncio.to_thread(bridge.ping)
        _setup_maya_panel()
        return json.dumps(info, indent=2, ensure_ascii=False)
    except Exception as e:
        return _handle_error(e)


async def _do_launch(params: dict, ctx: Context | None = None) -> str:
    """Open Maya and wait for the Command Port to respond.

    When an MCP ``Context`` is provided, the 90-second wait loop streams
    visible progress to the client (``ctx.report_progress`` every poll +
    an ``ctx.info`` line every 15s) instead of staying silent until done.
    """
    import socket

    # 1. Check if already connected
    try:
        info = await asyncio.to_thread(bridge.ping)
        _setup_maya_panel()
        return json.dumps({
            "status": "already_running",
            "version": info.get("version", "unknown"),
            "message": "Maya is already open and Command Port is responding."
        }, ensure_ascii=False)
    except Exception:
        pass  # Not running or not responding — open it

    # 2. Launch Maya — by resolved bundle PATH, never by app name.
    bundle, app_warnings, resolve_error = _resolve_maya_app()
    if resolve_error:
        return json.dumps(resolve_error, ensure_ascii=False)
    if ctx and app_warnings:
        for _w in app_warnings:
            await ctx.info(_w)

    rc, _, err = await _run_cmd(["open", "-a", bundle], timeout=10)
    if rc != 0:
        return json.dumps({
            "error": f"Could not open Maya ({bundle}): {err.strip()}",
            "hint": "Verify that the bundle is a working Maya install; MAYA_APP in "
                    ".env overrides which one is used.",
        }, ensure_ascii=False)

    # 3. Wait for Command Port to be ready (max 90s)
    max_wait = 90
    poll_interval = 3
    waited = 0

    if ctx:
        await ctx.info(
            f"Maya launching ({bundle}) — waiting for Command Port "
            f"{MAYA_HOST}:{MAYA_PORT} (up to {max_wait}s)..."
        )

    while waited < max_wait:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        if ctx:
            await ctx.report_progress(waited, max_wait)
            if waited % 15 == 0:
                await ctx.info(f"Still waiting for Maya's Command Port... ({waited}/{max_wait}s)")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((MAYA_HOST, MAYA_PORT))
            sock.close()
            # Port open — try real ping
            try:
                info = await asyncio.to_thread(bridge.ping)
                _setup_maya_panel()
                if ctx:
                    await ctx.info(f"Maya ready after {waited}s.")
                return json.dumps({
                    "status": "launched",
                    "waited_seconds": waited,
                    "version": info.get("version", "unknown"),
                    # Which bundle was actually started — the whole point of
                    # resolving a path instead of an app name.
                    "app": bundle,
                    "warnings": app_warnings,
                    "message": f"Maya open and Command Port ready ({waited}s)."
                }, ensure_ascii=False)
            except Exception:
                continue  # Port open but Maya still loading
        except (ConnectionRefusedError, socket.timeout, OSError):
            continue  # Port not yet available

    return json.dumps({
        "error": f"Maya opened but Command Port did not respond in {max_wait}s.",
        "hint": "Verify that you have Command Port in userSetup.py: cmds.commandPort(name='localhost:8100', sourceType='mel')"
    }, ensure_ascii=False)


@mcp.tool(name="maya_create_primitive")
@_audited("maya_create_primitive")
async def maya_create_primitive(params: CreatePrimitiveInput) -> str:
    """Create a 3D primitive in Maya (cube, sphere, cylinder, cone, plane, torus) with optional position, scale, and rotation."""
    from maya_mcp.suggestions import maybe_annotate_with_suggestions
    try:
        create_funcs = {
            "cube": "cmds.polyCube",
            "sphere": "cmds.polySphere",
            "cylinder": "cmds.polyCylinder",
            "cone": "cmds.polyCone",
            "plane": "cmds.polyPlane",
            "torus": "cmds.polyTorus",
        }
        func = create_funcs[params.primitive_type.value]
        name_arg = f"name={_py_str(params.name)}" if params.name else ""

        code = f"""
import maya.cmds as cmds
obj = {func}({name_arg})[0]
"""
        if params.position:
            code += f"cmds.xform(obj, translation={params.position}, worldSpace=True)\n"
        if params.scale:
            code += f"cmds.xform(obj, scale={params.scale})\n"
        if params.rotation:
            code += f"cmds.xform(obj, rotation={params.rotation})\n"

        code += "result = {'name': obj, 'type': '" + params.primitive_type.value + "'}"

        return maybe_annotate_with_suggestions("maya_create_primitive", await asyncio.to_thread(bridge.execute, code))
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_assign_material")
@_audited("maya_assign_material")
async def maya_assign_material(params: MaterialInput) -> str:
    """Create a material (lambert, blinn, phong, aiStandardSurface) with RGB color and assign it to an object."""
    try:
        mat_name = params.material_name or f"{params.object_name}_mat"
        sg_name = f"{mat_name}_SG"
        r, g, b = params.color

        code = f"""
import maya.cmds as cmds
mat = cmds.shadingNode({_py_str(params.material_type)}, asShader=True, name={_py_str(mat_name)})
sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name={_py_str(sg_name)})
cmds.connectAttr(mat + '.outColor', sg + '.surfaceShader')
cmds.setAttr(mat + '.color', {r}, {g}, {b}, type='double3')
cmds.select({_py_str(params.object_name)})
cmds.sets(forceElement=sg)
result = {{'material': mat, 'shading_group': sg, 'assigned_to': {_py_str(params.object_name)}}}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_transform")
@_audited("maya_transform")
async def maya_transform(params: TransformInput) -> str:
    """Move, rotate, or scale an object in the Maya scene."""
    try:
        ws = "False" if params.relative else "True"
        rel = "True" if params.relative else "False"

        obj = _py_str(params.object_name)
        code = "import maya.cmds as cmds\n"
        if params.position:
            code += f"cmds.xform({obj}, translation={params.position}, worldSpace={ws}, relative={rel})\n"
        if params.rotation:
            code += f"cmds.xform({obj}, rotation={params.rotation}, worldSpace={ws}, relative={rel})\n"
        if params.scale:
            code += f"cmds.xform({obj}, scale={params.scale}, relative={rel})\n"

        code += f"""
pos = cmds.xform({obj}, q=True, translation=True, worldSpace=True)
rot = cmds.xform({obj}, q=True, rotation=True, worldSpace=True)
scl = cmds.xform({obj}, q=True, scale=True)
result = {{'object': {obj}, 'position': pos, 'rotation': rot, 'scale': scl}}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


async def _do_list_scene(params: dict) -> str:
    """List objects in the Maya scene, with optional filters by type or name."""
    from pydantic import ValidationError
    try:
        validated = SceneQueryInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for list_scene: {e}"})
    try:
        filters = []
        if validated.object_type:
            filters.append(f"type={_py_str(validated.object_type)}")
        if validated.name_filter:
            filters.append(_py_str(validated.name_filter))

        filter_str = ", ".join(filters)

        code = f"""
import maya.cmds as cmds
import json
objects = cmds.ls({filter_str}) or []
result = {{'count': len(objects), 'objects': objects}}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


async def _do_delete(params: dict) -> str:
    """Delete an object from the Maya scene by name (supports wildcards like *sphere*)."""
    from pydantic import ValidationError
    try:
        validated = DeleteObjectInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for delete: {e}"})

    _stats["exec_calls"] += 1

    # Safety check
    warning = check_dangerous(f'cmds.delete("{validated.object_name}")')
    if warning:
        _stats["safety_blocks"] += 1
        _audit_record("maya_session", "delete", params, _audit.AUDIT_SAFETY_BLOCKED)
        return json.dumps({"safety_warning": warning})

    try:
        obj = _py_str(validated.object_name)
        code = f"""
import maya.cmds as cmds
targets = cmds.ls({obj})
if targets:
    cmds.delete(targets)
    result = {{'deleted': targets}}
else:
    result = {{'error': 'Not found: ' + {obj}}}
"""
        response = await asyncio.to_thread(bridge.execute, code)
        _stats["tokens_out"] += _tok(response)
        _audit_record("maya_session", "delete", params,
                      _audit.status_from_output(response))
        return response
    except Exception as e:
        _audit_record("maya_session", "delete", params, _audit.AUDIT_ERROR)
        return _handle_error(e)


@mcp.tool(name="maya_create_light")
@_audited("maya_create_light")
async def maya_create_light(params: LightInput) -> str:
    """Create a light in Maya (directional, point, spot, area, ambient) with configurable intensity and color."""
    from maya_mcp.suggestions import maybe_annotate_with_suggestions
    try:
        light_funcs = {
            "directional": "cmds.directionalLight",
            "point": "cmds.pointLight",
            "spot": "cmds.spotLight",
            "area": "cmds.shadingNode('areaLight', asLight=True",
            "ambient": "cmds.ambientLight",
        }

        name_kw = f"name={_py_str(params.name)}" if params.name else ""

        if params.light_type == "area":
            extra = f", {name_kw}" if name_kw else ""
            code = f"""
import maya.cmds as cmds
light = cmds.shadingNode('areaLight', asLight=True{extra})
"""
        else:
            func = light_funcs.get(params.light_type, "cmds.directionalLight")
            code = f"""
import maya.cmds as cmds
light = {func}({name_kw})
"""

        code += f"cmds.setAttr(light + '.intensity', {params.intensity})\n"

        if params.color:
            r, g, b = params.color
            code += f"cmds.setAttr(light + '.color', {r}, {g}, {b}, type='double3')\n"

        if params.position:
            code += f"""
parent = cmds.listRelatives(light, parent=True)[0]
cmds.xform(parent, translation={params.position}, worldSpace=True)
"""

        code += "result = {'light': light, 'type': " + _py_str(params.light_type) + "}\n"

        return maybe_annotate_with_suggestions("maya_create_light", await asyncio.to_thread(bridge.execute, code))
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_create_camera")
@_audited("maya_create_camera")
async def maya_create_camera(params: CameraInput) -> str:
    """Create a camera in Maya with configurable position, look-at point, and focal length."""
    from maya_mcp.suggestions import maybe_annotate_with_suggestions
    try:
        name_arg = f"name={_py_str(params.name)}" if params.name else ""
        code = f"""
import maya.cmds as cmds
cam = cmds.camera({name_arg})[0]
cmds.setAttr(cam + '.focalLength', {params.focal_length})
"""
        if params.position:
            code += f"cmds.xform(cam, translation={params.position}, worldSpace=True)\n"

        if params.look_at:
            code += f"""
aim = cmds.spaceLocator(name=cam + '_aim')[0]
cmds.xform(aim, translation={params.look_at}, worldSpace=True)
cmds.aimConstraint(aim, cam, aimVector=[0, 0, -1], upVector=[0, 1, 0])
cmds.delete(aim)
"""

        code += "result = {'camera': cam}\n"
        return maybe_annotate_with_suggestions("maya_create_camera", await asyncio.to_thread(bridge.execute, code))
    except Exception as e:
        return _handle_error(e)


async def _execute_with_heartbeat(
    code: str,
    ctx: Context | None,
    label: str,
    interval: float = 10,
    bridge_timeout: Optional[float] = None,
) -> str:
    """Run ``bridge.execute`` in a worker thread, streaming heartbeats.

    Long Maya operations block the Command Port socket until they finish;
    without this, MCP clients see total silence for the whole duration.
    Fast operations (under ``interval`` seconds) emit nothing — no noise
    on the common path. Visible-progress streaming port, Chat 62 design.

    ``bridge_timeout`` raises the per-call Command Port wait beyond the
    10s instance default (passed through only when set, so test doubles
    with a plain ``(code)`` signature keep working).
    """
    if bridge_timeout is not None:
        task = asyncio.ensure_future(
            asyncio.to_thread(bridge.execute, code, timeout=bridge_timeout)
        )
    else:
        task = asyncio.ensure_future(asyncio.to_thread(bridge.execute, code))
    elapsed = 0.0
    while True:
        done, _ = await asyncio.wait({task}, timeout=interval)
        if done:
            return task.result()
        elapsed += interval
        if ctx:
            await ctx.info(f"{label} still running in Maya... ({elapsed:g}s)")


async def _do_execute_python(params: dict, ctx: Context | None = None) -> str:
    """Execute arbitrary Python code in Maya. Code must assign its result to a 'result' variable. Useful for advanced operations not covered by other tools."""
    from pydantic import ValidationError
    try:
        validated = ExecutePythonInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for execute_python: {e}"})

    _stats["exec_calls"] += 1
    _stats["tokens_in"] += _tok(validated.code)

    # Safety check on code
    warning = check_dangerous(validated.code)
    if warning:
        _stats["safety_blocks"] += 1
        _audit_record("maya_session", "execute_python", params,
                      _audit.AUDIT_SAFETY_BLOCKED)
        return json.dumps({"safety_warning": warning})

    # F4b (3C Wave 4): AST dry-run — reject a hallucinated cmds.<command>
    # statically, before the Command Port round-trip. No-op when the api_graph
    # is missing (graph_loaded=False) or disabled via config ast_dry_run: false.
    if _get_config().get("ast_dry_run", True):
        _validation = validate_python(validated.code)
        if not _validation.ok:
            _audit_record("maya_session", "execute_python", params,
                          _audit.AUDIT_AST_REJECTED)
            return json.dumps({"ast_warning": format_issues(_validation)})

    # F0: a "turn" is an execute_python that ran past the safety gate (the
    # error-prone free-form path). p_fallo = failed_turns / turns_total;
    # dedicated tools are deliberately excluded (they track exec_calls only).
    _stats["turns_total"] += 1
    _t0 = time.monotonic()
    try:
        response = await _execute_with_heartbeat(
            validated.code, ctx, "execute_python",
            bridge_timeout=validated.timeout,
        )
        _stats["tokens_out"] += _tok(response)
        _track_timing({
            "op": "exec",
            "total_ms": round((time.monotonic() - _t0) * 1000),
            "error": False,
        })
        _audit_record("maya_session", "execute_python", params, _audit.AUDIT_OK)
        return response
    except Exception as e:
        _stats["failed_turns"] += 1
        _track_timing({
            "op": "exec",
            "total_ms": round((time.monotonic() - _t0) * 1000),
            "error": True,
        })
        _audit_record("maya_session", "execute_python", params, _audit.AUDIT_ERROR)
        return _handle_error(e)


async def _do_new_scene(params: dict) -> str:
    """Create a new empty scene in Maya.

    Guards unsaved work: ``cmds.file(new=True, force=True)`` is the exact
    pattern safety.py flags as dangerous because it silently discards the
    current scene. Unless the caller passes ``confirm=True``, this handler
    first checks ``cmds.file(q=True, modified=True)`` and refuses (returning
    an ``unsaved_changes`` error) when the scene has unsaved modifications,
    so a daily user cannot lose work by accident.

    Optional params: ``{"confirm": true}`` — discard unsaved changes anyway.
    """
    try:
        if bool(params.get("confirm", False)):
            code = """
import maya.cmds as cmds
cmds.file(new=True, force=True)
result = {'status': 'new_scene_created'}
"""
        else:
            code = """
import maya.cmds as cmds
if cmds.file(q=True, modified=True):
    result = {
        'error': 'unsaved_changes',
        'hint': 'The current scene has unsaved changes. Save it first, or re-run new_scene with confirm=True to discard them.',
    }
else:
    cmds.file(new=True, force=True)
    result = {'status': 'new_scene_created'}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


async def _do_save_scene(params: dict) -> str:
    """Save the current Maya scene."""
    try:
        code = """
import maya.cmds as cmds
scene = cmds.file(q=True, sceneName=True)
if scene:
    cmds.file(save=True)
    result = {'saved': scene}
else:
    result = {'error': 'Unnamed scene. Use maya_execute_python to do file(rename=...)'}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


# ─────────────────────────────────────────────
# New Input Models (P2-P5, A-E)
# ─────────────────────────────────────────────

class MeshOperationType(str, Enum):
    EXTRUDE = "extrude"
    BEVEL = "bevel"
    BOOLEAN_UNION = "boolean_union"
    BOOLEAN_DIFFERENCE = "boolean_difference"
    BOOLEAN_INTERSECTION = "boolean_intersection"
    COMBINE = "combine"
    SEPARATE = "separate"
    SMOOTH = "smooth"


class MeshOperationInput(BaseModel):
    """Parameters for mesh operations."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    object_name: str = Field(..., description="Name of the mesh object")
    operation: MeshOperationType = Field(..., description="Type of operation")
    second_object: Optional[str] = Field(default=None, description="Second object (required for boolean and combine)")
    faces: Optional[str] = Field(default=None, description="Face components (e.g., 'pCube1.f[0:3]') for extrude/bevel")
    offset: float = Field(default=0.2, description="Offset/distance for extrude or bevel", ge=0.0)
    divisions: int = Field(default=1, description="Divisions for smooth or segments for bevel", ge=1, le=10)


class KeyframeInput(BaseModel):
    """Parameters for creating animation keyframes."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    object_name: str = Field(..., description="Name of the object to animate")
    attribute: str = Field(default="translateX", description="Attribute to animate (translateX/Y/Z, rotateX/Y/Z, scaleX/Y/Z, visibility)")
    value: float = Field(..., description="Keyframe value")
    frame: float = Field(..., description="Frame to insert the keyframe on")
    in_tangent: str = Field(default="auto", description="In-tangent: auto, linear, flat, spline, step")
    out_tangent: str = Field(default="auto", description="Out-tangent: auto, linear, flat, spline, step")


class ImportFileInput(BaseModel):
    """Parameters for importing 3D files."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    file_path: str = Field(..., description="Absolute path to file to import (.obj, .fbx, .glb, .abc, .ma, .mb, .bvh)")
    namespace: Optional[str] = Field(default=None, description="Namespace to avoid name collisions")
    group_under: Optional[str] = Field(default=None, description="Parent group name (created if it doesn't exist)")
    scale_factor: Optional[float] = Field(default=None, description="Scale factor on import (e.g., 0.01 for cm to m)")


class ViewportCaptureInput(BaseModel):
    """Parameters for capturing the Maya viewport."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    output_path: str = Field(default="/tmp/maya_viewport.png", description="Output path for image (.png/.jpg)")
    width: int = Field(default=1920, description="Capture width in pixels", ge=100, le=8192)
    height: int = Field(default=1080, description="Capture height in pixels", ge=100, le=8192)
    camera: Optional[str] = Field(default=None, description="Camera to use (default: active panel)")
    frame: Optional[float] = Field(default=None, description="Frame to capture (default: current frame)")


class ShelfButtonInput(BaseModel):
    """Parameters for creating a button on the Maya shelf."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    label: str = Field(..., description="Button label (short text)")
    command: str = Field(..., description="Python code that executes when button is clicked")
    tooltip: str = Field(default="", description="Help text on mouseover")
    shelf_name: str = Field(default="Custom", description="Name of the shelf to create the button in")
    icon_label: str = Field(default="MCP", description="Text overlaid on icon (max 4 chars)")


# ─────────────────────────────────────────────
# New Tools (P2-P6, A-E)
# ─────────────────────────────────────────────


@mcp.tool(name="maya_mesh_operation")
@_audited("maya_mesh_operation")
async def maya_mesh_operation(params: MeshOperationInput) -> str:
    """Execute mesh operations: extrude, bevel, boolean (union/difference/intersection), combine, separate, smooth."""
    try:
        op = params.operation.value

        obj = _py_str(params.object_name)
        second = _py_str(params.second_object) if params.second_object else None

        if op == "extrude":
            faces = _py_str(params.faces or f"{params.object_name}.f[:]")
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_extrude')
try:
    result_faces = cmds.polyExtrudeFacet({faces}, localTranslateZ={params.offset}, divisions={params.divisions})
    result = {{'operation': 'extrude', 'faces': {faces}, 'offset': {params.offset}, 'result': str(result_faces)}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "bevel":
            faces = _py_str(params.faces or f"{params.object_name}.e[:]")
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_bevel')
try:
    result_edges = cmds.polyBevel3({faces}, offset={params.offset}, segments={params.divisions})
    result = {{'operation': 'bevel', 'target': {faces}, 'offset': {params.offset}, 'result': str(result_edges)}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op.startswith("boolean_"):
            if not params.second_object:
                return json.dumps({"error": "Boolean requires 'second_object'"})
            bool_op = {"boolean_union": 1, "boolean_difference": 2, "boolean_intersection": 3}[op]
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_boolean')
try:
    result_node = cmds.polyCBoolOp({obj}, {second}, op={bool_op}, ch=False)
    result = {{'operation': {_py_str(op)}, 'objects': [{obj}, {second}], 'result': str(result_node[0])}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "combine":
            if not params.second_object:
                return json.dumps({"error": "Combine requires 'second_object'"})
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_combine')
try:
    combined = cmds.polyUnite({obj}, {second}, ch=False)
    result = {{'operation': 'combine', 'result': str(combined[0])}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "separate":
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_separate')
try:
    separated = cmds.polySeparate({obj}, ch=False)
    result = {{'operation': 'separate', 'parts': [str(s) for s in separated]}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "smooth":
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_smooth')
try:
    cmds.polySmooth({obj}, divisions={params.divisions})
    verts = cmds.polyEvaluate({obj}, vertex=True)
    faces = cmds.polyEvaluate({obj}, face=True)
    result = {{'operation': 'smooth', 'object': {obj}, 'divisions': {params.divisions}, 'vertices': verts, 'faces': faces}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        else:
            return json.dumps({"error": f"Unknown operation: {op}"})

        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_set_keyframe")
@_audited("maya_set_keyframe")
async def maya_set_keyframe(params: KeyframeInput) -> str:
    """Create an animation keyframe on an object. Allows animating translate, rotate, scale, and visibility per frame."""
    try:
        obj = _py_str(params.object_name)
        attr = _py_str(params.attribute)
        in_tan = _py_str(params.in_tangent)
        out_tan = _py_str(params.out_tangent)
        code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_keyframe')
try:
    cmds.setKeyframe({obj}, attribute={attr},
                     value={params.value}, time={params.frame},
                     inTangentType={in_tan}, outTangentType={out_tan})
    result = {{'object': {obj}, 'attribute': {attr},
              'value': {params.value}, 'frame': {params.frame}}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_import_file")
@_audited("maya_import_file")
async def maya_import_file(params: ImportFileInput, ctx: Context | None = None) -> str:
    """Import 3D files into Maya: OBJ, FBX, GLB/GLTF, Alembic ABC, Maya MA/MB, BVH mocap. With namespace, parent group, and scale options.

    GLB/GLTF: uses Maya's native glTF scene parser (``type='glTF Import'``);
    if the parser is not registered or import fails, falls back to the
    Vision3D sibling pattern — ``mesh_uv.obj`` + ``texture_baked.png`` next
    to the GLB — building an ``aiStandardSurface`` with the texture in
    ``baseColor`` and assigning it to the imported meshes.

    BVH: Maya has no native BVH import, so ``.bvh`` motion-capture files are
    parsed and rebuilt by the pure-Python ``maya_mcp.bvh_import`` module
    (hierarchy → joints, motion → keyframes, with the per-joint
    BVH→Maya rotate-order mapping handled internally). ``namespace`` and
    ``scale_factor`` are forwarded to the builder; the skeleton lands under a
    ``<namespace>:bvh_grp`` group. The result feeds a HumanIK retarget onto a
    rigged character (mocap → generic animation library).
    """
    from maya_mcp.suggestions import maybe_annotate_with_suggestions
    try:
        ext = params.file_path.rsplit(".", 1)[-1].lower() if "." in params.file_path else ""
        fp = _py_str(params.file_path)  # safe literal for the import path

        if ext == "bvh":
            # BVH has no native Maya importer — parse + rebuild via the
            # pure-Python maya_mcp.bvh_import module inside Maya. The package
            # is not guaranteed to be on Maya's sys.path, so the server
            # injects its own src dir (absolute, no traversal) as a literal.
            import os as _os
            _src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            _bvh_ns = params.namespace or "bvh"
            _bvh_scale = params.scale_factor if params.scale_factor else 1.0
            bvh_code = f"""
import sys as _sys
_bvh_src = {_py_str(_src_dir)}
if _bvh_src not in _sys.path:
    _sys.path.insert(0, _bvh_src)
from maya_mcp import bvh_import as _bvh
_skel, _motion = _bvh.parse_bvh({fp})
result = _bvh.build_in_maya(
    _skel, _motion, namespace={_py_str(_bvh_ns)}, scale={_bvh_scale}
)
"""
            if ctx:
                await ctx.info(
                    f"Importing {params.file_path.rsplit('/', 1)[-1]} "
                    f"(BVH mocap) into Maya..."
                )
            # A dense mocap clip keys every joint per frame — well beyond the
            # 10s Command Port default; 240s + heartbeats.
            out = await _execute_with_heartbeat(
                bvh_code, ctx, "import bvh", bridge_timeout=240.0
            )
            return maybe_annotate_with_suggestions("maya_import_file", out)

        ns_opt = f", namespace={_py_str(params.namespace)}" if params.namespace else ""
        group_code = ""
        if params.group_under:
            grp = _py_str(params.group_under)
            group_code = f"""
if not cmds.objExists({grp}):
    cmds.group(empty=True, name={grp})
"""
        scale_code = ""
        if params.scale_factor:
            scale_code = f"""
for _mcp_obj in _mcp_imported:
    if cmds.objectType(_mcp_obj) == 'transform':
        cmds.scale({params.scale_factor}, {params.scale_factor}, {params.scale_factor}, _mcp_obj)
"""
        # Build file type string. GLB/GLTF use Maya's native glTF scene
        # parser; the type string is case-sensitive ('glTF Import', not
        # 'GLTF Import'). For all other types the historical mapping
        # remains unchanged.
        file_types = {
            "obj": "OBJ", "fbx": "FBX", "abc": "Alembic",
            "glb": "glTF Import", "gltf": "glTF Import",
            "ma": "mayaAscii", "mb": "mayaBinary",
        }
        ftype = file_types.get(ext, "")
        type_opt = f", type='{ftype}'" if ftype else ""

        if ext in ("glb", "gltf"):
            # Native glTF path with OBJ+texture fallback. The fallback
            # mirrors Vision3D's output convention: every textured.glb is
            # written alongside mesh_uv.obj + texture_baked.png in the
            # same directory.
            import_block = f"""
    _mcp_method = 'gltf'
    _mcp_warning = ''
    try:
        try:
            cmds.loadPlugin('libgltfsceneimport', quiet=True)
        except Exception:
            pass
        cmds.file({fp}, i=True, ignoreVersion=True,
                  mergeNamespacesOnClash=False, returnNewNodes=True,
                  type='glTF Import'{ns_opt})
    except RuntimeError as _mcp_e:
        import os as _mcp_os
        _mcp_dir = _mcp_os.path.dirname({fp})
        _mcp_obj = _mcp_os.path.join(_mcp_dir, 'mesh_uv.obj')
        _mcp_tex = _mcp_os.path.join(_mcp_dir, 'texture_baked.png')
        if _mcp_os.path.isfile(_mcp_obj):
            _mcp_method = 'obj_fallback'
            _mcp_warning = 'glTF translator unavailable: ' + str(_mcp_e)
            _mcp_obj_before = set(cmds.ls(transforms=True, long=True))
            cmds.file(_mcp_obj, i=True, ignoreVersion=True,
                      mergeNamespacesOnClash=False, returnNewNodes=True,
                      type='OBJ'{ns_opt})
            _mcp_obj_after = set(cmds.ls(transforms=True, long=True))
            _mcp_obj_new = list(_mcp_obj_after - _mcp_obj_before)
            if _mcp_os.path.isfile(_mcp_tex):
                _mcp_sh = cmds.shadingNode('aiStandardSurface', asShader=True, name='mcp_glb_fallback_mat')
                _mcp_sg = cmds.sets(name=_mcp_sh + 'SG', empty=True, renderable=True, noSurfaceShader=True)
                cmds.connectAttr(_mcp_sh + '.outColor', _mcp_sg + '.surfaceShader', force=True)
                _mcp_file = cmds.shadingNode('file', asTexture=True, isColorManaged=True, name='mcp_glb_fallback_tex')
                _mcp_p2d = cmds.shadingNode('place2dTexture', asUtility=True, name='mcp_glb_fallback_p2d')
                for _mcp_a in ('coverage', 'translateFrame', 'rotateFrame', 'mirrorU', 'mirrorV', 'stagger', 'wrapU', 'wrapV', 'repeatUV', 'offset', 'rotateUV', 'noiseUV', 'vertexUvOne', 'vertexUvTwo', 'vertexUvThree', 'vertexCameraOne'):
                    cmds.connectAttr(_mcp_p2d + '.' + _mcp_a, _mcp_file + '.' + _mcp_a, force=True)
                cmds.connectAttr(_mcp_p2d + '.outUV', _mcp_file + '.uv', force=True)
                cmds.connectAttr(_mcp_p2d + '.outUvFilterSize', _mcp_file + '.uvFilterSize', force=True)
                cmds.setAttr(_mcp_file + '.fileTextureName', _mcp_tex, type='string')
                cmds.connectAttr(_mcp_file + '.outColor', _mcp_sh + '.baseColor', force=True)
                for _mcp_xform in _mcp_obj_new:
                    if cmds.objectType(_mcp_xform) == 'transform':
                        cmds.sets(_mcp_xform, edit=True, forceElement=_mcp_sg)
        else:
            raise
"""
        else:
            import_block = f"""
    _mcp_method = '{ftype or 'auto'}'
    _mcp_warning = ''
    cmds.file({fp}, i=True, ignoreVersion=True,
              mergeNamespacesOnClash=False, returnNewNodes=True{ns_opt}{type_opt})
"""

        code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_import')
try:
    _mcp_before = set(cmds.ls(transforms=True))
    {group_code}
{import_block}
    _mcp_after = set(cmds.ls(transforms=True))
    _mcp_imported = list(_mcp_after - _mcp_before)
    {scale_code}
    result = {{'imported': len(_mcp_imported), 'objects': _mcp_imported[:20],
              'file': {fp}, 'method': _mcp_method,
              'warning': _mcp_warning}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        if ctx:
            await ctx.info(
                f"Importing {params.file_path.rsplit('/', 1)[-1]} "
                f"({ftype or 'auto'}) into Maya..."
            )
        # Imports of real assets (Vision3D GLBs, FBX rigs) routinely exceed
        # the 10s Command Port default; 120s + heartbeats instead of a
        # guaranteed timeout error.
        out = await _execute_with_heartbeat(
            code, ctx, f"import {ext or 'file'}", bridge_timeout=120.0
        )
        return maybe_annotate_with_suggestions("maya_import_file", out)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_viewport_capture")
async def maya_viewport_capture(params: ViewportCaptureInput) -> list:
    """Capture the Maya viewport as PNG/JPG image and return it for visual analysis. Does not do Arnold render — it is an instant Viewport 2.0 grab (<1s); for a ray-traced still use maya_session(action='render_still'). Useful for visually verifying scene state, checking lighting, framing, and detecting issues.

    NEVER captures the user's focused viewport: it playblasts a throw-away
    Viewport-2.0 window (its own modelPanel forced to rendererName='vp2Renderer'),
    so an Arnold IPR / render-override active on the user's panel can never be
    captured — which would saturate Maya's main thread and hang it (memory
    feedback_maya_gs_arnold_ipr_hang; same technique as review_turntable)."""
    import base64
    try:
        cam = _py_str(params.camera) if params.camera else "None"
        frame_expr = str(params.frame) if params.frame is not None else "cmds.currentTime(query=True)"
        fmt = "png" if params.output_path.endswith(".png") else "jpg"
        out_path = _py_str(params.output_path)

        # Pin the colour-management view transform so the grab matches the
        # viewport (not dark / riding session state); restored in `finally`.
        # Chat 79 — see maya_mcp.color_policy.
        view = _get_config().get("review_view_transform", color_policy.DEFAULT_REVIEW_VIEW)
        cm_apply = color_policy.view_transform_apply_code(view)
        cm_restore = color_policy.view_transform_restore_code()
        cm_restore_indented = "\n".join(
            ("    " + ln) if ln.strip() else ln
            for ln in cm_restore.strip("\n").splitlines()
        )

        # Capture from a DEDICATED throw-away Viewport-2.0 window, NEVER the
        # user's docked/focused panel. If an Arnold IPR / render-override is
        # active on that panel, playblasting it burns the Arnold render into the
        # buffer AND saturates the main thread -> hang (Chat 71/77,
        # feedback_maya_gs_arnold_ipr_hang). A fresh modelPanel forced to
        # rendererName='vp2Renderer' has no IPR attached (guaranteed VP2.0) and a
        # clean source. Onscreen (offScreen=False) so the context is valid even if
        # Maya is occluded (Chat 74). The window + the user's state are restored in
        # `finally`. Mirrors review_build.py's turntable capture.
        code = f"""
import maya.cmds as cmds
import os, base64
_mcp_cam = {cam}
if _mcp_cam is None:
    _mcp_fp = cmds.getPanel(withFocus=True)
    if _mcp_fp and cmds.getPanel(typeOf=_mcp_fp) == 'modelPanel':
        _mcp_cam = cmds.modelPanel(_mcp_fp, query=True, camera=True)
    if not _mcp_cam:
        _mcp_cam = 'persp'
_mcp_win = None
cmds.undoInfo(stateWithoutFlush=False)
{cm_apply}
try:
    if cmds.window("mcpViewportCaptureWin", exists=True):
        cmds.deleteUI("mcpViewportCaptureWin")
    _mcp_win = cmds.window("mcpViewportCaptureWin", title="MCP Capture",
                           widthHeight=({params.width}, {params.height}))
    cmds.paneLayout()
    _mcp_panel = cmds.modelPanel(menuBarVisible=False)
    cmds.modelPanel(_mcp_panel, edit=True, camera=_mcp_cam)
    cmds.modelEditor(_mcp_panel, edit=True, rendererName="vp2Renderer",
                     displayAppearance="smoothShaded", displayTextures=True,
                     headsUpDisplay=False, grid=False)
    cmds.showWindow(_mcp_win)
    cmds.refresh(force=True)
    _mcp_frame = {frame_expr}
    _mcp_img = cmds.playblast(
        completeFilename={out_path},
        format='image', compression='{fmt}',
        width={params.width}, height={params.height},
        showOrnaments=False, viewer=False,
        editorPanelName=_mcp_panel,
        offScreen=False, percent=100,
        startTime=_mcp_frame, endTime=_mcp_frame
    )
    _mcp_size = os.path.getsize({out_path}) // 1024
    with open({out_path}, 'rb') as _f:
        _mcp_b64 = base64.b64encode(_f.read()).decode('ascii')
    result = {{'captured': {out_path}, 'size_kb': _mcp_size,
              'resolution': '{params.width}x{params.height}',
              'image_b64': _mcp_b64}}
finally:
    if _mcp_win and cmds.window(_mcp_win, exists=True):
        try:
            cmds.deleteUI(_mcp_win)
        except Exception:
            pass
    cmds.undoInfo(stateWithoutFlush=True)
{cm_restore_indented}
"""
        raw = await asyncio.to_thread(bridge.execute, code)
        # Parse the result to extract the base64 image
        data = json.loads(raw) if isinstance(raw, str) else raw
        img_b64 = data.get("image_b64", "")
        meta = f"Captured {data.get('resolution', '?')} — {data.get('size_kb', '?')} KB — {data.get('captured', params.output_path)}"
        if img_b64:
            return [
                Image(data=base64.b64decode(img_b64), format=fmt),
                meta,
            ]
        return [meta, "(image data not available — check output_path)"]
    except Exception as e:
        return [_handle_error(e)]


async def _do_scene_snapshot(params: dict) -> str:
    """Return a complete snapshot of scene state: file, modified, frame, objects by type, renderer, plugins, render resolution. Useful for informed decisions before operations."""
    try:
        code = """
import maya.cmds as cmds
_mcp_meshes = cmds.ls(type='mesh') or []
_mcp_lights = cmds.ls(lights=True) or []
_mcp_cameras = cmds.ls(cameras=True) or []
_mcp_curves = cmds.ls(type='nurbsCurve') or []
_mcp_transforms = cmds.ls(transforms=True) or []
_mcp_plugins = cmds.pluginInfo(query=True, listPlugins=True) or []

result = {
    'file': cmds.file(q=True, sceneName=True) or 'untitled',
    'modified': cmds.file(q=True, modified=True),
    'current_frame': cmds.currentTime(q=True),
    'frame_range': [cmds.playbackOptions(q=True, min=True), cmds.playbackOptions(q=True, max=True)],
    'renderer': cmds.getAttr('defaultRenderGlobals.currentRenderer'),
    'render_resolution': [
        cmds.getAttr('defaultResolution.width'),
        cmds.getAttr('defaultResolution.height')
    ],
    'counts': {
        'transforms': len(_mcp_transforms),
        'meshes': len(_mcp_meshes),
        'lights': len(_mcp_lights),
        'cameras': len(_mcp_cameras),
        'curves': len(_mcp_curves),
    },
    'loaded_plugins': _mcp_plugins[:20],
    'up_axis': cmds.upAxis(q=True, axis=True),
    'units': cmds.currentUnit(q=True, linear=True),
}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


async def _do_shelf_button(params: dict) -> str:
    """Create a custom button on the Maya shelf with associated Python code. Allows Claude to leave reusable tools in the interface."""
    from pydantic import ValidationError
    try:
        validated = ShelfButtonInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for shelf_button: {e}"})
    try:
        # Escape the command for embedding in Python string
        safe_command = validated.command.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        # Free-form display fields go in as safe literals too (a label like
        # "Bob's Tool" would otherwise break the single-quoted literal). The
        # shelf name flows through the _mcp_shelf variable into mel.eval so the
        # MEL string is built by concatenation, not raw interpolation.
        shelf_lit = _py_str(validated.shelf_name)
        label_lit = _py_str(validated.label)
        tooltip_lit = _py_str(validated.tooltip)
        icon_lit = _py_str(validated.icon_label[:4])
        code = f"""
import maya.cmds as cmds
import maya.mel as mel

# Create or find the shelf
_mcp_shelf = {shelf_lit}
if not cmds.shelfLayout(_mcp_shelf, exists=True):
    mel.eval('addNewShelfTab "' + _mcp_shelf + '"')

_mcp_btn = cmds.shelfButton(
    parent=_mcp_shelf,
    label={label_lit},
    annotation={tooltip_lit},
    imageOverlayLabel={icon_lit},
    image='pythonFamily.png',
    command='{safe_command}',
    sourceType='python'
)
result = {{'button': _mcp_btn, 'shelf': _mcp_shelf, 'label': {label_lit}}}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


async def _do_operation_history(params: dict) -> str:
    """Read recent durable-audit records (read-only; no Maya round-trip).

    Read companion to the write-only audit log (``_audit.py`` / the
    ``MAYA_AUDIT_LOG`` toggle). Returns the most recent records newest-first as
    JSON, with optional ``limit`` / ``tool`` / ``action`` / ``status`` filters.
    When the audit log is OFF it returns an explanatory payload (not an error)
    so the caller knows to set ``MAYA_AUDIT_LOG=1``. Pure file read: no Command
    Port traffic, nothing scheduled on Maya's main thread.

    Optional params: {"limit": 50, "tool": "maya_transform",
                      "action": "execute_python", "status": "error"}
    """
    if not _audit.audit_enabled():
        return json.dumps({
            "audit_enabled": False,
            "records": [],
            "hint": "Durable audit logging is OFF. Set MAYA_AUDIT_LOG=1 (or "
                    "true/yes/on) and relaunch the server to record operations.",
        })
    p = params or {}
    try:
        limit = int(p.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    records = _audit.read_records(
        _AUDIT_LOG,
        limit=limit,
        tool=p.get("tool"),
        action=p.get("action"),
        status=p.get("status"),
    )
    return json.dumps({
        "audit_enabled": True,
        "count": len(records),
        "log_path": str(_AUDIT_LOG),
        "records": records,
    })


async def _do_publish(params: dict, ctx: Context | None = None) -> str:
    """Drive the native tk-multi-publish2 PublishManager inside the engine'd Maya.

    'preview' returns the collected publish tree as JSON (no side effects).
    'publish' activates tasks per include/exclude over the LIVE tree, then runs
    validate -> publish -> finalize, returning per-item status + errors as JSON.
    Long-running, so it streams heartbeats via _execute_with_heartbeat. The
    generated payload is curated and self-contained (it drives the Toolkit API,
    not free-form user code), so it is NOT run through the check_dangerous / AST
    gate — same as _do_save_scene / maya_import_file.
    """
    from pydantic import ValidationError
    try:
        validated = PublishInput(**(params or {}))
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for publish: {e}"})

    try:
        if validated.mode == PublishMode.PREVIEW:
            code = _PUBLISH_PREVIEW_CODE
            timeout = validated.timeout or 120.0
        else:
            include = _expand_tokens(validated.include)
            exclude = _expand_tokens(validated.exclude)
            comment = validated.comment or ""
            header = (
                "_INCLUDE = " + json.dumps(include) + "\n"
                + "_EXCLUDE = " + json.dumps(exclude) + "\n"
                + "_COMMENT = " + json.dumps(comment) + "\n"
            )
            code = header + _PUBLISH_EXECUTE_CODE
            timeout = validated.timeout or 600.0

        return await _execute_with_heartbeat(
            code, ctx, f"publish:{validated.mode.value}", bridge_timeout=timeout,
        )
    except Exception as e:
        return _handle_error(e)


# ─────────────────────────────────────────────
# Session Dispatch Tool
# ─────────────────────────────────────────────

# Session actions the dispatcher does NOT audit itself:
#   • DELETE / EXECUTE_PYTHON self-audit inside their handlers (so they can emit
#     the precise safety_blocked / ast_rejected status at their early returns);
#   • PING / LIST_SCENE / SCENE_SNAPSHOT are read-only and excluded by default
#     (the audit focuses on mutations + execute_python + blocked attempts — see
#     proposals/maya-durable-audit-log.md §4). Everything else (new_scene,
#     save_scene, shelf_button, launch) is audited centrally in the dispatcher.
_AUDIT_DISPATCH_SKIP = frozenset({
    SessionAction.DELETE,
    SessionAction.PING,
    SessionAction.LIST_SCENE,
    SessionAction.SCENE_SNAPSHOT,
    SessionAction.OPERATION_HISTORY,
})


async def _do_review_turntable(params: dict, ctx: Context | None = None) -> str:
    """Deterministic VP2.0 turntable playblast → .mov (ships review_build.py to Maya).

    The playblast recipe is fixed code (not LLM-supplied), so it can't improvise
    into an Arnold/on-screen playblast that hangs Maya's main thread (Chat 71).
    """
    out_path = params.get("out_path")
    if not out_path:
        return json.dumps({
            "error": "review_turntable requires params.out_path",
            "hint": "Resolve it first via fpt tk_resolve_path(template='movie_asset_publish', "
                    "name=<the task name>) so the .mov is {Asset}_{Task}_v###.mov "
                    "(e.g. DJ_Model_v001.mov), NOT 'turntable'.",
        })
    from pathlib import Path as _Path
    src = (_Path(__file__).parent / "review_build.py").read_text()
    # Colour-management view pinned for the VP2.0 capture so the .mov matches the
    # viewport (Chat 79). params override > config.json > Maya-default review view.
    view = (params.get("view_transform")
            or _get_config().get("review_view_transform", color_policy.DEFAULT_REVIEW_VIEW))
    code = src + (
        "\nimport json as _json\n"
        "result = _json.dumps(review_turntable("
        f"out_path={out_path!r}, start={int(params.get('start', 1))!r}, "
        f"end={int(params.get('end', 100))!r}, fps={int(params.get('fps', 25))!r}, "
        f"width={int(params.get('width', 1920))!r}, height={int(params.get('height', 1080))!r}, "
        f"objects={params.get('objects')!r}, focal={float(params.get('focal', 50.0))!r}, "
        f"view_transform={view!r}))\n"
    )
    try:
        result_str = await _execute_with_heartbeat(
            code, ctx, "review_turntable", bridge_timeout=int(params.get("timeout", 600))
        )
    except MayaBridgeError as exc:
        return json.dumps({
            "error": f"Maya bridge error: {exc}",
            "hint": "review_turntable runs in Maya — ensure the Command Port is up.",
        })
    # PNG-sequence fallback (Maya's movie encoder was unavailable) → assemble the
    # .mov server-side with ffmpeg so review_turntable always returns a .mov
    # (Chat 79). Best-effort: on any failure, keep the PNG-sequence result.
    try:
        _rt = json.loads(result_str)
    except (ValueError, TypeError):
        return result_str
    if _review_encode.is_png_fallback(_rt):
        _frames = _rt.get("frames") or [1, 100]
        if ctx:
            await ctx.info("Maya movie encoder unavailable — assembling the .mov "
                           "from the PNG sequence with ffmpeg…")
        if await asyncio.to_thread(
            _review_encode.assemble_mov_from_pngs,
            out_path, int(_frames[0]), int(_frames[1]), int(_rt.get("fps", 25)),
        ):
            _rt["mov"] = out_path
            _rt["format"] = {
                "format": "ffmpeg",
                "note": "Maya movie encoder unavailable; .mov assembled from the "
                        "PNG sequence via ffmpeg",
            }
            return json.dumps(_rt)
    return result_str


async def _do_render_still(params: dict, ctx: Context | None = None) -> str:
    """Single-frame **Arnold** still → out_path: Maya exports a .ass, ``kick`` renders it.

    Maya only exports the scene; the render happens OUT OF PROCESS. That split is
    not an optimisation — it is the only way the colour is right. Dumping Maya's
    Render View (``renderWindowEditor(writeImage=…)``) writes a **scene-linear**
    file and ignores every colour setting; Arnold writing the file itself applies
    the output transform. Both were measured in-vivo on a flat 0.5 patch (127 vs
    188 — Chat 94; see ``render_still.py``). Side benefits: no Render View window
    is opened, and a long render can never hang Maya's main thread.

    The recipe is fixed code (not LLM-supplied). Use this for a ray-traced still;
    use maya_viewport_capture for a fast VP2.0 grab.

    NB: kick runs on the MCP server host, so this assumes Maya is local
    (``MAYA_HOST=localhost``, the supported setup) — out_path is written by the
    render process, not by Maya.
    """
    out_path = params.get("out_path")
    if not out_path:
        return json.dumps({
            "error": "render_still requires params.out_path",
            "hint": "A PNG in the review area — resolve it via fpt tk_resolve_path "
                    "(e.g. an asset/shot review path) so the still lands with the "
                    "pipeline-correct name.",
        })
    import tempfile
    from pathlib import Path as _Path
    src = (_Path(__file__).parent / "render_still.py").read_text()
    view = (params.get("view_transform")
            or _get_config().get("review_view_transform", color_policy.DEFAULT_REVIEW_VIEW))
    cam = params.get("camera")
    frame = params.get("frame")
    width = int(params.get("width", 1920))
    height = int(params.get("height", 1080))
    aa_samples = int(params.get("aa_samples", 3))
    timeout = int(params.get("timeout", 600))
    ass_path = str(_Path(tempfile.gettempdir()) / f"mcp_render_still_{os.getpid()}.ass")

    code = src + (
        "\nimport json as _json\n"
        "result = _json.dumps(export_still_ass("
        f"out_path={out_path!r}, ass_path={ass_path!r}, camera={cam!r}, "
        f"frame={frame!r}, view_transform={view!r}))\n"
    )
    try:
        raw = await _execute_with_heartbeat(
            code, ctx, "render_still (scene export)", bridge_timeout=timeout
        )
    except MayaBridgeError as exc:
        return json.dumps({
            "error": f"Maya bridge error: {exc}",
            "hint": "render_still exports the scene from Maya — ensure the Command "
                    "Port is up and mtoa is available.",
        })

    try:
        report = json.loads(raw)
    except (TypeError, ValueError):
        return json.dumps({
            "error": "render_still: unreadable report from Maya",
            "raw": str(raw)[:500],
        })
    if report.get("error") or not report.get("ass"):
        return json.dumps(report, ensure_ascii=False)

    kick = report.get("kick")
    if not kick:
        report["error"] = "kick was not found next to the loaded mtoa plugin"
        report["hint"] = ("kick ships with mtoa (…/mtoa/<version>/bin/kick). "
                          "Check the Arnold install for this Maya version.")
        return json.dumps(report, ensure_ascii=False)

    # -dw -dp: no render window, no progressive passes — the invocation the
    # catcher-passes skill settled on. imageFilePrefix is baked into the .ass,
    # so NEVER pass -o (that is what made kick loop, Chat 94).
    argv = [kick, "-i", report["ass"], "-as", str(aa_samples),
            "-r", str(width), str(height), "-dw", "-dp", "-nostdin"]
    report["resolution"] = f"{width}x{height}"
    report["aa_samples"] = aa_samples
    if ctx:
        await ctx.info(f"render_still: kick rendering {width}x{height} (AA {aa_samples})...")
    rc, _stdout, stderr = await _run_cmd(argv, timeout=timeout)

    if os.path.exists(out_path):
        report["rendered"] = out_path
        report["size_kb"] = os.path.getsize(out_path) // 1024
    else:
        report["error"] = (f"kick finished (rc={rc}) but wrote no file at out_path — "
                           f"check the Arnold licence and the output path.")
        report["kick_stderr"] = (stderr or "")[-800:]
    try:
        os.remove(report["ass"])
    except OSError:
        pass
    return json.dumps(report, ensure_ascii=False)


@mcp.tool(name="maya_session")
async def maya_session(params: SessionDispatchInput, ctx: Context | None = None) -> str:
    """Manage Maya session, query scene state, and run utility commands.

    Available actions:

    • ping — Check connection to Maya, return version/scene/renderer. No params needed.
    • launch — Open Maya and wait for Command Port to respond. No params needed.
    • new_scene — Create a new empty scene. Refuses if the current scene has unsaved changes; pass {"confirm": true} to discard them.
    • save_scene — Save the current scene. No params needed.
    • list_scene — List objects in the scene. Optional params: {"object_type": "mesh", "name_filter": "*sphere*"}
    • scene_snapshot — Full scene state: file, modified flag, frame range, object counts by type, renderer, plugins, resolution. No params needed.
    • delete — Delete objects by name (wildcards supported). Required params: {"object_name": "*sphere*"}
    • execute_python — Run arbitrary Python in Maya. Assign result to 'result' variable. Required params: {"code": "import maya.cmds as cmds; ..."} Optional: {"timeout": 60} — seconds to wait for Maya (default 10, max 600); use for long operations, progress heartbeats stream every 10s while waiting.
    • shelf_button — Create a shelf button with Python code. Required params: {"label": "MyBtn", "command": "print('hello')"} Optional: {"tooltip": "...", "shelf_name": "Custom", "icon_label": "MCP"}
    • operation_history — Read recent durable-audit records (read-only; needs MAYA_AUDIT_LOG=1). Optional params: {"limit": 50, "tool": "maya_transform", "action": "execute_python", "status": "error"}
    • publish — Drive the native Toolkit publisher (tk-multi-publish2) inside an engine'd Maya (launched via 'tank'). params: {"mode": "preview"|"publish", "include": ["rig"], "exclude": ["render"], "comment": "...", "timeout": 600}. 'preview' returns the collected publish tree; 'publish' activates matching tasks then validate→publish→finalize. Dependencies are captured automatically by the publish plugins.
    • review_turntable — Deterministic Viewport-2.0 turntable playblast → .mov (RUNS IN MAYA, long op). Frames the model, orbits 360° over [start,end] at fps, 16:9 / square pixels / overscan, offScreen (never Arnold). Required params: {"out_path": "/path.mov"} (resolve via fpt tk_resolve_path template 'movie_asset_publish' with name=<the task name> so the file is {Asset}_{Task}_v###.mov, e.g. DJ_Model_v001.mov — NOT 'turntable'). Optional: {"start":1,"end":100,"fps":25,"width":1920,"height":1080,"objects":[...],"focal":50,"timeout":600}. Returns the .mov plus the engine asset/task and a Version code {Asset}_{Task} so the review Version is named after the task it was generated in.
    • render_still — Single-frame **Arnold** ray-traced still → a PNG at the exact out_path (long op). This is what "a still / render a still" means — a real Arnold render, NOT the VP2.0 grab of maya_viewport_capture (use that for a fast screenshot). Maya only EXPORTS the scene to a .ass; kick renders it out of process, so the review view transform actually applies (a Render View dump writes scene-linear and ignores colour management), no Render View window is opened, and the render can never hang Maya's main thread. Required params: {"out_path": "/review/….png"} (resolve via fpt tk_resolve_path so it lands in the review area with the pipeline name). Optional: {"camera":"persp","frame":42,"width":1920,"height":1080,"aa_samples":3,"view_transform":"…","timeout":600}. Returns JSON {rendered, size_kb, camera, frame, resolution, asset, task, version_code} or {error}. For a still "for review" the version_code ({Asset}_{Task}) is what names the ShotGrid review Version — rendering alone does not put it in review; create the Version (fpt sg_create type=Version) + sg_upload the PNG afterwards.
    """
    _track_call()
    # The two long-running handlers stream progress and take (params, ctx);
    # the rest keep the plain (params) signature on purpose — no silent
    # param drift. Dispatched directly so the types stay honest.
    if params.action == SessionAction.LAUNCH:
        result = await _do_launch(params.params or {}, ctx)
        _audit_record("maya_session", "launch", params.params,
                      _audit.status_from_output(result))
        return result
    if params.action == SessionAction.EXECUTE_PYTHON:
        # _do_execute_python self-audits (ok/error/safety_blocked/ast_rejected).
        return await _do_execute_python(params.params or {}, ctx)
    if params.action == SessionAction.PUBLISH:
        # Native Toolkit publish — long op, takes (params, ctx) like the two
        # above. Audited centrally here (a mutation, not in the skip set).
        result = await _do_publish(params.params or {}, ctx)
        _audit_record("maya_session", "publish", params.params,
                      _audit.status_from_output(result))
        return result
    if params.action == SessionAction.REVIEW_TURNTABLE:
        result = await _do_review_turntable(params.params or {}, ctx)
        _audit_record("maya_session", "review_turntable", params.params,
                      _audit.status_from_output(result))
        return result
    if params.action == SessionAction.RENDER_STILL:
        result = await _do_render_still(params.params or {}, ctx)
        _audit_record("maya_session", "render_still", params.params,
                      _audit.status_from_output(result))
        return result
    dispatch = {
        SessionAction.PING: _do_ping,
        SessionAction.NEW_SCENE: _do_new_scene,
        SessionAction.SAVE_SCENE: _do_save_scene,
        SessionAction.LIST_SCENE: _do_list_scene,
        SessionAction.SCENE_SNAPSHOT: _do_scene_snapshot,
        SessionAction.DELETE: _do_delete,
        SessionAction.SHELF_BUTTON: _do_shelf_button,
        SessionAction.OPERATION_HISTORY: _do_operation_history,
    }
    handler = dispatch[params.action]
    result = await handler(params.params or {})
    # DELETE self-audits; PING/LIST_SCENE/SCENE_SNAPSHOT are read-only (skipped).
    if params.action not in _AUDIT_DISPATCH_SKIP:
        _audit_record("maya_session", params.action.value, params.params,
                      _audit.status_from_output(result))
    return result


# ─────────────────────────────────────────────
# Remote GPU — Vision3D REST API (Hunyuan3D-2)
# ─────────────────────────────────────────────

# Context is imported at the top of the module (it is also used by the
# maya_session / maya_import_file progress streaming added in Chat 63).

# Connection-level configuration (shared by every vision3d server target)
_GPU_API_KEY  = os.environ.get("GPU_API_KEY",  "")
# TLS certificate verification for https Vision3D targets. Secure-by-default:
# verification is ON unless explicitly disabled with GPU_VERIFY_TLS=false (the
# documented opt-out for self-signed LAN https endpoints). For plain-http URLs
# httpx ignores this flag entirely, so LAN http deployments are unaffected.
_GPU_VERIFY   = os.environ.get("GPU_VERIFY_TLS", "true").lower() in ("true", "1", "yes")
_MAC_BASE_DIR = os.environ.get("MAYA_BASE_DIR",
                                str(_PROJECT_ROOT))                          # project root on Mac

# ── Vision3D server selection (per-session, fully runtime) ────────────────
#
# Policy (see memory project_vision3d_server_selection.md):
#   1. **Nothing about Vision3D servers is persisted anywhere.** No config
#      file field, no hardcoded defaults, no list of candidates. The URL
#      lives only in the running process's memory for the duration of the
#      session, and only after the user explicitly types it.
#   2. At process start no server is selected. The first handler that
#      needs to talk to the GPU returns ``vision3d_url_required``. The LLM
#      asks the user "which Vision3D URL?", the user types it into the
#      chat, the LLM calls ``select_server`` with that URL. Selection is
#      cached in ``_selected_vision3d`` for the rest of the process.
#   3. Subsequent calls reuse the chosen client from ``_http_clients``.
#   4. On restart of the MCP server, the selection is forgotten by design.
#
# Retro-compat: ``GPU_API_URL`` env var is honored as a **suggested default**
# the LLM can surface in the prompt, but it is NOT auto-selected — the user
# still has to confirm/override via ``select_server``. This keeps pre-
# selector installs working without muting the per-session policy.

_selected_vision3d: Optional[str] = None  # URL picked for this session (None = ask)
_http_clients: dict = {}                  # URL -> httpx.AsyncClient (lazy cache)

# Track log cursors per job (for incremental log delivery)
_job_log_cursors: dict[str, int] = {}


def _is_valid_http_url(url: str) -> bool:
    """Return True if ``url`` parses as an http/https URL with a host.

    This is the only validation applied to the URL the user provides via
    ``select_server``. There is no whitelist.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _build_http_client(url: str):
    """Create a fresh httpx.AsyncClient bound to the given base URL."""
    import httpx
    headers = {}
    if _GPU_API_KEY:
        headers["x-api-key"] = _GPU_API_KEY
    return httpx.AsyncClient(
        base_url=url,
        headers=headers,
        verify=_GPU_VERIFY,
        timeout=httpx.Timeout(connect=10, read=900, write=60, pool=10),
    )


def _vision3d_url_required_error() -> str:
    """Return the JSON payload shown when no Vision3D URL has been picked.

    Nothing is persisted anywhere: the LLM must ask the user for the URL
    in the chat on the first Vision3D call of the session. If the
    ``GPU_API_URL`` env var is set it is surfaced as a *suggested default*
    for the user to confirm or override — it is never auto-selected.
    """
    suggested = os.environ.get("GPU_API_URL", "").strip().rstrip("/")
    payload = {
        "error": "vision3d_url_required",
        "hint": (
            "No Vision3D server URL has been set for this MCP session. "
            "Ask the user which Vision3D endpoint to use (e.g. the local "
            "MPS server or a remote CUDA host) and have them type the full "
            "URL into the chat. Then call "
            "maya_vision3d(action='select_server', params={'url': '<the-url>'}). "
            "The URL is cached in memory for the rest of this session and "
            "forgotten when the MCP server restarts."
        ),
    }
    if suggested:
        payload["suggested_default"] = suggested
        payload["hint"] += (
            f" A suggested default is available via GPU_API_URL=\"{suggested}\", "
            "but it is NOT auto-selected — the user must confirm it explicitly."
        )
    return json.dumps(payload, indent=2)


def _resolve_client_or_error() -> tuple:
    """Return ``(client, error_json)``.

    - If a URL has been selected: ``(AsyncClient, None)``.
      The client is created lazily on first use and cached in
      ``_http_clients``.
    - If no URL has been selected yet: ``(None, json_error_string)``.
      The caller must return the error string verbatim to the MCP caller.
    """
    if _selected_vision3d is None:
        return None, _vision3d_url_required_error()
    url = _selected_vision3d
    client = _http_clients.get(url)
    if client is None:
        client = _build_http_client(url)
        _http_clients[url] = client
    return client, None


async def _download_file(job_id: str, filename: str, dest: Path) -> bool:
    """Download a single file from a completed job.

    Precondition: a vision3d server must already be selected. The caller is
    responsible for surfacing the selection error; this helper assumes a
    client is available.
    """
    client, err = _resolve_client_or_error()
    if err is not None or client is None:
        return False
    resp = await client.get(f"/api/jobs/{job_id}/files/{filename}")
    if resp.status_code == 200:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    return False


def _build_quality_form_data(params) -> dict:
    """Build form_data dict with quality params from a ShapeGenerateInput or ShapeTextInput."""
    form_data = {}
    # Only forward target_faces when the caller asked for decimation (> 0).
    # target_faces == 0 (the ShapeTextInput default) means "no decimation":
    # omit it so the GPU server applies its own default instead of being
    # pinned to 0. Mirrors the octree_resolution / num_inference_steps guards
    # below.
    if hasattr(params, "target_faces") and params.target_faces > 0:
        form_data["target_faces"] = str(params.target_faces)
    if params.preset:
        form_data["preset"] = params.preset
    if params.model:
        form_data["model"] = params.model
    if params.octree_resolution > 0:
        form_data["octree_resolution"] = str(params.octree_resolution)
    if params.num_inference_steps > 0:
        form_data["num_inference_steps"] = str(params.num_inference_steps)
    return form_data


# ── Input models ──────────────────────────────────────────────────────────


class ShapeGenerateInput(BaseModel):
    """Parameters for initiating 3D generation from image in Vision3D.

    Quality presets:
      - low:    turbo, octree 256, 10 steps, 10k faces   (~1 min, fast preview)
      - medium: turbo, octree 384, 20 steps, 50k faces   (~2 min, general use)
      - high:   full,  octree 384, 30 steps, 150k faces  (~8 min, detailed)
      - ultra:  full,  octree 512, 50 steps, no limit    (~12 min, maximum detail)
    """
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    image_path: str = Field(
        ...,
        description="Absolute local path to reference image (.jpg/.png)."
    )
    output_subdir: str = Field(
        default="0",
        description="Output subdirectory within reference/3d_output/ (e.g., '0', 'asset_1478')"
    )
    preset: str = Field(
        default="",
        description="Quality preset: 'low', 'medium', 'high', 'ultra'. "
                    "Individual parameters override the preset."
    )
    model: str = Field(
        default="",
        description="Shape model: 'turbo' (~1 min) or 'full' (~5 min, more detail). "
                    "Empty = use preset's or 'turbo' by default."
    )
    octree_resolution: int = Field(
        default=0,
        description="Octree resolution (256/384/512). 0 = use preset's."
    )
    num_inference_steps: int = Field(
        default=0,
        description="Inference steps. turbo: 5-10, full: 30-50. 0 = use preset's."
    )
    target_faces: int = Field(
        default=50000,
        description="Target faces after decimation. 0 = no decimation."
    )


class ShapeTextInput(BaseModel):
    """Parameters for initiating 3D generation from text in Vision3D."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text_prompt: str = Field(
        ...,
        description="English description of the 3D object to generate."
    )
    output_subdir: str = Field(
        default="0",
        description="Output subdirectory (e.g., '0', 'mailbox_0')"
    )
    preset: str = Field(default="", description="Preset: 'low', 'medium', 'high', 'ultra'.")
    model: str = Field(default="", description="'turbo' or 'full'. Empty = preset.")
    octree_resolution: int = Field(default=0, description="256/384/512. 0 = preset.")
    num_inference_steps: int = Field(default=0, description="Steps. 0 = preset.")
    target_faces: int = Field(default=0, description="Target faces. 0 = no decimation.")


class TextureRemoteInput(BaseModel):
    """Parameters for initiating texturing in Vision3D."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    output_subdir: str = Field(
        ...,
        description="Subdirectory within reference/3d_output/"
    )
    mesh_filename: str = Field(
        default="mesh.glb",
        description="Mesh filename within output_subdir"
    )
    image_filename: str = Field(
        default="input.png",
        description="Reference image filename within output_subdir"
    )


class Vision3DPollInput(BaseModel):
    """Parameters for polling job status in Vision3D."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., description="Job ID returned by shape_generate_remote/text/texture.")


class Vision3DDownloadInput(BaseModel):
    """Parameters for downloading results from a completed job."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., description="Completed job ID.")
    output_subdir: str = Field(..., description="Local output subdirectory (same as used when creating the job).")
    files: List[str] = Field(
        default_factory=lambda: ["textured.glb", "mesh_uv.obj", "texture_baked.png", "mesh.glb"],
        description="List of files to download. By default downloads all from the complete pipeline."
    )


# ── Tools: server selection (per-session, freeform URL) ────────


async def _do_v3d_select_server(params: dict, ctx: Context) -> str:
    """Set the Vision3D server URL for the rest of this MCP session.

    Required param: ``url`` — any valid ``http://`` or ``https://`` URL.
    There is **no whitelist and no predefined pool**: the LLM must have
    asked the user for the URL in the chat and passed whatever they typed
    straight into this call. The selection is cached in
    ``_selected_vision3d`` until the MCP process is restarted.

    Trailing slashes in the supplied URL are tolerated (normalized out).
    """
    global _selected_vision3d

    raw_url = (params or {}).get("url")
    if not raw_url or not isinstance(raw_url, str):
        return json.dumps({
            "error": "Missing required param 'url'.",
            "hint": (
                "Call with params={'url': '<http-or-https-url>'}. "
                "Ask the user for the URL if you do not have it yet."
            ),
        })

    url = raw_url.strip().rstrip("/")
    if not _is_valid_http_url(url):
        return json.dumps({
            "error": f"Invalid URL: {raw_url!r}",
            "hint": (
                "Expected an http:// or https:// URL with a host. "
                "Ask the user to retype it."
            ),
        })

    _selected_vision3d = url
    await ctx.info(f"Vision3D server set to {url} for this session.")
    return json.dumps({
        "status": "selected",
        "url": url,
        "note": (
            "This URL is cached in memory for the rest of this MCP session. "
            "Restarting the MCP server will clear it — the user will be "
            "asked again on the next run."
        ),
    }, indent=2)


# ── Tools: check Vision3D availability ─────────────────────────


async def _do_v3d_health(params: dict, ctx: Context) -> str:
    """Check if the selected Vision3D server is available and responding.

    Returns GPU information, available models, and text-to-3D status.
    Call this after selecting a server to confirm connectivity before
    submitting generation jobs.

    If no server has been selected yet, returns ``server_selection_required``
    with the list of available URLs.
    """
    client, err = _resolve_client_or_error()
    if err is not None:
        return err
    selected = _selected_vision3d
    try:
        await ctx.info(f"Checking Vision3D availability at {selected}...")
        resp = await client.get("/api/health", timeout=5.0)

        if resp.status_code != 200:
            return json.dumps({
                "available": False,
                "error": f"Vision3D responded with HTTP {resp.status_code}",
                "url": selected,
            })

        health = resp.json()
        return json.dumps({
            "available": True,
            "url": selected,
            "gpu": health.get("gpu", "unknown"),
            "vram_gb": health.get("vram_gb"),
            "models": health.get("models", []),
            "text_to_3d": health.get("text_to_3d", "unknown"),
        }, indent=2)

    except Exception as e:
        return json.dumps({
            "available": False,
            "error": f"Could not connect to Vision3D ({selected}): {e}",
            "hint": "Verify that the selected Vision3D server is running and reachable from this host.",
        })


# ── Tools: start jobs (non-blocking) ───────────────────────────────────


async def _do_v3d_generate_image(params: dict, ctx: Context) -> str:
    """Start textured 3D generation from image in Vision3D (non-blocking).

    Uploads the image and starts the complete pipeline (shape + decimation + texturing).
    Returns a job_id immediately. Use poll to follow progress and download to get results.
    """
    from pydantic import ValidationError
    try:
        validated = ShapeGenerateInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for generate_image: {e}"})

    client, err = _resolve_client_or_error()
    if err is not None:
        return err

    try:
        image_local = Path(validated.image_path)
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / validated.output_subdir

        if not image_local.exists():
            return json.dumps({
                "error": f"Image not found: {image_local}",
                "hint": "Download the image first with sg_download from fpt-mcp."
            })

        out_dir.mkdir(parents=True, exist_ok=True)

        # Copy image to output directory as input.png
        import shutil
        input_copy = out_dir / "input.png"
        shutil.copy2(str(image_local), str(input_copy))

        quality_desc = validated.preset or f"model={validated.model or 'turbo'}"

        await ctx.info(f"Uploading image to Vision3D ({quality_desc}) at {_selected_vision3d}...")

        form_data = {"output_subdir": validated.output_subdir}
        form_data.update(_build_quality_form_data(validated))

        with open(str(image_local), "rb") as f:
            resp = await client.post(
                "/api/generate-full",
                files={"image": (image_local.name, f, "image/png")},
                data=form_data,
            )

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verify Vision3D is running: curl -k {_selected_vision3d}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Job started: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": validated.output_subdir,
            "output_dir": str(out_dir),
            "quality": quality_desc,
            "image_copy": str(input_copy),
            "next_step": f"Call maya_vision3d(action='poll', params={{'job_id': '{job_id}'}}) to see progress. "
                         f"When status is 'completed', call maya_vision3d(action='download', "
                         f"params={{'job_id': '{job_id}', 'output_subdir': '{validated.output_subdir}'}}).",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


async def _do_v3d_generate_text(params: dict, ctx: Context) -> str:
    """Start 3D generation from text in Vision3D (non-blocking).

    Sends the prompt and starts the text-to-3D pipeline.
    Returns job_id. Use poll to follow progress.
    """
    from pydantic import ValidationError
    try:
        validated = ShapeTextInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for generate_text: {e}"})

    client, err = _resolve_client_or_error()
    if err is not None:
        return err

    try:
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / validated.output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)

        quality_desc = validated.preset or f"model={validated.model or 'turbo'}"

        await ctx.info(
            f"Sending prompt to Vision3D ({_selected_vision3d}): "
            f"'{validated.text_prompt}' ({quality_desc})..."
        )

        form_data = {
            "text_prompt": validated.text_prompt,
            "output_subdir": validated.output_subdir,
        }
        form_data.update(_build_quality_form_data(validated))

        resp = await client.post("/api/generate-text", data=form_data)

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verify Vision3D is running: curl -k {_selected_vision3d}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Job started: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": validated.output_subdir,
            "output_dir": str(out_dir),
            "quality": quality_desc,
            "next_step": f"Call maya_vision3d(action='poll', params={{'job_id': '{job_id}'}}) to follow progress.",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


async def _do_v3d_texture(params: dict, ctx: Context) -> str:
    """Start mesh texturing in Vision3D (non-blocking).

    Uploads mesh + image and starts the texturing pipeline.
    Returns job_id. Use poll to follow progress.
    """
    from pydantic import ValidationError
    try:
        validated = TextureRemoteInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for texture: {e}"})

    client, err = _resolve_client_or_error()
    if err is not None:
        return err

    try:
        out_dir     = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / validated.output_subdir
        mesh_local  = out_dir / validated.mesh_filename
        image_local = out_dir / validated.image_filename

        if not mesh_local.exists():
            return json.dumps({
                "error": f"Mesh not found: {mesh_local}",
                "hint":  "Generate the mesh first with maya_vision3d(action='generate_image', ...)."
            })
        if not image_local.exists():
            return json.dumps({
                "error":  f"Image not found: {image_local}",
                "hint":   f"Copy the image as '{validated.image_filename}' in {out_dir}"
            })

        await ctx.info(
            f"Uploading {validated.mesh_filename} + {validated.image_filename} "
            f"to Vision3D at {_selected_vision3d}..."
        )

        with open(str(mesh_local), "rb") as mf, open(str(image_local), "rb") as imf:
            resp = await client.post(
                "/api/texture-mesh",
                files={
                    "mesh": (validated.mesh_filename, mf, "application/octet-stream"),
                    "image": (validated.image_filename, imf, "image/png"),
                },
                data={"output_subdir": validated.output_subdir},
            )

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Check Vision3D: curl -k {_selected_vision3d}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Texturing job started: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": validated.output_subdir,
            "next_step": f"Call maya_vision3d(action='poll', params={{'job_id': '{job_id}'}}) to follow progress.",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tools: poll progress and download ──────────────────────────────────


async def _do_v3d_poll(params: dict, ctx: Context) -> str:
    """Poll job status in Vision3D. Returns new log lines since last call (incremental progress).

    Call repeatedly while status is 'running'.
    When status is 'completed', call download.
    When status is 'failed', show the error to the user.
    """
    from pydantic import ValidationError
    try:
        validated = Vision3DPollInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for poll: {e}"})

    client, err = _resolve_client_or_error()
    if err is not None:
        return err

    try:
        resp = await client.get(f"/api/jobs/{validated.job_id}")

        if resp.status_code == 404:
            return json.dumps({"error": f"Job '{validated.job_id}' not found in Vision3D."})

        resp.raise_for_status()
        job = resp.json()

        # Deliver only new log lines since last poll
        cursor = _job_log_cursors.get(validated.job_id, 0)
        all_log = job.get("log", [])
        new_lines = all_log[cursor:]
        _job_log_cursors[validated.job_id] = len(all_log)

        # ctx.info for future MCP progress support
        for line in new_lines:
            await ctx.info(line)

        elapsed = job.get("elapsed_s", 0)
        status = job["status"]

        result = {
            "status": status,
            "elapsed_s": elapsed,
            "new_log_lines": new_lines,
            "total_log_lines": len(all_log),
        }

        if status == "completed":
            result["files"] = [f["name"] for f in job.get("files", [])]
            result["next_step"] = (
                f"Job completed in {elapsed}s. Call maya_vision3d(action='download', "
                f"params={{'job_id': '{validated.job_id}', 'output_subdir': '...'}}) to download files."
            )
            # Cleanup cursor
            _job_log_cursors.pop(validated.job_id, None)
        elif status == "failed":
            result["error"] = job.get("error", "Unknown error")
            _job_log_cursors.pop(validated.job_id, None)
        else:
            result["next_step"] = (
                f"Job in progress ({elapsed}s). Call "
                f"maya_vision3d(action='poll', params={{'job_id': '{validated.job_id}'}}) again to update."
            )

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


async def _do_v3d_download(params: dict, ctx: Context) -> str:
    """Download files from a completed Vision3D job to local directory.

    Call after poll reports status='completed'.
    Downloads specified files to local output subdirectory.
    """
    from pydantic import ValidationError
    try:
        validated = Vision3DDownloadInput(**params)
    except ValidationError as e:
        return json.dumps({"error": f"Invalid params for download: {e}"})

    # Resolve early so an unselected session returns server_selection_required
    # instead of a confusing bulk-download failure.
    _client, err = _resolve_client_or_error()
    if err is not None:
        return err

    try:
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / validated.output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)

        await ctx.info(
            f"Downloading {len(validated.files)} files from Vision3D at {_selected_vision3d}..."
        )

        downloaded = []
        failed = []

        for fname in validated.files:
            ok = await _download_file(validated.job_id, fname, out_dir / fname)
            if ok:
                size_kb = (out_dir / fname).stat().st_size // 1024
                downloaded.append({"name": fname, "size_kb": size_kb})
                await ctx.info(f"  {fname} ({size_kb} KB)")
            else:
                failed.append(fname)

        baked_ready = (out_dir / "mesh_uv.obj").exists() and \
                      (out_dir / "texture_baked.png").exists()
        textured_ready = (out_dir / "textured.glb").exists()

        return json.dumps({
            "status": "ok",
            "output_dir": str(out_dir),
            "downloaded": downloaded,
            "failed": failed,
            "textured": textured_ready,
            "baked_texture": baked_ready,
            "next_step": (
                "Files downloaded. Import textured.glb in Maya with maya_session(action='execute_python', ...), "
                "or use mesh_uv.obj + texture_baked.png for full UV control."
                if textured_ready else
                "Partial download. Check 'failed' to see which files failed."
            ),
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────
# Vision3D Dispatch Tool
# ─────────────────────────────────────────────

@mcp.tool(name="maya_vision3d")
async def maya_vision3d(params: Vision3DDispatchInput, ctx: Context) -> str:
    """AI-powered 3D asset generation via Vision3D server (requires GPU with Hunyuan3D-2).

    Server selection is fully per-session and runtime-only: the first
    action that needs a GPU call returns ``vision3d_url_required``. The
    LLM must ask the user for the Vision3D URL in the chat and then call
    ``select_server`` with that URL. Nothing is persisted to disk — the
    URL lives only in process memory until the MCP server restarts.

    After selection, jobs are non-blocking: start → poll → download.

    Available actions:

    • select_server — Set the Vision3D server URL for the rest of the session. Required params: {"url": "http://..."}. Accepts any valid http/https URL; ask the user first.
    • health — Check if the selected Vision3D server is running and what GPU/models are available. No params.
    • generate_image — Start 3D generation from a reference image. Required params: {"image_path": "/path/to/image.png", "output_subdir": "my_asset"} Optional: {"preset": "medium", "model": "turbo", "octree_resolution": 384, "num_inference_steps": 20, "target_faces": 50000}
    • generate_text — Start 3D generation from a text prompt. Required params: {"text_prompt": "a medieval sword", "output_subdir": "sword"} Optional: {"preset": "medium", "model": "turbo", "octree_resolution": 384, "num_inference_steps": 20, "target_faces": 50000}
    • texture — Texture an existing mesh using a reference image. Required params: {"output_subdir": "my_asset"} Optional: {"mesh_filename": "mesh.glb", "image_filename": "input.png"}
    • poll — Check job progress (call repeatedly while running). Required params: {"job_id": "uuid-from-generate"}
    • download — Download completed job results. Required params: {"job_id": "uuid", "output_subdir": "my_asset"} Optional: {"files": ["textured.glb", "mesh.glb"]}
    """
    from maya_mcp.suggestions import maybe_annotate_with_suggestions
    _track_call()
    dispatch = {
        Vision3DAction.SELECT_SERVER: _do_v3d_select_server,
        Vision3DAction.HEALTH: _do_v3d_health,
        Vision3DAction.GENERATE_IMAGE: _do_v3d_generate_image,
        Vision3DAction.GENERATE_TEXT: _do_v3d_generate_text,
        Vision3DAction.TEXTURE: _do_v3d_texture,
        Vision3DAction.POLL: _do_v3d_poll,
        Vision3DAction.DOWNLOAD: _do_v3d_download,
    }
    handler = dispatch[params.action]
    out = await handler(params.params or {}, ctx)
    return maybe_annotate_with_suggestions("maya_vision3d", out)


# ─────────────────────────────────────────────
# WorldLabs Dispatch Tool (Gaussian-splat environments via the Marble API)
# ─────────────────────────────────────────────

async def _do_wl_health(params: dict, ctx: Context) -> str:
    from maya_mcp.worldlabs import tool as wl
    return await asyncio.to_thread(wl.health)


async def _do_wl_generate(params: dict, ctx: Context) -> str:
    from maya_mcp.worldlabs import tool as wl
    image = params.get("image")
    if not image:
        return json.dumps({"error": "generate requires params.image (local path or https URI)"})
    return await asyncio.to_thread(
        wl.generate, image, params.get("output_subdir", "world"),
        params.get("model", "marble-1.1"), params.get("display_name"),
        params.get("text_prompt"), bool(params.get("confirm", False)),
        params.get("work_dir"),
    )


async def _do_wl_poll(params: dict, ctx: Context) -> str:
    from maya_mcp.worldlabs import tool as wl
    op = params.get("operation_id")
    if not op:
        return json.dumps({"error": "poll requires params.operation_id"})
    return await asyncio.to_thread(wl.poll, op)


async def _do_wl_download(params: dict, ctx: Context) -> str:
    from maya_mcp.worldlabs import tool as wl
    op = params.get("operation_id")
    dest = params.get("dest_dir")
    if not op or not dest:
        return json.dumps({"error": "download requires params.operation_id and params.dest_dir"})
    which = tuple(params.get("which") or ("splats_full_res", "pano"))
    return await asyncio.to_thread(wl.download, op, dest, which)


async def _do_wl_convert(params: dict, ctx: Context) -> str:
    from maya_mcp.worldlabs import tool as wl
    spz = params.get("spz_path")
    if not spz:
        return json.dumps({"error": "convert requires params.spz_path"})
    return await asyncio.to_thread(wl.convert, spz, params.get("ply_path"))


async def _do_wl_status(params: dict, ctx: Context) -> str:
    from maya_mcp.worldlabs import tool as wl
    work_dir = params.get("work_dir")
    if not work_dir:
        return json.dumps({"error": "status requires params.work_dir (the Toolkit work area)"})
    return await asyncio.to_thread(wl.status, work_dir)


async def _do_wl_build(params: dict, ctx: Context) -> str:
    # RUNS IN MAYA: ships the validated build recipe to the Command Port bridge.
    from maya_mcp.worldlabs import tool as wl
    ply = params.get("ply_path")
    if not ply:
        return json.dumps({"error": "build requires params.ply_path"})
    code = wl.build_maya_code(
        ply, params.get("pano_path"),
        float(params.get("eye_height", 1.5)),
        int(params.get("proxy_step", 1)),
        bool(params.get("relight", False)),
        int(params.get("draw_mode", 2)),
        float(params.get("focal", 15.0)),
        params.get("save_path"),
    )
    try:
        return await asyncio.to_thread(
            bridge.execute, code, timeout=int(params.get("timeout", 300))
        )
    except MayaBridgeError as exc:
        return json.dumps({
            "error": f"Maya bridge error: {exc}",
            "hint": "The build action runs inside Maya — ensure Maya is open "
                    "with the Command Port active.",
        })


@mcp.tool(name="maya_worldlabs")
async def maya_worldlabs(params: WorldLabsDispatchInput, ctx: Context) -> str:
    """Generate a World Labs (Marble) Gaussian-splat ENVIRONMENT from an image
    and load it into Maya for Arnold.

    Pipeline — call the actions in order: generate → poll → download → convert →
    build. Credits are spent ONLY by generate with confirm=true. Call status on a
    work area to resume an interrupted run from disk without re-generating.

    Actions:

    • health — Check the WorldLabs API key + credit balance. No params.
    • generate — Start image→world generation. Required params: {"image": "/path.png" or "https://..."}. Optional: {"output_subdir": "world", "model": "marble-1.1"|"marble-1.1-plus", "display_name": ..., "text_prompt": ..., "confirm": true, "work_dir": "/work/worldlabs/<asset>"}. WITHOUT confirm=true it returns a cost-confirmation payload and spends NOTHING. Pass work_dir (the Toolkit work area, resolved via fpt-mcp tk_resolve_path) to write a resume sidecar so an interrupted run resumes without re-generating.
    • poll — Poll a generation (~5 min). Required params: {"operation_id": "..."}.
    • download — Download a finished world's assets to the work area. Required params: {"operation_id": "...", "dest_dir": "/work/worldlabs/<asset>"}. Optional: {"which": ["splats_full_res", "pano"]}. Updates the resume sidecar (world_id + downloaded paths).
    • convert — Convert the downloaded SPZ to PLY (Arnold-readable, via gsbox). Required params: {"spz_path": "/path.spz"}. Optional: {"ply_path": "/out.ply"}.
    • build — Load into Maya (RUNS IN MAYA): aiGaussianSplat + coloured point proxy + emission shader + eye-level centred camera, plus a fake-HDR panorama dome if given. Required params: {"ply_path": "/world.ply"}. Optional: {"pano_path": "/pano.png", "eye_height": 1.5, "proxy_step": 1, "relight": false, "draw_mode": 2, "focal": 15.0, "timeout": 300, "save_path": "/…/maya/scene.v001.ma"}. draw_mode: 2=Gaussian Splat (default, draws natively in VP2.0), 1=Point Cloud, 0=Bounding Box. focal: camera focal length in mm (default 15, wide for environments). save_path: when given (the Toolkit work-file path resolved via fpt-mcp tk_resolve_path on template maya_asset_work — Toolkit naming/versioning), the assembled scene is saved there so "open in Maya" lands the work file at the config-correct path with no manual Workfiles pick.
    • status — Report the resumable state of a work area (sidecar + on-disk .spz/.ply/.png). Required params: {"work_dir": "/work/worldlabs/<asset>"}. Returns where the pipeline left off (needs_generate / needs_download / needs_convert / ready_to_build).
    """
    from maya_mcp.suggestions import maybe_annotate_with_suggestions
    _track_call()
    dispatch = {
        WorldLabsAction.HEALTH: _do_wl_health,
        WorldLabsAction.GENERATE: _do_wl_generate,
        WorldLabsAction.POLL: _do_wl_poll,
        WorldLabsAction.DOWNLOAD: _do_wl_download,
        WorldLabsAction.CONVERT: _do_wl_convert,
        WorldLabsAction.BUILD: _do_wl_build,
        WorldLabsAction.STATUS: _do_wl_status,
    }
    handler = dispatch[params.action]
    out = await handler(params.params or {}, ctx)
    return maybe_annotate_with_suggestions("maya_worldlabs", out)


# ---------------------------------------------------------------------------
# RAG Tools (mirrors fpt-mcp architecture)
# ---------------------------------------------------------------------------

class SearchMayaDocsInput(BaseModel):
    """Parameters for searching Maya API documentation."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        description=(
            "Natural language query about Maya Python API. Examples: "
            "'how to set keyframe tangents', 'arnold AOV setup', "
            "'polyBevel flags', 'USD export with materials'."
        ),
    )
    n_results: int = Field(
        default=5,
        description="Number of documentation chunks to return (1-10).",
        ge=1,
        le=10,
    )


@mcp.tool(name="search_maya_docs")
async def search_maya_docs_tool(params: SearchMayaDocsInput) -> str:
    """Search Maya API documentation using hybrid RAG (semantic + BM25).

    Call this BEFORE writing complex Maya commands, using unfamiliar flags,
    or when unsure about command names, return values, or syntax.
    Returns the most relevant documentation chunks with relevance scores.

    Covers: maya.cmds, PyMEL, Arnold/mtoa, Maya-USD, and common anti-patterns.
    Uses HyDE query expansion + Reciprocal Rank Fusion for high precision.
    """
    global _last_rag_score, _rag_called_this_session
    _track_call()

    try:
        from maya_mcp.rag.search import search
        text, relevance = search(params.query, n_results=params.n_results)
    except ImportError:
        return json.dumps({
            "error": "RAG dependencies not installed. Run: pip install chromadb sentence-transformers rank-bm25",
            "fallback": "Proceed with caution — no documentation verification available.",
        })
    except Exception as e:
        return json.dumps({"error": f"RAG search failed: {e}"})

    _stats["rag_calls"] += 1
    _stats["tokens_saved"] += _FULL_DOC_TOKENS - _tok(text)
    _last_rag_score = relevance
    _rag_called_this_session = True

    result = {
        "documentation": text,
        "max_relevance": relevance,
        "chunks_returned": params.n_results,
    }

    if relevance < 60:
        result["warning"] = (
            f"Low relevance ({relevance}%) — this query may cover an undocumented area. "
            "Proceed carefully. If your approach works, call learn_pattern to save it."
        )

    return json.dumps(result, default=str)


class LearnPatternInput(BaseModel):
    """Parameters for saving a validated working pattern."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    description: str = Field(
        description="Short description of what the pattern does (e.g. 'set Arnold AOV via Python').",
    )
    code: str = Field(
        description="The working code/command pattern to remember.",
    )
    api: str = Field(
        default="maya_cmds",
        description="Which API this pattern belongs to: 'maya_cmds', 'pymel', 'arnold', 'usd', or 'anti_patterns'.",
    )


@mcp.tool(name="learn_pattern")
async def learn_pattern_tool(params: LearnPatternInput) -> str:
    """Save a validated working pattern to the RAG knowledge base.

    Call this after a successful operation when search_maya_docs returned
    low relevance (< 60%), indicating the pattern was not well-documented.
    The pattern will be available in future sessions.

    Model trust gates: only Opus/Fable can write directly.
    Other models stage candidates for review.
    """
    _track_call()
    if _model_can_write():
        # Direct write to docs
        api_file_map = {
            "maya_cmds": "CMDS_API.md",
            "pymel": "PYMEL_API.md",
            "arnold": "ARNOLD_API.md",
            "usd": "USD_API.md",
            "anti_patterns": "ANTI_PATTERNS.md",
        }
        doc_file = api_file_map.get(params.api, "CMDS_API.md")
        doc_path = _SERVER_DIR / "docs" / doc_file

        try:
            entry = (
                f"\n\n## Learned: {params.description}\n\n"
                f"```python\n{params.code}\n```\n"
            )
            with open(doc_path, "a", encoding="utf-8") as f:
                f.write(entry)
            _stats["patterns_learned"] += 1

            # Clear RAG cache so new pattern is found on next search
            try:
                from maya_mcp.rag.search import clear_cache
                clear_cache()
            except ImportError:
                pass

            return json.dumps({
                "status": "learned",
                "description": params.description,
                "file": doc_file,
                "searchable": False,
                "note": (
                    f"Pattern appended to docs/{doc_file}, and the in-memory "
                    "search cache was cleared. It is NOT searchable yet: the "
                    "ChromaDB index and the BM25 corpus.json are built offline "
                    "and are not rebuilt by this call. An operator must run "
                    "`python -m maya_mcp.rag.build_index` before this pattern "
                    "appears in search_maya_docs results."
                ),
            })
        except Exception as e:
            return json.dumps({"error": f"Failed to write pattern: {e}"})
    else:
        # Stage candidate for review
        candidates_path = _SERVER_DIR / "rag" / "candidates.json"
        try:
            candidates = json.loads(candidates_path.read_text()) if candidates_path.exists() else []
        except Exception:
            candidates = []

        candidates.append({
            "description": params.description,
            "code": params.code,
            "api": params.api,
            "model": _get_current_model(),
            "timestamp": datetime.datetime.now().isoformat(),
        })

        try:
            candidates_path.parent.mkdir(parents=True, exist_ok=True)
            candidates_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False))
        except Exception:
            pass

        _stats["patterns_staged"] += 1

        return json.dumps({
            "status": "staged",
            "description": params.description,
            "note": f"Model '{_get_current_model()}' is read-only. Pattern staged for review.",
        })


@mcp.tool(name="session_stats")
async def session_stats_tool() -> str:
    """Show session efficiency statistics: token usage, RAG savings, patterns learned.

    Call at the end of multi-step tasks or when asked about efficiency.
    Shows how much context was saved by RAG vs loading full documentation.
    """
    _track_call()
    used = _stats["tokens_in"] + _stats["tokens_out"]
    saved = _stats["tokens_saved"]
    total = used + saved
    ratio = f"{saved / total * 100:.0f}%" if total > 0 else "—"
    uptime = str(datetime.datetime.now() - _stats_reset_at).split(".")[0]

    # F0: p_fallo = failed_turns / turns_total over the execute_python path.
    turns = _stats["turns_total"]
    failed = _stats["failed_turns"]
    p_fallo = f"{failed / turns * 100:.0f}%" if turns > 0 else "—"

    return json.dumps({
        "session_duration": uptime,
        "tool_calls": _stats["exec_calls"],
        "rag_calls": _stats["rag_calls"],
        "tokens_used": used,
        "tokens_saved_by_rag": saved,
        "token_efficiency": ratio,
        "patterns_learned": _stats["patterns_learned"],
        "patterns_staged": _stats["patterns_staged"],
        "safety_blocks": _stats["safety_blocks"],
        "cache_hits": _stats["cache_hits"],
        "execute_python_turns": turns,
        "failed_turns": failed,
        "p_fallo": p_fallo,
        "full_doc_baseline": _FULL_DOC_TOKENS,
    }, indent=2)


@mcp.tool(name="reset_session_stats")
async def reset_session_stats_tool() -> str:
    """Zero the session stats counters immediately.

    Use at the start of a new Claude session (or a fresh debugging run) when
    the idle-based auto-reset has not fired — for example when two sessions
    happen back-to-back. Returns a confirmation line with the new reset
    timestamp.
    """
    global _stats_reset_at
    _track_call()
    now = datetime.datetime.now()
    _stats_reset_at = _reset_stats_helper(_stats, now)
    return json.dumps({
        "status": "reset",
        "reset_at": now.strftime("%H:%M:%S"),
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Maya panel auto-setup — inject userSetup.py + menu + panel on first connect
# ---------------------------------------------------------------------------

# _PROJECT_ROOT already defined near top of file (as Path, not str)
_panel_setup_done = False


def _setup_maya_panel():
    """Install MCP Pipeline menu & panel inside Maya.

    Strategy:
      1. Add project root to sys.path (current session)
      2. Inject a guarded block into Maya's userSetup.py so the path
         persists across restarts — standard VFX industry approach
      3. Install "MCP Pipeline" menu if missing
      4. Open the console panel

    The userSetup.py injection is the key: Maya executes userSetup.py on
    every startup, so ``from console.maya_panel import ...`` works BEFORE
    the MCP server connects.  This fixes the retain=True workspaceControl
    restore problem that .mod files failed to solve in Maya 2026.

    Safe for cross-MCP usage (fpt-mcp/flame-mcp): code executes inside
    Maya via TCP regardless of which console started the server.
    """
    global _panel_setup_done
    if _panel_setup_done:
        return
    try:
        setup_code = f'''
import sys, os, maya.cmds as cmds, maya.utils

_mcp_root = r"{_PROJECT_ROOT}"
_mcp_port = {MAYA_PORT}

# 1. Current session: add to sys.path now
if _mcp_root not in sys.path:
    sys.path.insert(0, _mcp_root)

# 2. Persistent: inject into userSetup.py for future restarts
_SENTINEL = "# --- MCP Pipeline Console auto-setup ---"
_END_SENTINEL = "# --- end MCP Pipeline Console ---"
NL = chr(10)

def _inject_user_setup():
    maya_ver = cmds.about(version=True)
    candidates = [
        os.path.expanduser("~/Library/Preferences/Autodesk/maya/" + maya_ver + "/scripts"),
        os.path.expanduser("~/maya/" + maya_ver + "/scripts"),
        os.path.expanduser("~/Library/Preferences/Autodesk/maya/scripts"),
        os.path.expanduser("~/maya/scripts"),
    ]
    target_dir = None
    for d in candidates:
        if os.path.isdir(d):
            target_dir = d
            break
        parent = os.path.dirname(d)
        if os.path.isdir(parent):
            os.makedirs(d, exist_ok=True)
            target_dir = d
            break
    if not target_dir:
        cmds.warning("[MCP] No Maya scripts dir found for userSetup.py")
        return False

    us_path = os.path.join(target_dir, "userSetup.py")

    snippet_lines = [
        _SENTINEL,
        "import sys as _mcp_sys",
        '_mcp_root = r"' + _mcp_root + '"',
        "if _mcp_root not in _mcp_sys.path:",
        "    _mcp_sys.path.insert(0, _mcp_root)",
        "import maya.utils as _mcp_utils",
        "def _mcp_open_command_port():",
        "    try:",
        "        import maya.cmds as _mc",
        '        if not _mc.commandPort("localhost:' + str(_mcp_port) + '", query=True):',
        '            _mc.commandPort(name="localhost:' + str(_mcp_port) + '", sourceType="mel")',
        "    except Exception:",
        "        pass",
        "def _mcp_menu_startup():",
        "    try:",
        "        from console.maya_panel import install_menu",
        "        import maya.cmds as _mc",
        '        if not _mc.menu("mcpPipelineMenu", exists=True):',
        "            install_menu()",
        "    except Exception:",
        "        pass",
        "_mcp_utils.executeDeferred(_mcp_open_command_port)",
        "_mcp_utils.executeDeferred(_mcp_menu_startup)",
        _END_SENTINEL,
    ]
    snippet = NL.join(snippet_lines) + NL

    existing = ""
    if os.path.isfile(us_path):
        with open(us_path) as f:
            existing = f.read()

    # Respect an existing HEALTHY block regardless of which writer produced
    # it (install.sh Step 7 "block vN" or this runtime injector): if the
    # block already points at the right repo root and port, leave it alone.
    # Without this, the two writers ping-pong rewrites (different formats,
    # same sentinels) and Maya prints "[MCP] userSetup.py updated" on every
    # fresh server process.
    if _SENTINEL in existing:
        _blk_end = existing.index(_END_SENTINEL) if _END_SENTINEL in existing else len(existing)
        _blk = existing[existing.index(_SENTINEL):_blk_end]
        if ('_mcp_root = r"' + _mcp_root + '"') in _blk and ('localhost:' + str(_mcp_port) + '"') in _blk:
            return False

    if _SENTINEL in existing:
        before = existing[:existing.index(_SENTINEL)]
        if _END_SENTINEL in existing:
            after = existing[existing.index(_END_SENTINEL) + len(_END_SENTINEL):]
            after = after.lstrip(NL)
        else:
            after = ""
        existing = before.rstrip(NL)
        if existing:
            existing += NL + NL
        existing += after

    if existing and not existing.endswith(NL):
        existing += NL + NL
    new_content = existing + snippet

    # Skip write if the file already contains exactly this content.
    # Without this guard, every new claude -p subprocess (which resets
    # _panel_setup_done) would rewrite the file and print the warning —
    # even though nothing changed.
    if os.path.isfile(us_path):
        with open(us_path) as _f:
            if _f.read() == new_content:
                return False

    with open(us_path, "w") as f:
        f.write(new_content)

    cmds.warning("[MCP] userSetup.py updated: " + us_path)
    return True

_inject_user_setup()

# 3. Deferred: install menu + panel after UI is ready
def _mcp_deferred_setup():
    try:
        from console.maya_panel import install_menu, show, PANEL_NAME
        if not cmds.menu("mcpPipelineMenu", exists=True):
            install_menu()
        if not cmds.workspaceControl(PANEL_NAME, exists=True):
            show()
        elif not cmds.workspaceControl(PANEL_NAME, q=True, visible=True):
            show()
    except Exception as exc:
        cmds.warning("[MCP] Panel setup: " + str(exc))

maya.utils.executeDeferred(_mcp_deferred_setup)
result = "panel_setup_ok"
'''
        bridge.execute(setup_code)
        _panel_setup_done = True
    except Exception:
        pass  # Non-critical — don't block tools


def _bg_panel_install():
    """Background thread: poll Maya port, install panel when ready."""
    import time
    import socket as _sock
    for _ in range(24):
        time.sleep(5)
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(2)
            s.connect((MAYA_HOST, MAYA_PORT))
            s.close()
            _setup_maya_panel()
            return
        except Exception:
            continue


def main():
    """Entry point for ``python -m maya_mcp.server`` and pyproject.toml console_scripts."""
    import threading
    threading.Thread(target=_bg_panel_install, daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
