"""
test_audit.py
=============
Unit + integration tests for the durable, append-only audit log
(`maya_mcp._audit`) and its server.py wiring.

Covers the brief's required behaviours:
  - toggle OFF (default) writes nothing;
  - toggle ON appends one well-formed entry with the expected fields;
  - execute_python code is truncated + hashed, full payloads are never stored;
  - blocked / failed ops are recorded with the right status
    (safety_blocked / ast_rejected / error);
  - the 5 MB + `.1` rotation guard (reused from persist_timing) works;
  - the standalone @mcp.tool decorator records direct mutations.

The MCP SDK is stubbed by conftest.py, so the @mcp.tool decorator is a no-op
and `server.maya_*` resolves to the `_audited`-wrapped handler directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from maya_mcp import _audit
from maya_mcp._session_stats import TELEMETRY_MAX_BYTES, make_empty_stats


# ── audit_enabled (the toggle) ───────────────────────────────────────────────

def test_audit_disabled_by_default(monkeypatch) -> None:
    """Unset MAYA_AUDIT_LOG → audit is OFF (the safe, no-op default)."""
    monkeypatch.delenv("MAYA_AUDIT_LOG", raising=False)
    assert _audit.audit_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " 1 "])
def test_audit_enabled_truthy_spellings(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MAYA_AUDIT_LOG", value)
    assert _audit.audit_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_audit_enabled_falsy_spellings(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MAYA_AUDIT_LOG", value)
    assert _audit.audit_enabled() is False


# ── build_record / sanitize_params ───────────────────────────────────────────

def test_build_record_has_required_fields() -> None:
    rec = _audit.build_record(
        "maya_session", "execute_python", {"code": "result = 1"}, _audit.AUDIT_OK
    )
    assert {"ts", "tool", "action", "status", "model", "backend", "params"} <= set(rec)
    assert rec["tool"] == "maya_session"
    assert rec["action"] == "execute_python"
    assert rec["status"] == "ok"


def test_build_record_coerces_unknown_status() -> None:
    """An out-of-set status is defensively coerced to 'error', never stored raw."""
    rec = _audit.build_record("t", "a", {}, "bogus_status")
    assert rec["status"] == _audit.AUDIT_ERROR


def test_sanitize_truncates_and_hashes_long_code() -> None:
    big = "c" * 5000
    out = _audit.sanitize_params({"code": big})
    assert len(out["code"]) == _audit.DEFAULT_MAX_CODE_CHARS
    assert out["code_len"] == 5000
    assert out["code_sha256"] == hashlib.sha256(big.encode("utf-8")).hexdigest()
    assert out["code_truncated"] is True


def test_sanitize_keeps_short_code_intact() -> None:
    out = _audit.sanitize_params({"code": "result = 1 + 1", "timeout": 30})
    assert out["code"] == "result = 1 + 1"
    assert out["code_len"] == len("result = 1 + 1")
    assert "code_truncated" not in out
    assert out["timeout"] == 30  # other params pass through


def test_sanitize_handles_pydantic_model() -> None:
    """Standalone tools hand a Pydantic model; it must be dumped to a dict."""
    from maya_mcp.server import CreatePrimitiveInput

    out = _audit.sanitize_params(CreatePrimitiveInput(primitive_type="cube"))
    assert out["primitive_type"] == "cube"


def test_sanitize_none_params() -> None:
    assert _audit.sanitize_params(None) == {}


# ── status_from_output ───────────────────────────────────────────────────────

def test_status_from_output_classifies_payloads() -> None:
    assert _audit.status_from_output(json.dumps({"safety_warning": "x"})) == "safety_blocked"
    assert _audit.status_from_output(json.dumps({"ast_warning": "x"})) == "ast_rejected"
    assert _audit.status_from_output(json.dumps({"error": "nope"})) == "error"
    assert _audit.status_from_output(json.dumps({"deleted": ["pCube1"]})) == "ok"
    assert _audit.status_from_output("Maya error: boom") == "error"
    assert _audit.status_from_output("Unexpected error: TypeError") == "error"
    assert _audit.status_from_output("ERROR: RuntimeError") == "error"
    assert _audit.status_from_output("OK") == "ok"


def test_status_from_output_handles_list_payload() -> None:
    """viewport_capture-style list payloads: error string => error, else ok."""
    assert _audit.status_from_output(["meta", "(image data)"]) == "ok"
    assert _audit.status_from_output(["Maya error: nope"]) == "error"


# ── write_record / rotation guard ────────────────────────────────────────────

def test_write_record_appends_one_line(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    _audit.write_record(log, {"tool": "maya_session", "status": "ok"})
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "ok"


def test_write_record_rotation_guard(tmp_path: Path) -> None:
    """At TELEMETRY_MAX_BYTES the log rotates to .1 and the new line lands in a
    fresh file — the rotation is inherited verbatim from persist_timing."""
    log = tmp_path / "audit.jsonl"
    rotated = tmp_path / "audit.jsonl.1"
    log.write_bytes(b"X" * (TELEMETRY_MAX_BYTES + 1))

    rec = _audit.build_record(
        "maya_session", "execute_python", {"code": "x"}, _audit.AUDIT_OK
    )
    _audit.write_record(log, rec)

    assert rotated.exists()
    new_lines = log.read_text(encoding="utf-8").splitlines()
    assert len(new_lines) == 1
    assert json.loads(new_lines[0])["action"] == "execute_python"


def test_write_record_swallows_io_errors(tmp_path: Path) -> None:
    """An unwritable path must NOT raise — the audit is best-effort."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    _audit.write_record(blocker / "audit.jsonl", {"status": "ok"})  # must not raise


