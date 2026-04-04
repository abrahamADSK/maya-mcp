#!/usr/bin/env python3
"""Maya MCP Core Server — MCP server for controlling Autodesk Maya.

Core module: scene operations, objects, transforms, materials, modeling,
animation, I/O, viewport capture, and Vision3D integration.
Communicates with Maya via Command Port (TCP) using maya_bridge.

Features:
    - 24 Maya tools (primitives, transforms, modeling, animation, I/O, rendering)
    - 6 Vision3D tools (image-to-3D, text-to-3D, texturing)
    - 3 RAG tools (search_maya_docs, learn_pattern, session_stats)
    - Dangerous pattern detection (safety.py)
    - Hybrid search: ChromaDB + BM25 + HyDE + RRF fusion
    - Token tracking with RAG savings measurement
    - Model trust gates for self-learning

Usage:
    python server.py                    # stdio transport (MCP standard)
    python server.py --transport http   # HTTP transport (dev/debug)

Environment variables (see .env.example):
    MAYA_HOST          — host where Maya is running (default: localhost)
    MAYA_PORT          — Maya Command Port (default: 7001)
    GPU_API_URL        — GPU API server URL (e.g. http://your-gpu-host:8000)
    GPU_API_KEY        — API key for authentication (empty for open LAN access)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from typing import Optional, List, Any
from enum import Enum
from pathlib import Path

# Ensure this file's directory is on the path so maya_bridge
# can be found regardless of where the server is launched from
sys.path.insert(0, str(Path(__file__).parent))

from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

from maya_bridge import MayaBridge, MayaBridgeError
from safety import check_dangerous

_SERVER_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAYA_HOST = os.environ.get("MAYA_HOST", "localhost")
MAYA_PORT = int(os.environ.get("MAYA_PORT", "7001"))
MAYA_APP  = os.environ.get("MAYA_APP", "Maya")  # macOS app name for `open -a`

# ---------------------------------------------------------------------------
# Token tracking (mirrors fpt-mcp / flame-mcp architecture)
# ---------------------------------------------------------------------------

_FULL_DOC_TOKENS = 14000  # combined size of all indexed docs

_stats = {
    "exec_calls": 0,       # total tool calls
    "tokens_in": 0,        # tokens in parameters
    "tokens_out": 0,       # tokens in responses
    "rag_calls": 0,        # search_maya_docs calls
    "tokens_saved": 0,     # tokens saved by RAG vs loading full doc
    "patterns_learned": 0, # patterns added to docs
    "patterns_staged": 0,  # candidates staged by non-trusted models
    "safety_blocks": 0,    # dangerous pattern detections
    "cache_hits": 0,       # RAG cache hits
}
_stats_reset_at = datetime.datetime.now()

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
# Auto-setup: install MCP Pipeline menu & panel inside Maya on first connect
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).parent.parent)
_panel_installed = False  # track per-session so we only inject once


def _ensure_panel_installed():
    """Inject the MCP Pipeline menu into Maya if not already present.

    Called automatically on first successful maya_ping or on server startup
    (via _startup_panel_install background thread).  Sends Python code
    through the Command Port that:
      1. Adds the maya-mcp repo root to sys.path (so `from console…` works)
      2. Calls install_menu() if the menu doesn't exist yet
      3. Calls show() ONLY if the workspaceControl doesn't exist yet
         (respects user closing the panel — won't force-reopen it)

    Safe for cross-MCP usage: when maya-mcp is started from fpt-mcp or
    flame-mcp consoles, the code still executes inside Maya via TCP.

    Idempotent — guarded by _panel_installed flag + Maya-side exists() checks.
    Uses maya.utils.executeDeferred to ensure UI is ready.
    """
    global _panel_installed
    if _panel_installed:
        return
    try:
        setup_code = f'''
import sys, maya.cmds as cmds, maya.utils

_mcp_root = r"{_PROJECT_ROOT}"
if _mcp_root not in sys.path:
    sys.path.insert(0, _mcp_root)

def _mcp_auto_setup():
    try:
        from console.maya_panel import install_menu, show, PANEL_NAME
        if not cmds.menu("mcpPipelineMenu", exists=True):
            install_menu()
        # Only open panel on first install — respect user if they closed it
        if not cmds.workspaceControl(PANEL_NAME, exists=True):
            show()
    except Exception as exc:
        cmds.warning("[MCP] Auto-setup: " + str(exc))

maya.utils.executeDeferred(_mcp_auto_setup)
result = "panel_setup_queued"
'''
        bridge.execute(setup_code)
        _panel_installed = True
    except Exception:
        pass  # Non-critical — don't block ping


# ---------------------------------------------------------------------------
# Model trust gates (C5 — from fpt-mcp / flame-mcp)
# ---------------------------------------------------------------------------

WRITE_ALLOWED_MODELS = {
    "claude-opus", "claude-sonnet", "claude-sonnet-4",
    "claude-sonnet-4-6", "claude-opus-4-5", "claude-opus-4-6",
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
    model_config = ConfigDict(str_strip_whitespace=True)

    primitive_type: PrimitiveType = Field(..., description="Primitive type: cube, sphere, cylinder, cone, plane, torus")
    name: Optional[str] = Field(default=None, description="Object name (Maya generates one if omitted)")
    position: Optional[List[float]] = Field(default=None, description="Position [x, y, z] in world space", min_length=3, max_length=3)
    scale: Optional[List[float]] = Field(default=None, description="Scale [x, y, z]", min_length=3, max_length=3)
    rotation: Optional[List[float]] = Field(default=None, description="Rotation [x, y, z] in degrees", min_length=3, max_length=3)


class MaterialInput(BaseModel):
    """Parameters for creating and assigning a material."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Name of the object to assign the material to")
    material_name: Optional[str] = Field(default=None, description="Material name (generated if omitted)")
    color: List[float] = Field(..., description="Normalized RGB color [r, g, b] (0.0-1.0)", min_length=3, max_length=3)
    material_type: str = Field(default="lambert", description="Shader type: lambert, blinn, phong, aiStandardSurface")


