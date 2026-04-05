"""
test_import_file.py
===================
Tests for the maya_import_file tool in core/server.py.

Monkeypatches bridge.execute to capture the Python code sent to Maya,
then asserts the code contains the correct import commands for each format,
namespace, scale, and error handling.

No Maya instance, MCP SDK, or network access required.

Test cases (from TESTING_PLAN section 4.5):
  1. Import GLB sends correct command to bridge
  2. Import OBJ sends correct command
  3. Import FBX sends correct command
  4. Import with namespace applies namespace correctly
  5. Import with scale applies scale factor
  6. Import of non-existent file returns error without crash
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── Path setup ────────────────────────────────────────────────────────────
_CORE_DIR = Path(__file__).resolve().parent.parent / "core"
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))


# ── Stub mcp SDK ─────────────────────────────────────────────────────────
# Same approach as test_vision3d.py: lightweight stubs so server.py imports
# succeed without the full MCP SDK installed.

_mcp_pkg = types.ModuleType("mcp")
_mcp_server = types.ModuleType("mcp.server")
_mcp_fastmcp = types.ModuleType("mcp.server.fastmcp")


class _StubContext:
    pass


class _StubImage:
    pass


class _StubFastMCP:
    def __init__(self, *a, **kw):
        pass

    def tool(self, **kw):
        def decorator(fn):
            return fn
        return decorator


_mcp_fastmcp.FastMCP = _StubFastMCP
_mcp_fastmcp.Context = _StubContext
_mcp_fastmcp.Image = _StubImage

_mcp_pkg.server = _mcp_server
_mcp_server.fastmcp = _mcp_fastmcp

sys.modules["mcp"] = _mcp_pkg
sys.modules["mcp.server"] = _mcp_server
sys.modules["mcp.server.fastmcp"] = _mcp_fastmcp

# ── Stub maya_bridge ──────────────────────────────────────────────────────
_maya_bridge = types.ModuleType("maya_bridge")


class _StubMayaBridge:
    def __init__(self, *a, **kw):
        pass

    def execute(self, code: str) -> str:
        """Default stub — tests override via monkeypatch."""
        return json.dumps({"imported": 1, "objects": ["imported_obj"], "file": "test"})


class _StubMayaBridgeError(Exception):
    pass


_maya_bridge.MayaBridge = _StubMayaBridge
_maya_bridge.MayaBridgeError = _StubMayaBridgeError
sys.modules["maya_bridge"] = _maya_bridge

# ── Stub safety module ───────────────────────────────────────────────────
_safety = types.ModuleType("safety")
_safety.check_dangerous = lambda code: None
sys.modules["safety"] = _safety

# ── Import server ────────────────────────────────────────────────────────
import server as srv  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────

def _capture_bridge_execute(monkeypatch):
    """Monkeypatch bridge.execute to capture the code string sent to Maya.

    Returns a dict with a 'code' key that will be populated when
    bridge.execute is called.
    """
    captured = {"code": None}

    def fake_execute(code: str) -> str:
        captured["code"] = code
        return json.dumps({
            "imported": 2,
            "objects": ["imported_transform", "imported_shape"],
            "file": "test.glb",
        })

    monkeypatch.setattr(srv.bridge, "execute", fake_execute)
    return captured


# ── 1. Import GLB ────────────────────────────────────────────────────────

class TestImportGLB:
    """Import GLB sends the correct command to bridge."""

    @pytest.mark.asyncio
    async def test_glb_import_command(self, monkeypatch):
        """GLB import uses type='glTF' and i=True in cmds.file()."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/model.glb")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert code is not None
        assert "cmds.file(" in code
        assert "/assets/model.glb" in code
        assert "i=True" in code
        assert "type='glTF'" in code

    @pytest.mark.asyncio
    async def test_gltf_import_command(self, monkeypatch):
        """GLTF extension also maps to type='glTF'."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/scene.gltf")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "type='glTF'" in code


# ── 2. Import OBJ ────────────────────────────────────────────────────────

class TestImportOBJ:
    """Import OBJ sends the correct command."""

    @pytest.mark.asyncio
    async def test_obj_import_command(self, monkeypatch):
        """OBJ import uses type='OBJ'."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/mesh.obj")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "type='OBJ'" in code
        assert "/assets/mesh.obj" in code
        assert "i=True" in code


# ── 3. Import FBX ────────────────────────────────────────────────────────

class TestImportFBX:
    """Import FBX sends the correct command."""

    @pytest.mark.asyncio
    async def test_fbx_import_command(self, monkeypatch):
        """FBX import uses type='FBX'."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/character.fbx")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "type='FBX'" in code
        assert "/assets/character.fbx" in code

    @pytest.mark.asyncio
    async def test_alembic_import_command(self, monkeypatch):
        """Alembic (.abc) import uses type='Alembic'."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/cache/sim.abc")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "type='Alembic'" in code

    @pytest.mark.asyncio
    async def test_ma_import_command(self, monkeypatch):
        """Maya ASCII (.ma) import uses type='mayaAscii'."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/scenes/layout.ma")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "type='mayaAscii'" in code

    @pytest.mark.asyncio
    async def test_mb_import_command(self, monkeypatch):
        """Maya Binary (.mb) import uses type='mayaBinary'."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/scenes/rig.mb")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "type='mayaBinary'" in code