# ── server wiring (integration) ──────────────────────────────────────────────

@pytest.fixture()
def audit_log(tmp_path, monkeypatch):
    """Point the server's audit log at a temp file and reset _stats."""
    from maya_mcp import server

    log = tmp_path / "audit.jsonl"
    monkeypatch.setattr(server, "_AUDIT_LOG", log)
    server._stats.update(make_empty_stats())
    return log


def _last_record(log: Path) -> dict:
    lines = log.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def test_toggle_off_writes_nothing(audit_log, monkeypatch) -> None:
    """With the toggle unset, a successful execute_python writes no audit file."""
    from maya_mcp import server

    monkeypatch.delenv("MAYA_AUDIT_LOG", raising=False)
    monkeypatch.setattr(server.bridge, "execute", lambda code, as_json=False: "OK")

    asyncio.run(server._do_execute_python({"code": "result = 1 + 1"}))

    assert not audit_log.exists()


def test_execute_python_ok_recorded(audit_log, monkeypatch) -> None:
    """Toggle ON: a successful execute_python appends exactly one ok entry with
    the truncation/hash fields, and never stores the Maya payload."""
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")
    monkeypatch.setattr(server.bridge, "execute", lambda code, as_json=False: "OK")

    asyncio.run(server._do_execute_python({"code": "result = 1 + 1"}))

    lines = audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["tool"] == "maya_session"
    assert rec["action"] == "execute_python"
    assert rec["status"] == "ok"
    assert rec["params"]["code"] == "result = 1 + 1"
    assert "code_sha256" in rec["params"]
    # The Maya result payload ("OK") must NOT be present anywhere in the record.
    assert "OK" not in json.dumps(rec["params"])


def test_execute_python_safety_block_recorded(audit_log, monkeypatch) -> None:
    """A safety-blocked execute_python (never reaches Maya) is still audited."""
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")
    monkeypatch.setattr(server.bridge, "execute", lambda code, as_json=False: "OK")

    asyncio.run(server._do_execute_python({"code": "cmds.delete('*')"}))

    rec = _last_record(audit_log)
    assert rec["action"] == "execute_python"
    assert rec["status"] == "safety_blocked"