class TransformInput(BaseModel):
    """Parameters for transforming an object."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Name of the object to transform")
    position: Optional[List[float]] = Field(default=None, description="New position [x, y, z]", min_length=3, max_length=3)
    rotation: Optional[List[float]] = Field(default=None, description="New rotation [x, y, z] in degrees", min_length=3, max_length=3)
    scale: Optional[List[float]] = Field(default=None, description="New scale [x, y, z]", min_length=3, max_length=3)
    relative: bool = Field(default=False, description="If True, transform relative to current position")


class SceneQueryInput(BaseModel):
    """Parameters for querying the scene."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_type: Optional[str] = Field(default=None, description="Filter by type: mesh, light, camera, transform, etc.")
    name_filter: Optional[str] = Field(default=None, description="Filter by name (supports wildcards: *sphere*)")


class ExecutePythonInput(BaseModel):
    """Execute arbitrary Python code in Maya."""
    model_config = ConfigDict(str_strip_whitespace=True)

    code: str = Field(..., description="Python code to execute in Maya. Assign result to variable 'result'.")


class DeleteObjectInput(BaseModel):
    """Parameters for deleting objects."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Name of the object to delete (supports wildcards)")


class LightInput(BaseModel):
    """Parameters for creating a light."""
    model_config = ConfigDict(str_strip_whitespace=True)

    light_type: str = Field(default="directional", description="Type: directional, point, spot, area, ambient")
    name: Optional[str] = Field(default=None, description="Light name")
    intensity: float = Field(default=1.0, description="Light intensity", ge=0.0)
    color: Optional[List[float]] = Field(default=None, description="RGB color [r, g, b] (0.0-1.0)", min_length=3, max_length=3)
    position: Optional[List[float]] = Field(default=None, description="Position [x, y, z]", min_length=3, max_length=3)


class CameraInput(BaseModel):
    """Parameters for creating a camera."""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, description="Camera name")
    position: Optional[List[float]] = Field(default=None, description="Position [x, y, z]", min_length=3, max_length=3)
    look_at: Optional[List[float]] = Field(default=None, description="Look at point [x, y, z]", min_length=3, max_length=3)
    focal_length: float = Field(default=35.0, description="Focal length in mm", ge=1.0, le=500.0)


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


def _handle_error(e: Exception) -> str:
    """Consistent error formatting."""
    if isinstance(e, MayaBridgeError):
        return f"Maya error: {e}"
    return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(name="maya_ping")
async def maya_ping() -> str:
    """Check connection to Maya and return environment info (version, current scene, renderer)."""
    try:
        info = bridge.ping()
        # Auto-install MCP Pipeline menu & panel on first successful connection
        _ensure_panel_installed()
        return json.dumps(info, indent=2, ensure_ascii=False)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_launch")
async def maya_launch() -> str:
    """Open Maya and wait for the Command Port to respond."""
    import socket
    import time

    # 1. Check if already connected
    try:
        info = bridge.ping()
        _ensure_panel_installed()
        return json.dumps({
            "status": "already_running",
            "version": info.get("version", "unknown"),
            "message": "Maya is already open and Command Port is responding."
        }, ensure_ascii=False)
    except Exception:
        pass  # Not running or not responding — open it

    # 2. Launch Maya
    rc, _, err = await _run_cmd(["open", "-a", MAYA_APP], timeout=10)
    if rc != 0:
        return json.dumps({
            "error": f"Could not open Maya ({MAYA_APP}): {err.strip()}",
            "hint": "Verify that Maya is installed. Configure MAYA_APP in .env if the name is different."
        }, ensure_ascii=False)

    # 3. Wait for Command Port to be ready (max 90s)
    max_wait = 90
    poll_interval = 3
    waited = 0

    while waited < max_wait:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((MAYA_HOST, MAYA_PORT))
            sock.close()
            # Port open — try real ping
            try:
                info = bridge.ping()
                _ensure_panel_installed()
                return json.dumps({
                    "status": "launched",
                    "waited_seconds": waited,
                    "version": info.get("version", "unknown"),
                    "message": f"Maya open and Command Port ready ({waited}s)."
                }, ensure_ascii=False)
            except Exception:
                continue  # Port open but Maya still loading
        except (ConnectionRefusedError, socket.timeout, OSError):
            continue  # Port not yet available

    return json.dumps({
        "error": f"Maya opened but Command Port did not respond in {max_wait}s.",
        "hint": "Verify that you have Command Port in userSetup.py: cmds.commandPort(name=':7001', sourceType='mel')"
    }, ensure_ascii=False)


@mcp.tool(name="maya_create_primitive")
async def maya_create_primitive(params: CreatePrimitiveInput) -> str:
    """Create a 3D primitive in Maya (cube, sphere, cylinder, cone, plane, torus) with optional position, scale, and rotation."""
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
        name_arg = f", name='{params.name}'" if params.name else ""

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

        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_assign_material")
async def maya_assign_material(params: MaterialInput) -> str:
    """Create a material (lambert, blinn, phong, aiStandardSurface) with RGB color and assign it to an object."""
    try:
        mat_name = params.material_name or f"{params.object_name}_mat"
        r, g, b = params.color

        code = f"""
