"""
test_reference.py
=================
Tests for the maya_reference tool in src/maya_mcp/server.py.

Monkeypatches bridge.execute to capture the Python code sent to Maya, then
asserts each lifecycle operation emits the right cmds.file/referenceQuery call,
that operand validation fires before the bridge is touched, and that reference
REMOVAL is not reachable through this tool.

No Maya instance, MCP SDK, or network access required.
"""

import json

import pytest

from maya_mcp import server as srv


def _capture_bridge_execute(monkeypatch):
    """Capture the code string sent to Maya; return a dict populated on call."""
    captured = {"code": None, "timeout": None, "calls": 0}

    def fake_execute(code: str, timeout=None) -> str:
        captured["code"] = code
        captured["timeout"] = timeout
        captured["calls"] += 1
        return json.dumps({"ok": True})

    monkeypatch.setattr(srv.bridge, "execute", fake_execute)
    return captured


class TestList:
    """operation='list' enumerates reference nodes with provenance."""

    @pytest.mark.asyncio
    async def test_list_queries_reference_nodes(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(operation="list"))

        code = captured["code"]
        assert "cmds.ls(type='reference')" in code
        assert "cmds.referenceQuery(" in code
        assert "filename=True" in code
        assert "isLoaded=True" in code

    @pytest.mark.asyncio
    async def test_list_skips_shared_reference_node(self, monkeypatch):
        """sharedReferenceNode is Maya bookkeeping, not a user reference."""
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(operation="list"))

        assert "sharedReferenceNode" in captured["code"]

    @pytest.mark.asyncio
    async def test_list_needs_no_other_field(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        out = await srv.maya_reference(srv.ReferenceInput(operation="list"))

        assert "error" not in json.loads(out)
        assert captured["calls"] == 1


class TestCreate:
    """operation='create' references a file in, namespaced."""

    @pytest.mark.asyncio
    async def test_create_uses_reference_flag_not_import(self, monkeypatch):
        """The whole point: reference=True keeps the link that i=True breaks."""
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="create", file_path="/pub/char/hero_v003.ma"))

        code = captured["code"]
        assert "reference=True" in code
        assert "i=True" not in code
        assert "/pub/char/hero_v003.ma" in code

    @pytest.mark.asyncio
    async def test_create_defaults_namespace_to_basename(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="create", file_path="/pub/char/hero_v003.ma"))

        assert "'hero_v003'" in captured["code"]

    @pytest.mark.asyncio
    async def test_create_honours_explicit_namespace(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="create", file_path="/pub/char/hero_v003.ma",
            namespace="hero"))

        code = captured["code"]
        assert "namespace='hero'" in code
        assert "mergeNamespacesOnClash=False" in code

    @pytest.mark.asyncio
    async def test_create_returns_the_new_reference_node(self, monkeypatch):
        """Caller needs the node back — every other operation requires it."""
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="create", file_path="/pub/char/hero_v003.ma"))

        assert "'reference_node'" in captured["code"]

    @pytest.mark.asyncio
    async def test_create_wraps_in_undo_chunk(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="create", file_path="/pub/char/hero_v003.ma"))

        code = captured["code"]
        assert "undoInfo(openChunk=True" in code
        assert "undoInfo(closeChunk=True)" in code


class TestReplace:
    """operation='replace' is the version swap."""

    @pytest.mark.asyncio
    async def test_replace_repoints_node_at_new_file(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="replace", reference_node="heroRN",
            file_path="/pub/char/hero_v004.ma"))

        code = captured["code"]
        assert "loadReference='heroRN'" in code
        assert "/pub/char/hero_v004.ma" in code

    @pytest.mark.asyncio
    async def test_replace_reports_both_sides_of_the_swap(self, monkeypatch):
        """'was' and 'now' make the swap auditable instead of silent."""
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="replace", reference_node="heroRN",
            file_path="/pub/char/hero_v004.ma"))

        code = captured["code"]
        assert "'was'" in code
        assert "'now'" in code


class TestLoadUnload:
    """load/unload toggle content while keeping the link."""

    @pytest.mark.asyncio
    async def test_unload_uses_unload_flag(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="unload", reference_node="heroRN"))

        code = captured["code"]
        assert "unloadReference='heroRN'" in code
        assert "removeReference" not in code

    @pytest.mark.asyncio
    async def test_load_uses_load_flag(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="load", reference_node="heroRN"))

        assert "cmds.file(loadReference='heroRN')" in captured["code"]

    @pytest.mark.asyncio
    async def test_reports_loaded_state_back(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        await srv.maya_reference(srv.ReferenceInput(
            operation="unload", reference_node="heroRN"))

        assert "isLoaded=True" in captured["code"]


class TestOperandValidation:
    """Missing operands fail before Maya is touched, with an actionable message."""

    @pytest.mark.asyncio
    async def test_replace_without_node_errors_without_calling_bridge(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        out = await srv.maya_reference(srv.ReferenceInput(
            operation="replace", file_path="/pub/char/hero_v004.ma"))

        assert "reference_node" in json.loads(out)["error"]
        assert captured["calls"] == 0

    @pytest.mark.asyncio
    async def test_error_points_at_list_to_obtain_the_node(self, monkeypatch):
        _capture_bridge_execute(monkeypatch)

        out = await srv.maya_reference(srv.ReferenceInput(
            operation="unload"))

        assert "list" in json.loads(out)["error"]

    @pytest.mark.asyncio
    async def test_create_without_file_path_errors(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        out = await srv.maya_reference(srv.ReferenceInput(operation="create"))

        assert "file_path" in json.loads(out)["error"]
        assert captured["calls"] == 0

    @pytest.mark.asyncio
    async def test_load_without_node_errors(self, monkeypatch):
        captured = _capture_bridge_execute(monkeypatch)

        out = await srv.maya_reference(srv.ReferenceInput(operation="load"))

        assert "error" in json.loads(out)
        assert captured["calls"] == 0


class TestRemovalNotOffered:
    """Reference removal is destructive and deliberately out of reach here."""

    def test_no_remove_operation_exists(self):
        values = {m.value for m in srv.ReferenceOperation}
        assert values == {"create", "list", "replace", "load", "unload"}
        assert "remove" not in values

    def test_remove_is_rejected_by_the_input_model(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            srv.ReferenceInput(operation="remove", reference_node="heroRN")
