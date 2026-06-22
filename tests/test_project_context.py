"""Tests for the Maya console's engine-project resolution (Chat 69).

The Maya console binds its fpt-mcp ShotGrid ops to the ``tk-maya`` engine's
project (authoritative when tank-launched) via ``SHOTGRID_PROJECT_ID``, falling
back to ``"0"`` ("no project") when there is no engine — so a project-scoped
create fails loudly instead of writing to a stale ``.env`` default.
"""
import sys
import types

from console.project_context import project_env, resolve_engine_project


def _fake_sgtk(project):
    """A minimal stand-in for the sgtk module with an engine context."""
    mod = types.ModuleType("sgtk")
    platform = types.ModuleType("sgtk.platform")

    class _Ctx:
        pass

    class _Eng:
        pass

    eng = _Eng()
    eng.context = _Ctx()
    eng.context.project = project
    platform.current_engine = lambda: eng
    mod.platform = platform
    return mod


# ── project_env (pure) ──────────────────────────────────────────────────────

def test_project_env_valid_int():
    assert project_env(1244) == {"SHOTGRID_PROJECT_ID": "1244"}


def test_project_env_string_coerced():
    assert project_env("1244") == {"SHOTGRID_PROJECT_ID": "1244"}


def test_project_env_zero_or_none_is_no_project():
    assert project_env(0) == {"SHOTGRID_PROJECT_ID": "0"}
    assert project_env(None) == {"SHOTGRID_PROJECT_ID": "0"}
    assert project_env("nope") == {"SHOTGRID_PROJECT_ID": "0"}


# ── resolve_engine_project ──────────────────────────────────────────────────

def test_resolve_engine_project_from_context(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "sgtk",
        _fake_sgtk({"type": "Project", "id": 1244, "name": "MCP_project_Abraham"}),
    )
    assert resolve_engine_project() == 1244


def test_resolve_no_engine(monkeypatch):
    fake = _fake_sgtk(None)
    fake.platform.current_engine = lambda: None
    monkeypatch.setitem(sys.modules, "sgtk", fake)
    assert resolve_engine_project() is None


def test_resolve_engine_without_project(monkeypatch):
    monkeypatch.setitem(sys.modules, "sgtk", _fake_sgtk(None))
    assert resolve_engine_project() is None


def test_resolve_no_sgtk(monkeypatch):
    # sgtk absent (the CI/non-Maya case): import fails → None, no raise.
    monkeypatch.delitem(sys.modules, "sgtk", raising=False)
    assert resolve_engine_project() is None