import maya.cmds as cmds
mat = cmds.shadingNode('{params.material_type}', asShader=True, name='{mat_name}')
sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='{mat_name}_SG')
cmds.connectAttr(mat + '.outColor', sg + '.surfaceShader')
cmds.setAttr(mat + '.color', {r}, {g}, {b}, type='double3')
cmds.select('{params.object_name}')
cmds.sets(forceElement=sg)
result = {{'material': mat, 'shading_group': sg, 'assigned_to': '{params.object_name}'}}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_transform")
async def maya_transform(params: TransformInput) -> str:
    """Move, rotate, or scale an object in the Maya scene."""
    try:
        ws = "False" if params.relative else "True"
        rel = "True" if params.relative else "False"

        code = f"import maya.cmds as cmds\n"
        if params.position:
            code += f"cmds.xform('{params.object_name}', translation={params.position}, worldSpace={ws}, relative={rel})\n"
        if params.rotation:
            code += f"cmds.xform('{params.object_name}', rotation={params.rotation}, worldSpace={ws}, relative={rel})\n"
        if params.scale:
            code += f"cmds.xform('{params.object_name}', scale={params.scale}, relative={rel})\n"

        code += f"""
pos = cmds.xform('{params.object_name}', q=True, translation=True, worldSpace=True)
rot = cmds.xform('{params.object_name}', q=True, rotation=True, worldSpace=True)
scl = cmds.xform('{params.object_name}', q=True, scale=True)
result = {{'object': '{params.object_name}', 'position': pos, 'rotation': rot, 'scale': scl}}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_list_scene")
async def maya_list_scene(params: SceneQueryInput) -> str:
    """List objects in the Maya scene, with optional filters by type or name."""
    try:
        filters = []
        if params.object_type:
            filters.append(f"type='{params.object_type}'")
        if params.name_filter:
            filters.append(f"'{params.name_filter}'")

        filter_str = ", ".join(filters)

        code = f"""
