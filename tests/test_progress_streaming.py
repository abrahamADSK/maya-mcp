"""
test_progress_streaming.py
==========================
Tests for the visible-progress streaming port (Chat 62 design, Chat 63 impl):
MCP-native ``ctx.info`` / ``ctx.report_progress`` emissions from the
long-running maya-mcp paths.

Covered surfaces (src/maya_mcp/server.py):
  1. ``_execute_with_heartbeat`` — silent on fast operations, emits
     ``ctx.info`` heartbeats on slow ones, tolerates ``ctx=None``.
  2. ``_do_execute_python`` — routes through the heartbeat helper and
     still returns the bridge response unchanged.
  3. ``maya_session`` dispatch — threads ``ctx`` ONLY into the two
     long-running handlers (launch / execute_python); the plain handlers
     keep their (params) signature.
  4. ``maya_import_file`` — emits a start ``ctx.info`` line and routes
     the bridge call through the heartbeat helper.

No Maya instance, MCP SDK, or network access required (conftest stubs).
"""

import json
import time

import pytest

from maya_mcp import server as srv


# ── Helpers ──────────────────────────────────────────────────────────────

def _fast_execute(monkeypatch, response=None):
    """bridge.execute returns instantly."""
    payload = response or json.dumps({"result": "ok"})

    def fake_execute(code: str, timeout=None) -> str:
        return payload

    monkeypatch.setattr(srv.bridge, "execute", fake_execute)
    return payload


def _slow_execute(monkeypatch, delay_s: float, response=None):
    """bridge.execute blocks for delay_s seconds (in the worker thread)."""
    payload = response or json.dumps({"result": "ok"})

    def fake_execute(code: str, timeout=None) -> str:
        time.sleep(delay_s)
        return payload

    monkeypatch.setattr(srv.bridge, "execute", fake_execute)
    return payload


# ── 1. _execute_with_heartbeat ───────────────────────────────────────────

class TestExecuteWithHeartbeat:
    """Heartbeat helper: silence on fast ops, info lines on slow ones."""

    @pytest.mark.asyncio
    async def test_fast_operation_emits_nothing(self, monkeypatch, mock_ctx):
        """An operation faster than the interval produces zero ctx calls."""
        payload = _fast_execute(monkeypatch)

        out = await srv._execute_with_heartbeat("code", mock_ctx, "exec", interval=1)

        assert out == payload
        mock_ctx.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_slow_operation_emits_heartbeats(self, monkeypatch, mock_ctx):
        """An operation slower than the interval emits >=1 ctx.info heartbeat."""
        payload = _slow_execute(monkeypatch, delay_s=0.35)

        out = await srv._execute_with_heartbeat(
            "code", mock_ctx, "exec", interval=0.1
        )

        assert out == payload
        assert mock_ctx.info.await_count >= 1
        msg = mock_ctx.info.await_args_list[0].args[0]
        assert "exec still running in Maya" in msg

    @pytest.mark.asyncio
    async def test_none_ctx_is_tolerated(self, monkeypatch):
        """ctx=None must not raise even when heartbeats would fire."""
        payload = _slow_execute(monkeypatch, delay_s=0.25)

        out = await srv._execute_with_heartbeat("code", None, "exec", interval=0.1)

        assert out == payload

    @pytest.mark.asyncio
    async def test_bridge_exception_propagates(self, monkeypatch, mock_ctx):
        """A bridge error surfaces to the caller (handled upstream)."""
        def boom(code: str) -> str:
            raise RuntimeError("socket dead")

        monkeypatch.setattr(srv.bridge, "execute", boom)

        with pytest.raises(RuntimeError, match="socket dead"):
            await srv._execute_with_heartbeat("code", mock_ctx, "exec", interval=1)

    @pytest.mark.asyncio
    async def test_bridge_timeout_forwarded(self, monkeypatch, mock_ctx):
        """bridge_timeout reaches bridge.execute as the timeout kwarg."""
        seen = {}

        def fake_execute(code: str, timeout=None) -> str:
            seen["timeout"] = timeout
            return "{}"

        monkeypatch.setattr(srv.bridge, "execute", fake_execute)

        await srv._execute_with_heartbeat(
            "code", mock_ctx, "exec", interval=1, bridge_timeout=120.0
        )

        assert seen["timeout"] == 120.0

    @pytest.mark.asyncio
    async def test_no_timeout_kwarg_when_unset(self, monkeypatch, mock_ctx):
        """Without bridge_timeout, plain (code)-signature doubles keep working."""
        def fake_execute(code: str) -> str:  # no timeout kwarg on purpose
            return "{}"

        monkeypatch.setattr(srv.bridge, "execute", fake_execute)

        out = await srv._execute_with_heartbeat("code", mock_ctx, "exec", interval=1)

        assert out == "{}"