def test_execute_python_ast_rejected_recorded(audit_log, monkeypatch) -> None:
    """An AST-rejected (hallucinated command) execute_python is audited."""
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")
    monkeypatch.setattr(server.bridge, "execute", lambda code, as_json=False: "OK")

    class _FailVal:
        ok = False

    monkeypatch.setattr(server, "validate_python", lambda code: _FailVal())
    monkeypatch.setattr(server, "format_issues", lambda v: "unknown command")

    asyncio.run(server._do_execute_python({"code": "result = cmds.notAReal()"}))

    rec = _last_record(audit_log)
    assert rec["status"] == "ast_rejected"


def test_execute_python_error_recorded(audit_log, monkeypatch) -> None:
    """A bridge failure is audited with status 'error'."""
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")

    def boom(code, as_json=False):
        raise server.MayaBridgeError("boom")

    monkeypatch.setattr(server.bridge, "execute", boom)

    asyncio.run(server._do_execute_python({"code": "result = bad()"}))

    rec = _last_record(audit_log)
    assert rec["status"] == "error"


def test_delete_safety_block_recorded(audit_log, monkeypatch) -> None:
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")
    monkeypatch.setattr(server.bridge, "execute", lambda code, as_json=False: "OK")

    asyncio.run(server._do_delete({"object_name": "*"}))

    rec = _last_record(audit_log)
    assert rec["tool"] == "maya_session"
    assert rec["action"] == "delete"
    assert rec["status"] == "safety_blocked"


def test_standalone_tool_audited(audit_log, monkeypatch) -> None:
    """A direct mutation tool records via the @_audited decorator with action '-'."""
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")
    monkeypatch.setattr(
        server.bridge, "execute",
        lambda code, as_json=False: '{"name": "pCube1", "type": "cube"}',
    )

    out = asyncio.run(
        server.maya_create_primitive(server.CreatePrimitiveInput(primitive_type="cube"))
    )
    # The decorator must not alter the returned payload.
    assert "pCube1" in out

    rec = _last_record(audit_log)
    assert rec["tool"] == "maya_create_primitive"
    assert rec["action"] == "-"
    assert rec["status"] == "ok"


def test_standalone_tool_error_audited(audit_log, monkeypatch) -> None:
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")

    def boom(code, as_json=False):
        raise server.MayaBridgeError("boom")

    monkeypatch.setattr(server.bridge, "execute", boom)

    asyncio.run(
        server.maya_transform(
            server.TransformInput(object_name="pCube1", position=[1, 0, 0])
        )
    )

    rec = _last_record(audit_log)
    assert rec["tool"] == "maya_transform"
    assert rec["status"] == "error"


def test_dispatcher_audits_save_scene(audit_log, monkeypatch) -> None:
    """A session mutation routed through the dispatcher is audited centrally."""
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")
    monkeypatch.setattr(
        server.bridge, "execute",
        lambda code, as_json=False: '{"saved": "/tmp/scene.ma"}',
    )

    asyncio.run(
        server.maya_session(
            server.SessionDispatchInput(action="save_scene"), None
        )
    )

    rec = _last_record(audit_log)
    assert rec["tool"] == "maya_session"
    assert rec["action"] == "save_scene"
    assert rec["status"] == "ok"


def test_dispatcher_skips_readonly_actions(audit_log, monkeypatch) -> None:
    """Read-only actions (list_scene) are excluded from the audit by default."""
    from maya_mcp import server

    monkeypatch.setenv("MAYA_AUDIT_LOG", "1")
    monkeypatch.setattr(
        server.bridge, "execute",
        lambda code, as_json=False: '{"count": 0, "objects": []}',
    )

    asyncio.run(
        server.maya_session(
            server.SessionDispatchInput(action="list_scene"), None
        )
    )

    assert not audit_log.exists()
