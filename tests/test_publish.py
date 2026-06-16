"""
test_publish.py
===============
Tests for maya_session(action='publish') -> _do_publish in src/maya_mcp/server.py.

Monkeypatches bridge.execute to capture the code string + timeout sent to Maya,
then asserts the PREVIEW payload shape contract and the host-side include/exclude/
comment injection + synonym expansion. No Maya, MCP SDK, or network required.

The in-Maya activation/validate/publish/finalize logic (PublishManager) runs only
inside an engine'd Maya and is validated in-vivo (see HANDOFF rig->model), not here.
"""

import json

import pytest

from maya_mcp.maya_bridge import MayaBridgeError
from maya_mcp import server as srv


# ── helpers ─────────────────────────────────────────────────────────────────

def _capture(monkeypatch, ret=None):
    """Capture code+timeout; return a canned JSON (a representative preview)."""
    captured = {"code": None, "timeout": None}
    payload = ret if ret is not None else {
        "ok": True, "mode": "preview", "engine": "tk-maya", "item_count": 1,
        "items": [{"name": "Current Maya Session", "type_spec": "maya.session",
                   "active": True, "is_top_level": True, "tasks": []}],
    }

    def fake_execute(code: str, timeout=None) -> str:
        captured["code"] = code
        captured["timeout"] = timeout
        return json.dumps(payload)

    monkeypatch.setattr(srv.bridge, "execute", fake_execute)
    return captured


# ── 1. synonym expansion (pure host-side logic) ──────────────────────────────

class TestExpandTokens:
    def test_keeps_literal_and_expands(self):
        out = srv._expand_tokens(["USD"])
        assert "usd" in out

    def test_no_render_phrase_expands_to_render_tokens(self):
        out = srv._expand_tokens(["sin render"])
        assert "render" in out and "rendered image" in out and "exr" in out

    def test_dedupe_preserves_order_and_lowercases(self):
        out = srv._expand_tokens(["Rig", "rig", "model"])
        assert out == ["rig", "model"]

    def test_none_and_blank_safe(self):
        assert srv._expand_tokens(None) == []
        assert srv._expand_tokens(["", "  "]) == []


# ── 2. preview mode: shape contract + generated code ─────────────────────────

class TestPreviewMode:
    @pytest.mark.asyncio
    async def test_preview_uses_native_api_and_no_filter_header(self, monkeypatch):
        cap = _capture(monkeypatch)
        out = await srv._do_publish({"mode": "preview"})
        code = cap["code"]
        assert "sgtk.platform.current_engine()" in code
        assert 'engine.apps.get("tk-multi-publish2")' in code
        assert "create_publish_manager()" in code
        assert "collect_session()" in code
        assert "for item in manager.tree:" in code
        # preview must NOT inject the publish filter header
        assert "_INCLUDE" not in code and "_EXCLUDE" not in code
        # robustness guards present
        assert '"no_engine"' in code and '"publisher_not_configured"' in code
        # handler passes the bridge payload through unchanged
        parsed = json.loads(out)
        assert parsed["ok"] is True and parsed["mode"] == "preview"
        assert "items" in parsed and "item_count" in parsed

    @pytest.mark.asyncio
    async def test_preview_default_timeout(self, monkeypatch):
        cap = _capture(monkeypatch)
        await srv._do_publish({"mode": "preview"})
        assert cap["timeout"] == 120.0

    @pytest.mark.asyncio
    async def test_default_mode_is_preview(self, monkeypatch):
        cap = _capture(monkeypatch)
        await srv._do_publish({})        # no mode -> preview
        assert "_INCLUDE" not in cap["code"]


# ── 3. publish mode: include/exclude/comment injection ───────────────────────