# ── 1b. Per-call bridge timeout (ExecutePythonInput.timeout) ─────────────

class TestExecutePythonTimeout:
    """ExecutePythonInput.timeout is validated and forwarded to the bridge."""

    @pytest.mark.asyncio
    async def test_timeout_forwarded_to_bridge(self, monkeypatch, mock_ctx):
        seen = {}

        def fake_execute(code: str, timeout=None) -> str:
            seen["timeout"] = timeout
            return "{}"

        monkeypatch.setattr(srv.bridge, "execute", fake_execute)

        await srv._do_execute_python(
            {"code": "result = 1", "timeout": 60}, mock_ctx
        )

        assert seen["timeout"] == 60

    @pytest.mark.asyncio
    async def test_timeout_out_of_range_rejected(self, monkeypatch):
        """timeout > 600 fails Pydantic validation, never reaches the bridge."""
        out = await srv._do_execute_python({"code": "result = 1", "timeout": 9999})

        assert "Invalid params" in out


# ── 2. _do_execute_python via heartbeat ──────────────────────────────────

class TestExecutePythonStreaming:
    """_do_execute_python routes through the heartbeat helper."""

    @pytest.mark.asyncio
    async def test_response_unchanged_with_ctx(self, monkeypatch, mock_ctx):
        payload = _fast_execute(monkeypatch)

        out = await srv._do_execute_python({"code": "result = 1"}, mock_ctx)

        assert out == payload

    @pytest.mark.asyncio
    async def test_works_without_ctx(self, monkeypatch):
        """Direct calls without ctx (tests, internal callers) keep working."""
        payload = _fast_execute(monkeypatch)

        out = await srv._do_execute_python({"code": "result = 1"})

        assert out == payload


# ── 3. maya_session dispatch threading ───────────────────────────────────

class TestSessionDispatchCtx:
    """maya_session threads ctx only into launch / execute_python."""

    @pytest.mark.asyncio
    async def test_execute_python_receives_ctx(self, monkeypatch, mock_ctx):
        """Dispatch passes ctx through to _do_execute_python without error."""
        _slow_execute(monkeypatch, delay_s=0.0)
        received = {}

        async def spy(params, ctx=None):
            received["ctx"] = ctx
            return "{}"

        monkeypatch.setattr(srv, "_do_execute_python", spy)
        params = srv.SessionDispatchInput(
            action="execute_python", params={"code": "result = 1"}
        )

        await srv.maya_session(params, mock_ctx)

        assert received["ctx"] is mock_ctx

    @pytest.mark.asyncio
    async def test_plain_action_does_not_receive_ctx(self, monkeypatch, mock_ctx):
        """Non-streaming handlers keep the plain (params) signature."""
        called = {}

        async def spy(params):
            called["params"] = params
            return "{}"

        monkeypatch.setattr(srv, "_do_ping", spy)
        params = srv.SessionDispatchInput(action="ping")

        await srv.maya_session(params, mock_ctx)

        assert "params" in called  # would TypeError if ctx were passed


# ── 4. maya_import_file streaming ────────────────────────────────────────

class TestImportFileStreaming:
    """maya_import_file emits a start info line and uses the heartbeat path."""

    @pytest.mark.asyncio
    async def test_start_info_emitted(self, monkeypatch, mock_ctx):
        _fast_execute(monkeypatch, response=json.dumps({
            "imported": 1, "objects": ["obj1"], "file": "m.obj",
            "method": "OBJ", "warning": "",
        }))

        params = srv.ImportFileInput(file_path="/assets/m.obj")
        await srv.maya_import_file(params, mock_ctx)

        assert mock_ctx.info.await_count == 1
        msg = mock_ctx.info.await_args_list[0].args[0]
        assert "Importing m.obj" in msg

    @pytest.mark.asyncio
    async def test_works_without_ctx(self, monkeypatch):
        """Existing single-arg call sites keep working (regression guard)."""
        _fast_execute(monkeypatch, response=json.dumps({
            "imported": 1, "objects": ["obj1"], "file": "m.obj",
            "method": "OBJ", "warning": "",
        }))

        params = srv.ImportFileInput(file_path="/assets/m.obj")
        out = await srv.maya_import_file(params)

        assert "imported" in out