# ── 4. Import with namespace ─────────────────────────────────────────────

class TestImportNamespace:
    """Import with namespace applies the namespace correctly."""

    @pytest.mark.asyncio
    async def test_namespace_in_command(self, monkeypatch):
        """When namespace is provided, cmds.file() includes namespace= argument."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(
            file_path="/assets/prop.glb",
            namespace="hero_prop",
        )
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "namespace='hero_prop'" in code

    @pytest.mark.asyncio
    async def test_no_namespace_by_default(self, monkeypatch):
        """When namespace is None, cmds.file() does NOT include namespace=."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/prop.glb")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "namespace=" not in code


# ── 5. Import with scale ─────────────────────────────────────────────────

class TestImportScale:
    """Import with scale applies the scale factor."""

    @pytest.mark.asyncio
    async def test_scale_factor_in_command(self, monkeypatch):
        """When scale_factor is provided, code includes cmds.scale()."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(
            file_path="/assets/model.fbx",
            scale_factor=0.01,
        )
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "cmds.scale(0.01, 0.01, 0.01" in code

    @pytest.mark.asyncio
    async def test_no_scale_by_default(self, monkeypatch):
        """When scale_factor is None, code does NOT include cmds.scale()."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/model.fbx")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "cmds.scale(" not in code

    @pytest.mark.asyncio
    async def test_scale_applies_to_transforms_only(self, monkeypatch):
        """Scale code checks objectType == 'transform' before scaling."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(
            file_path="/assets/model.obj",
            scale_factor=2.5,
        )
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "objectType" in code
        assert "'transform'" in code
        assert "cmds.scale(2.5, 2.5, 2.5" in code


# ── 6. Import error handling ─────────────────────────────────────────────

class TestImportErrors:
    """Import of non-existent file returns error without crash."""

    @pytest.mark.asyncio
    async def test_bridge_error_returns_message(self, monkeypatch):
        """MayaBridgeError from bridge.execute is caught and formatted."""

        def raise_bridge_error(code: str):
            raise _StubMayaBridgeError("file not found: /nonexistent.glb")

        monkeypatch.setattr(srv.bridge, "execute", raise_bridge_error)

        params = srv.ImportFileInput(file_path="/nonexistent.glb")
        result = await srv.maya_import_file(params)

        assert "Maya error" in result
        assert "file not found" in result

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_message(self, monkeypatch):
        """Unexpected exceptions are caught and formatted without crash."""

        def raise_runtime_error(code: str):
            raise RuntimeError("Unexpected failure in Maya")

        monkeypatch.setattr(srv.bridge, "execute", raise_runtime_error)

        params = srv.ImportFileInput(file_path="/some/file.obj")
        result = await srv.maya_import_file(params)

        assert "Unexpected error" in result
        assert "RuntimeError" in result

    @pytest.mark.asyncio
    async def test_group_under_creates_group(self, monkeypatch):
        """When group_under is provided, code checks/creates the parent group."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(
            file_path="/assets/model.glb",
            group_under="env_group",
        )
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "cmds.objExists('env_group')" in code
        assert "cmds.group(empty=True, name='env_group')" in code


# ── Additional: undo chunk and returnNewNodes ────────────────────────────

class TestImportStructure:
    """Verify structural elements of the generated import code."""

    @pytest.mark.asyncio
    async def test_undo_chunk_wraps_import(self, monkeypatch):
        """Import code is wrapped in undoInfo openChunk/closeChunk."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/model.obj")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "cmds.undoInfo(openChunk=True" in code
        assert "cmds.undoInfo(closeChunk=True)" in code

    @pytest.mark.asyncio
    async def test_return_new_nodes_flag(self, monkeypatch):
        """Import uses returnNewNodes=True to track what was imported."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/model.glb")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "returnNewNodes=True" in code

    @pytest.mark.asyncio
    async def test_before_after_diff(self, monkeypatch):
        """Import code compares transforms before/after to detect new objects."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/model.glb")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "_mcp_before" in code
        assert "_mcp_after" in code
        assert "_mcp_imported" in code

    @pytest.mark.asyncio
    async def test_unknown_extension_no_type(self, monkeypatch):
        """Unknown file extension results in no type= argument."""
        captured = _capture_bridge_execute(monkeypatch)

        params = srv.ImportFileInput(file_path="/assets/model.xyz")
        await srv.maya_import_file(params)

        code = captured["code"]
        assert "type=" not in code
        assert "/assets/model.xyz" in code