class TestPublishFiltering:
    def _header(self, code):
        # first three lines are the injected literals
        return {ln.split(" = ")[0]: json.loads(ln.split(" = ", 1)[1])
                for ln in code.splitlines()[:3]}

    @pytest.mark.asyncio
    async def test_include_whitelist_expanded(self, monkeypatch):
        cap = _capture(monkeypatch)
        await srv._do_publish({"mode": "publish", "include": ["rig"]})
        h = self._header(cap["code"])
        assert "rig" in h["_INCLUDE"]
        assert h["_EXCLUDE"] == []
        assert "validate()" in cap["code"] and "manager.publish()" in cap["code"]

    @pytest.mark.asyncio
    async def test_exclude_no_render_expanded(self, monkeypatch):
        cap = _capture(monkeypatch)
        await srv._do_publish({"mode": "publish", "exclude": ["no render"]})
        h = self._header(cap["code"])
        assert "render" in h["_EXCLUDE"] and "exr" in h["_EXCLUDE"]
        assert h["_INCLUDE"] == []

    @pytest.mark.asyncio
    async def test_include_and_exclude_both_present(self, monkeypatch):
        cap = _capture(monkeypatch)
        await srv._do_publish({"mode": "publish",
                               "include": ["model"], "exclude": ["texture"]})
        h = self._header(cap["code"])
        assert "model" in h["_INCLUDE"] and "texture" in h["_EXCLUDE"]

    @pytest.mark.asyncio
    async def test_comment_injected_and_escaped(self, monkeypatch):
        cap = _capture(monkeypatch)
        await srv._do_publish({"mode": "publish", "include": ["rig"],
                               "comment": 'rig v2 "final"'})
        h = self._header(cap["code"])
        assert h["_COMMENT"] == 'rig v2 "final"'   # json round-trips the quotes

    @pytest.mark.asyncio
    async def test_publish_default_timeout(self, monkeypatch):
        cap = _capture(monkeypatch)
        await srv._do_publish({"mode": "publish", "include": ["rig"]})
        assert cap["timeout"] == 600.0

    @pytest.mark.asyncio
    async def test_explicit_timeout_passthrough(self, monkeypatch):
        cap = _capture(monkeypatch)
        await srv._do_publish({"mode": "publish", "include": ["rig"], "timeout": 300})
        assert cap["timeout"] == 300.0


# ── 4. validation / error handling ───────────────────────────────────────────

class TestValidationAndErrors:
    @pytest.mark.asyncio
    async def test_extra_field_forbidden(self, monkeypatch):
        _capture(monkeypatch)
        out = await srv._do_publish({"mode": "preview", "bogus": 1})
        assert "Invalid params for publish" in json.loads(out)["error"]

    @pytest.mark.asyncio
    async def test_bad_mode_rejected(self, monkeypatch):
        _capture(monkeypatch)
        out = await srv._do_publish({"mode": "nope"})
        assert "Invalid params for publish" in json.loads(out)["error"]

    @pytest.mark.asyncio
    async def test_timeout_out_of_range_rejected(self, monkeypatch):
        _capture(monkeypatch)
        out = await srv._do_publish({"mode": "preview", "timeout": 9999})
        assert "Invalid params for publish" in json.loads(out)["error"]

    @pytest.mark.asyncio
    async def test_bridge_error_formatted(self, monkeypatch):
        def boom(code, timeout=None):
            raise MayaBridgeError("command port down")
        monkeypatch.setattr(srv.bridge, "execute", boom)
        out = await srv._do_publish({"mode": "preview"})
        assert out.startswith("Maya error:")


# ── 5. dispatcher wiring ─────────────────────────────────────────────────────

class TestDispatch:
    def test_publish_action_registered(self):
        assert srv.SessionAction.PUBLISH.value == "publish"

    @pytest.mark.asyncio
    async def test_dispatch_routes_publish_with_ctx(self, monkeypatch, mock_ctx):
        cap = _capture(monkeypatch)
        params = srv.SessionDispatchInput(action="publish", params={"mode": "preview"})
        out = await srv.maya_session(params, mock_ctx)
        assert json.loads(out)["mode"] == "preview"
        assert "create_publish_manager()" in cap["code"]