import maya.cmds as cmds
import json
objects = cmds.ls({filter_str}) or []
result = {{'count': len(objects), 'objects': objects}}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_delete")
async def maya_delete(params: DeleteObjectInput) -> str:
    """Delete an object from the Maya scene by name (supports wildcards like *sphere*)."""
    _stats["exec_calls"] += 1

    # Safety check
    warning = check_dangerous(f'cmds.delete("{params.object_name}")')
    if warning:
        _stats["safety_blocks"] += 1
        return json.dumps({"safety_warning": warning})

    try:
        code = f"""
import maya.cmds as cmds
targets = cmds.ls('{params.object_name}')
if targets:
    cmds.delete(targets)
    result = {{'deleted': targets}}
else:
    result = {{'error': 'Not found: {params.object_name}'}}
"""
        response = bridge.execute(code)
        _stats["tokens_out"] += _tok(response)
        return response
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_create_light")
async def maya_create_light(params: LightInput) -> str:
    """Create a light in Maya (directional, point, spot, area, ambient) with configurable intensity and color."""
    try:
        light_funcs = {
            "directional": "cmds.directionalLight",
            "point": "cmds.pointLight",
            "spot": "cmds.spotLight",
            "area": "cmds.shadingNode('areaLight', asLight=True",
            "ambient": "cmds.ambientLight",
        }

        name_arg = f", name='{params.name}'" if params.name else ""

        if params.light_type == "area":
            code = f"""
import maya.cmds as cmds
light = cmds.shadingNode('areaLight', asLight=True{name_arg})
"""
        else:
            func = light_funcs.get(params.light_type, "cmds.directionalLight")
            code = f"""
import maya.cmds as cmds
light = {func}({name_arg})
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

        code += "result = {'light': light, 'type': '" + params.light_type + "'}\n"

        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_create_camera")
async def maya_create_camera(params: CameraInput) -> str:
    """Create a camera in Maya with configurable position, look-at point, and focal length."""
    try:
        name_arg = f", name='{params.name}'" if params.name else ""
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
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_execute_python")
async def maya_execute_python(params: ExecutePythonInput) -> str:
    """Execute arbitrary Python code in Maya. Code must assign its result to a 'result' variable. Useful for advanced operations not covered by other tools."""
    _stats["exec_calls"] += 1
    _stats["tokens_in"] += _tok(params.code)

    # Safety check on code
    warning = check_dangerous(params.code)
    if warning:
        _stats["safety_blocks"] += 1
        return json.dumps({"safety_warning": warning})

    try:
        response = bridge.execute(params.code)
        _stats["tokens_out"] += _tok(response)
        return response
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_new_scene")
async def maya_new_scene() -> str:
    """Create a new empty scene in Maya (discards current scene without saving)."""
    try:
        code = """
import maya.cmds as cmds
cmds.file(new=True, force=True)
result = {'status': 'new_scene_created'}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_save_scene")
async def maya_save_scene() -> str:
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
        return bridge.execute(code)
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
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Name of the mesh object")
    operation: MeshOperationType = Field(..., description="Type of operation")
    second_object: Optional[str] = Field(default=None, description="Second object (required for boolean and combine)")
    faces: Optional[str] = Field(default=None, description="Face components (e.g., 'pCube1.f[0:3]') for extrude/bevel")
    offset: float = Field(default=0.2, description="Offset/distance for extrude or bevel", ge=0.0)
    divisions: int = Field(default=1, description="Divisions for smooth or segments for bevel", ge=1, le=10)


