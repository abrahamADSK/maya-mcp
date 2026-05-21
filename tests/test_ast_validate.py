"""
test_ast_validate.py
====================
F4b (3C Wave 4) — tests for the maya.cmds AST dry-run validator
(`maya_mcp._ast_validate`) and its wiring into execute_python.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from maya_mcp._ast_validate import (
    AstValidation,
    format_issues,
    validate_python,
    _API_GRAPH_PATH,
    _reset_graph_cache,
)

# A tiny in-memory graph for the pure-logic tests — independent of any
# on-disk api_graph.json so the mock tests are deterministic.
_GRAPH = {"_meta": {"maya_version": "test"}, "commands": {"polyCube": {}, "ls": {}, "xform": {}}}


# ── command existence ────────────────────────────────────────────────────────

def test_valid_cmds_command_passes():
    v = validate_python("cmds.polyCube(w=1, h=1, d=1)", graph=_GRAPH)
    assert v.ok
    assert v.graph_loaded


def test_unknown_cmds_command_flagged_with_suggestion():
    v = validate_python("cmds.polyCubez()", graph=_GRAPH)
    assert not v.ok
    assert len(v.issues) == 1
    issue = v.issues[0]
    assert issue.command == "polyCubez"
    assert issue.suggestion == "polyCube"
    assert issue.line == 1


def test_maya_cmds_fully_qualified_form():
    assert validate_python("maya.cmds.ls(sl=True)", graph=_GRAPH).ok
    bad = validate_python("maya.cmds.bogusThing()", graph=_GRAPH)
    assert not bad.ok
    assert bad.issues[0].command == "bogusThing"


def test_method_on_command_result_is_ignored():
    """cmds.ls() returns a list; .strip()/.append() on the result must NOT be
    treated as cmds commands (only the command directly on cmds counts)."""
    v = validate_python("names = cmds.ls(type='mesh')\nfirst = names[0].strip()", graph=_GRAPH)
    assert v.ok


def test_chained_attr_validates_only_the_command():
    # cmds.xform exists; .something on cmds.xform would be a separate node but
    # its value is `cmds.xform` (an Attribute), not the cmds Name → not a command.
    v = validate_python("cmds.xform('obj', t=(1, 2, 3))", graph=_GRAPH)
    assert v.ok


def test_aliased_module_not_validated():
    """`import maya.cmds as mc; mc.foo()` — we do not know `mc` is cmds, so we
    must NOT false-positive on an unrelated name."""
    v = validate_python("mc.totallyNotAMayaCommand()", graph=_GRAPH)
    assert v.ok  # no cmds.* references → nothing to flag


def test_multiple_unknowns_each_reported():
    v = validate_python("cmds.fooBar()\ncmds.bazQux()", graph=_GRAPH)
    assert len(v.issues) == 2
    assert {i.command for i in v.issues} == {"fooBar", "bazQux"}


# ── graceful degradation ─────────────────────────────────────────────────────

def test_empty_graph_is_noop():
    v = validate_python("cmds.anythingGoes()", graph={"commands": {}})
    assert v.ok
    assert v.graph_loaded is False


def test_syntax_error_is_noop_not_double_error():
    v = validate_python("cmds.polyCube(", graph=_GRAPH)  # unbalanced paren
    assert v.ok
    assert v.graph_loaded is True


def test_missing_graph_file_degrades(tmp_path: Path):
    _reset_graph_cache()
    v = validate_python("cmds.bogus()", graph_path=tmp_path / "nope.json")
    assert v.ok
    assert v.graph_loaded is False
    _reset_graph_cache()


# ── format_issues ────────────────────────────────────────────────────────────

def test_format_issues_mentions_command_and_suggestion():
    v = validate_python("cmds.polyCubez()", graph=_GRAPH)
    msg = format_issues(v)
    assert "cmds.polyCubez" in msg
    assert "did you mean `cmds.polyCube`?" in msg
    assert "introspect_maya_api.py" in msg


def test_format_issues_empty_when_ok():
    assert format_issues(AstValidation(issues=[])) == ""


# ── real on-disk api_graph.json (regression guard, not mocked) ───────────────

@pytest.mark.skipif(
    not _API_GRAPH_PATH.exists(),
    reason="api_graph.json not generated (run mayapy scripts/introspect_maya_api.py)",
)
def test_real_graph_accepts_known_command_rejects_fake():
    _reset_graph_cache()
    assert validate_python("cmds.polyCube(w=1)").ok
    fake = validate_python("cmds.thisCommandDoesNotExist()")
    assert not fake.ok
    assert fake.graph_loaded
    _reset_graph_cache()


@pytest.mark.skipif(
    not _API_GRAPH_PATH.exists(),
    reason="api_graph.json not generated",
)
def test_real_graph_has_reasonable_command_count():
    import json as _json
    graph = _json.loads(_API_GRAPH_PATH.read_text(encoding="utf-8"))
    # Maya ships thousands of cmds commands; guard against a truncated/empty graph.
    assert len(graph["commands"]) > 1000
    assert "polyCube" in graph["commands"]
    assert "ls" in graph["commands"]


# ── server wiring (execute_python pre-flight) ────────────────────────────────

@pytest.mark.skipif(
    not _API_GRAPH_PATH.exists(),
    reason="api_graph.json not generated",
)
def test_execute_python_rejects_hallucinated_command(monkeypatch):
    """A hallucinated cmds.<command> is rejected with ast_warning and the
    bridge is never called (and it does not count as a turn)."""
    from maya_mcp import server
    from maya_mcp._session_stats import make_empty_stats

    called = {"bridge": False}

    def fake_execute(code, as_json=False):
        called["bridge"] = True
        return "OK"

    monkeypatch.setattr(server.bridge, "execute", fake_execute)
    server._stats.update(make_empty_stats())
    _reset_graph_cache()

    out = asyncio.run(server._do_execute_python({"code": "cmds.totallyFakeCommandXYZ()"}))

    parsed = json.loads(out)
    assert "ast_warning" in parsed
    assert called["bridge"] is False
    assert server._stats["turns_total"] == 0   # rejected before the turn ran


@pytest.mark.skipif(
    not _API_GRAPH_PATH.exists(),
    reason="api_graph.json not generated",
)
def test_execute_python_allows_known_command(monkeypatch):
    from maya_mcp import server
    from maya_mcp._session_stats import make_empty_stats

    monkeypatch.setattr(server.bridge, "execute", lambda code, as_json=False: "OK")
    server._stats.update(make_empty_stats())
    _reset_graph_cache()

    out = asyncio.run(server._do_execute_python({"code": "result = cmds.ls(type='mesh')"}))

    assert out == "OK"
    assert server._stats["turns_total"] == 1