class KeyframeInput(BaseModel):
    """Parameters for creating animation keyframes."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Name of the object to animate")
    attribute: str = Field(default="translateX", description="Attribute to animate (translateX/Y/Z, rotateX/Y/Z, scaleX/Y/Z, visibility)")
    value: float = Field(..., description="Keyframe value")
    frame: float = Field(..., description="Frame to insert the keyframe on")
    in_tangent: str = Field(default="auto", description="In-tangent: auto, linear, flat, spline, step")
    out_tangent: str = Field(default="auto", description="Out-tangent: auto, linear, flat, spline, step")


class ImportFileInput(BaseModel):
    """Parameters for importing 3D files."""
    model_config = ConfigDict(str_strip_whitespace=True)

    file_path: str = Field(..., description="Absolute path to file to import (.obj, .fbx, .glb, .abc, .ma, .mb)")
    namespace: Optional[str] = Field(default=None, description="Namespace to avoid name collisions")
    group_under: Optional[str] = Field(default=None, description="Parent group name (created if it doesn't exist)")
    scale_factor: Optional[float] = Field(default=None, description="Scale factor on import (e.g., 0.01 for cm to m)")


class ViewportCaptureInput(BaseModel):
    """Parameters for capturing the Maya viewport."""
    model_config = ConfigDict(str_strip_whitespace=True)

    output_path: str = Field(default="/tmp/maya_viewport.png", description="Output path for image (.png/.jpg)")
    width: int = Field(default=1920, description="Capture width in pixels", ge=100, le=8192)
    height: int = Field(default=1080, description="Capture height in pixels", ge=100, le=8192)
    camera: Optional[str] = Field(default=None, description="Camera to use (default: active panel)")
    frame: Optional[float] = Field(default=None, description="Frame to capture (default: current frame)")


class ShelfButtonInput(BaseModel):
    """Parameters for creating a button on the Maya shelf."""
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(..., description="Button label (short text)")
    command: str = Field(..., description="Python code that executes when button is clicked")
    tooltip: str = Field(default="", description="Help text on mouseover")
    shelf_name: str = Field(default="Custom", description="Name of the shelf to create the button in")
    icon_label: str = Field(default="MCP", description="Text overlaid on icon (max 4 chars)")


# ─────────────────────────────────────────────
# New Tools (P2-P6, A-E)
# ─────────────────────────────────────────────


@mcp.tool(name="maya_mesh_operation")
async def maya_mesh_operation(params: MeshOperationInput) -> str:
    """Execute mesh operations: extrude, bevel, boolean (union/difference/intersection), combine, separate, smooth."""
    try:
        op = params.operation.value

        if op == "extrude":
            faces = params.faces or f"{params.object_name}.f[:]"
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_extrude')
try:
    result_faces = cmds.polyExtrudeFacet('{faces}', localTranslateZ={params.offset}, divisions={params.divisions})
    result = {{'operation': 'extrude', 'faces': '{faces}', 'offset': {params.offset}, 'result': str(result_faces)}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "bevel":
            faces = params.faces or f"{params.object_name}.e[:]"
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_bevel')
try:
    result_edges = cmds.polyBevel3('{faces}', offset={params.offset}, segments={params.divisions})
    result = {{'operation': 'bevel', 'target': '{faces}', 'offset': {params.offset}, 'result': str(result_edges)}}
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
    result_node = cmds.polyCBoolOp('{params.object_name}', '{params.second_object}', op={bool_op}, ch=False)
    result = {{'operation': '{op}', 'objects': ['{params.object_name}', '{params.second_object}'], 'result': str(result_node[0])}}
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
    combined = cmds.polyUnite('{params.object_name}', '{params.second_object}', ch=False)
    result = {{'operation': 'combine', 'result': str(combined[0])}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "separate":
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_separate')
try:
    separated = cmds.polySeparate('{params.object_name}', ch=False)
    result = {{'operation': 'separate', 'parts': [str(s) for s in separated]}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "smooth":
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_smooth')
try:
    cmds.polySmooth('{params.object_name}', divisions={params.divisions})
    verts = cmds.polyEvaluate('{params.object_name}', vertex=True)
    faces = cmds.polyEvaluate('{params.object_name}', face=True)
    result = {{'operation': 'smooth', 'object': '{params.object_name}', 'divisions': {params.divisions}, 'vertices': verts, 'faces': faces}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        else:
            return json.dumps({"error": f"Unknown operation: {op}"})

        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_set_keyframe")
async def maya_set_keyframe(params: KeyframeInput) -> str:
    """Create an animation keyframe on an object. Allows animating translate, rotate, scale, and visibility per frame."""
    try:
        code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_keyframe')
try:
    cmds.setKeyframe('{params.object_name}', attribute='{params.attribute}',
                     value={params.value}, time={params.frame},
                     inTangentType='{params.in_tangent}', outTangentType='{params.out_tangent}')
    result = {{'object': '{params.object_name}', 'attribute': '{params.attribute}',
              'value': {params.value}, 'frame': {params.frame}}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_import_file")
async def maya_import_file(params: ImportFileInput) -> str:
    """Import 3D files into Maya: OBJ, FBX, GLB/GLTF, Alembic ABC, Maya MA/MB. With namespace, parent group, and scale options."""
    try:
        ext = params.file_path.rsplit(".", 1)[-1].lower() if "." in params.file_path else ""
        ns_opt = f", namespace='{params.namespace}'" if params.namespace else ""
        group_code = ""
        if params.group_under:
            group_code = f"""
if not cmds.objExists('{params.group_under}'):
    cmds.group(empty=True, name='{params.group_under}')
"""
        scale_code = ""
        if params.scale_factor:
            scale_code = f"""
for _mcp_obj in _mcp_imported:
    if cmds.objectType(_mcp_obj) == 'transform':
        cmds.scale({params.scale_factor}, {params.scale_factor}, {params.scale_factor}, _mcp_obj)
"""
        # Build file type string
        file_types = {
            "obj": "OBJ", "fbx": "FBX", "abc": "Alembic",
            "glb": "glTF", "gltf": "glTF", "ma": "mayaAscii", "mb": "mayaBinary",
        }
        ftype = file_types.get(ext, "")
        type_opt = f", type='{ftype}'" if ftype else ""

        code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_import')
try:
    _mcp_before = set(cmds.ls(transforms=True))
    {group_code}
    cmds.file('{params.file_path}', i=True, ignoreVersion=True,
              mergeNamespacesOnClash=False, returnNewNodes=True{ns_opt}{type_opt})
    _mcp_after = set(cmds.ls(transforms=True))
    _mcp_imported = list(_mcp_after - _mcp_before)
    {scale_code}
    result = {{'imported': len(_mcp_imported), 'objects': _mcp_imported[:20],
              'file': '{params.file_path}'}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_viewport_capture")
async def maya_viewport_capture(params: ViewportCaptureInput) -> str:
    """Capture the Maya viewport as PNG/JPG image. Does not do Arnold render — it is an instant viewport grab (<1s). Useful for visually verifying scene state."""
    try:
        camera_opt = ""
        if params.camera:
            camera_opt = f"""
# Set camera for the active panel
_mcp_panel = cmds.getPanel(withFocus=True)
if cmds.getPanel(typeOf=_mcp_panel) == 'modelPanel':
    cmds.modelPanel(_mcp_panel, edit=True, camera='{params.camera}')
"""
        frame_opt = f", frame=[{params.frame}]" if params.frame is not None else ""
        fmt = "png" if params.output_path.endswith(".png") else "jpg"

        code = f"""
import maya.cmds as cmds
import os
cmds.undoInfo(stateWithoutFlush=False)
try:
    {camera_opt}
    _mcp_img = cmds.playblast(
        completeFilename='{params.output_path}',
        format='image', compression='{fmt}',
        width={params.width}, height={params.height},
        showOrnaments=False, viewer=False,
        offScreen=True, percent=100{frame_opt}
    )
    _mcp_size = os.path.getsize('{params.output_path}') // 1024
    result = {{'captured': '{params.output_path}', 'size_kb': _mcp_size,
              'resolution': '{params.width}x{params.height}'}}
finally:
    cmds.undoInfo(stateWithoutFlush=True)
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_scene_snapshot")
async def maya_scene_snapshot() -> str:
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


@mcp.tool(name="maya_shelf_button")
async def maya_shelf_button(params: ShelfButtonInput) -> str:
    """Create a custom button on the Maya shelf with associated Python code. Allows Claude to leave reusable tools in the interface."""
    try:
        # Escape the command for embedding in Python string
        safe_command = params.command.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        code = f"""
import maya.cmds as cmds
import maya.mel as mel

# Create or find the shelf
_mcp_shelf = '{params.shelf_name}'
if not cmds.shelfLayout(_mcp_shelf, exists=True):
    mel.eval('addNewShelfTab "{params.shelf_name}"')

_mcp_btn = cmds.shelfButton(
    parent=_mcp_shelf,
    label='{params.label}',
    annotation='{params.tooltip}',
    imageOverlayLabel='{params.icon_label[:4]}',
    image='pythonFamily.png',
    command='{safe_command}',
    sourceType='python'
)
result = {{'button': _mcp_btn, 'shelf': _mcp_shelf, 'label': '{params.label}'}}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


# ─────────────────────────────────────────────
# Remote GPU — Vision3D REST API (Hunyuan3D-2)
# ─────────────────────────────────────────────

from mcp.server.fastmcp import Context

# Configuration via environment variables
_GPU_API_URL  = os.environ.get("GPU_API_URL",  "http://localhost:8000").rstrip("/")
_GPU_API_KEY  = os.environ.get("GPU_API_KEY",  "")
_GPU_VERIFY   = os.environ.get("GPU_VERIFY_TLS", "false").lower() in ("true", "1", "yes")
_MAC_BASE_DIR = os.environ.get("MAYA_BASE_DIR",
                                str(Path(__file__).parent.parent))           # project root on Mac

# Lazy httpx client
_http_client = None

# Track log cursors per job (for incremental log delivery)
_job_log_cursors: dict[str, int] = {}


def _get_http_client():
    """Return a reusable httpx client for GPU API calls."""
    global _http_client
    if _http_client is None:
        import httpx
        headers = {}
        if _GPU_API_KEY:
            headers["x-api-key"] = _GPU_API_KEY
        _http_client = httpx.AsyncClient(
            base_url=_GPU_API_URL,
            headers=headers,
            verify=_GPU_VERIFY,
            timeout=httpx.Timeout(connect=10, read=900, write=60, pool=10),
        )
    return _http_client


async def _download_file(job_id: str, filename: str, dest: Path) -> bool:
    """Download a single file from a completed job."""
    client = _get_http_client()
    resp = await client.get(f"/api/jobs/{job_id}/files/{filename}")
    if resp.status_code == 200:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    return False


def _build_quality_form_data(params) -> dict:
    """Build form_data dict with quality params from a ShapeGenerateInput or ShapeTextInput."""
    form_data = {}
    if hasattr(params, "target_faces") and params.target_faces > 0:
        form_data["target_faces"] = str(params.target_faces)
    elif hasattr(params, "target_faces"):
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
    model_config = ConfigDict(str_strip_whitespace=True)

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
    model_config = ConfigDict(str_strip_whitespace=True)

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
    model_config = ConfigDict(str_strip_whitespace=True)

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
    model_config = ConfigDict(str_strip_whitespace=True)

    job_id: str = Field(..., description="Job ID returned by shape_generate_remote/text/texture.")


class Vision3DDownloadInput(BaseModel):
    """Parameters for downloading results from a completed job."""
    model_config = ConfigDict(str_strip_whitespace=True)

    job_id: str = Field(..., description="Completed job ID.")
    output_subdir: str = Field(..., description="Local output subdirectory (same as used when creating the job).")
    files: List[str] = Field(
        default_factory=lambda: ["textured.glb", "mesh_uv.obj", "texture_baked.png", "mesh.glb"],
        description="List of files to download. By default downloads all from the complete pipeline."
    )


# ── Tools: check Vision3D availability ─────────────────────────


@mcp.tool(name="vision3d_health")
async def vision3d_health(ctx: Context) -> str:
    """Check if Vision3D server is available and responding.

    Returns GPU information, available models, and text-to-3D status.
    Call this tool BEFORE offering AI generation options to the user,
    to know if Vision3D is running and accessible.
    """
    try:
        client = _get_http_client()
        await ctx.info("Checking Vision3D availability...")
        resp = await client.get("/api/health", timeout=5.0)

        if resp.status_code != 200:
            return json.dumps({
                "available": False,
                "error": f"Vision3D responded with HTTP {resp.status_code}",
                "url": _GPU_API_URL,
            })

        health = resp.json()
        return json.dumps({
            "available": True,
            "url": _GPU_API_URL,
            "gpu": health.get("gpu", "unknown"),
            "vram_gb": health.get("vram_gb"),
            "models": health.get("models", []),
            "text_to_3d": health.get("text_to_3d", "unknown"),
        }, indent=2)

    except Exception as e:
        return json.dumps({
            "available": False,
            "error": f"Could not connect to Vision3D ({_GPU_API_URL}): {e}",
            "hint": "Verify that Vision3D server is running and accessible from this network.",
        })


# ── Tools: start jobs (non-blocking) ───────────────────────────────────


@mcp.tool(name="shape_generate_remote")
async def shape_generate_remote(params: ShapeGenerateInput, ctx: Context) -> str:
    """Start textured 3D generation from image in Vision3D (non-blocking).

    Uploads the image and starts the complete pipeline (shape + decimation + texturing).
    Returns a job_id immediately. Use vision3d_poll to follow progress
    and vision3d_download to download results when finished.
    """
    try:
        image_local = Path(params.image_path)
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir

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

        client = _get_http_client()
        quality_desc = params.preset or f"model={params.model or 'turbo'}"

        await ctx.info(f"Uploading image to Vision3D ({quality_desc})...")

        form_data = {"output_subdir": params.output_subdir}
        form_data.update(_build_quality_form_data(params))

        with open(str(image_local), "rb") as f:
            resp = await client.post(
                "/api/generate-full",
                files={"image": (image_local.name, f, "image/png")},
                data=form_data,
            )

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verify Vision3D is running: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Job started: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": params.output_subdir,
            "output_dir": str(out_dir),
            "quality": quality_desc,
            "image_copy": str(input_copy),
            "next_step": f"Call vision3d_poll(job_id='{job_id}') to see progress. "
                         f"When status is 'completed', call vision3d_download(job_id='{job_id}', "
                         f"output_subdir='{params.output_subdir}').",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(name="shape_generate_text")
async def shape_generate_text(params: ShapeTextInput, ctx: Context) -> str:
    """Start 3D generation from text in Vision3D (non-blocking).

    Sends the prompt and starts the text-to-3D pipeline.
    Returns job_id. Use vision3d_poll to follow progress.
    """
    try:
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)

        client = _get_http_client()
        quality_desc = params.preset or f"model={params.model or 'turbo'}"

        await ctx.info(f"Sending prompt to Vision3D: '{params.text_prompt}' ({quality_desc})...")

        form_data = {
            "text_prompt": params.text_prompt,
            "output_subdir": params.output_subdir,
        }
        form_data.update(_build_quality_form_data(params))

        resp = await client.post("/api/generate-text", data=form_data)

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verify Vision3D is running: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Job started: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": params.output_subdir,
            "output_dir": str(out_dir),
            "quality": quality_desc,
            "next_step": f"Call vision3d_poll(job_id='{job_id}') to follow progress.",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(name="texture_mesh_remote")
async def texture_mesh_remote(params: TextureRemoteInput, ctx: Context) -> str:
    """Start mesh texturing in Vision3D (non-blocking).

    Uploads mesh + image and starts the texturing pipeline.
    Returns job_id. Use vision3d_poll to follow progress.
    """
    try:
        out_dir     = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir
        mesh_local  = out_dir / params.mesh_filename
        image_local = out_dir / params.image_filename

        if not mesh_local.exists():
            return json.dumps({
                "error": f"Mesh not found: {mesh_local}",
                "hint":  "Generate the mesh first with shape_generate_remote."
            })
        if not image_local.exists():
            return json.dumps({
                "error":  f"Image not found: {image_local}",
                "hint":   f"Copy the image as '{params.image_filename}' in {out_dir}"
            })

        client = _get_http_client()

        await ctx.info(f"Uploading {params.mesh_filename} + {params.image_filename} to Vision3D...")

        with open(str(mesh_local), "rb") as mf, open(str(image_local), "rb") as imf:
            resp = await client.post(
                "/api/texture-mesh",
                files={
                    "mesh": (params.mesh_filename, mf, "application/octet-stream"),
                    "image": (params.image_filename, imf, "image/png"),
                },
                data={"output_subdir": params.output_subdir},
            )

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Check Vision3D: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Texturing job started: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": params.output_subdir,
            "next_step": f"Call vision3d_poll(job_id='{job_id}') to follow progress.",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tools: poll progress and download ──────────────────────────────────


@mcp.tool(name="vision3d_poll")
async def vision3d_poll(params: Vision3DPollInput, ctx: Context) -> str:
    """Poll job status in Vision3D. Returns new log lines since last call (incremental progress).

    Call this tool repeatedly while status is 'running'.
    When status is 'completed', call vision3d_download.
    When status is 'failed', show the error to the user.
    """
    try:
        client = _get_http_client()
        resp = await client.get(f"/api/jobs/{params.job_id}")

        if resp.status_code == 404:
            return json.dumps({"error": f"Job '{params.job_id}' not found in Vision3D."})

        resp.raise_for_status()
        job = resp.json()

        # Deliver only new log lines since last poll
        cursor = _job_log_cursors.get(params.job_id, 0)
        all_log = job.get("log", [])
        new_lines = all_log[cursor:]
        _job_log_cursors[params.job_id] = len(all_log)

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
                f"Job completed in {elapsed}s. Call vision3d_download("
                f"job_id='{params.job_id}', output_subdir='...') to download files."
            )
            # Cleanup cursor
            _job_log_cursors.pop(params.job_id, None)
        elif status == "failed":
            result["error"] = job.get("error", "Unknown error")
            _job_log_cursors.pop(params.job_id, None)
        else:
            result["next_step"] = (
                f"Job in progress ({elapsed}s). Call "
                f"vision3d_poll(job_id='{params.job_id}') again to update."
            )

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(name="vision3d_download")
async def vision3d_download(params: Vision3DDownloadInput, ctx: Context) -> str:
    """Download files from a completed Vision3D job to local directory.

    Call this tool after vision3d_poll reports status='completed'.
    Downloads specified files to local output subdirectory.
    """
    try:
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)

        await ctx.info(f"Downloading {len(params.files)} files from Vision3D...")

        downloaded = []
        failed = []

        for fname in params.files:
            ok = await _download_file(params.job_id, fname, out_dir / fname)
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
                "Files downloaded. Import textured.glb in Maya with maya_execute_python, "
                "or use mesh_uv.obj + texture_baked.png for full UV control."
                if textured_ready else
                "Partial download. Check 'failed' to see which files failed."
            ),
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# RAG Tools (mirrors fpt-mcp architecture)
# ---------------------------------------------------------------------------

class SearchMayaDocsInput(BaseModel):
    """Parameters for searching Maya API documentation."""
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

    try:
        from rag.search import search
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

    Model trust gates: only Sonnet/Opus can write directly.
    Other models stage candidates for review.
    """
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
                from rag.search import clear_cache
                clear_cache()
            except ImportError:
                pass

            return json.dumps({
                "status": "learned",
                "description": params.description,
                "file": doc_file,
                "note": "Pattern appended to docs. Run build_index to include in RAG.",
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
    used = _stats["tokens_in"] + _stats["tokens_out"]
    saved = _stats["tokens_saved"]
    total = used + saved
    ratio = f"{saved / total * 100:.0f}%" if total > 0 else "—"
    uptime = str(datetime.datetime.now() - _stats_reset_at).split(".")[0]

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
        "full_doc_baseline": _FULL_DOC_TOKENS,
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _startup_panel_install():
    """Background thread: wait for Maya Command Port, then install panel.

    Runs once when server.py starts.  Retries every 5s for up to 120s.
    If Maya isn't running or Command Port isn't open, gives up silently.
    """
    import time
    import socket as _sock

    for _ in range(24):  # 24 × 5s = 120s max
        time.sleep(5)
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(2)
            s.connect((MAYA_HOST, MAYA_PORT))
            s.close()
            _ensure_panel_installed()
            return
        except Exception:
            continue


if __name__ == "__main__":
    import threading
    t = threading.Thread(target=_startup_panel_install, daemon=True)
    t.start()
    mcp.run()
