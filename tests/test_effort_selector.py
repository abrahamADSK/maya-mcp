"""Tests for the console effort-level selector (build_backend_env).

Background
----------
The Maya Console panel exposes an effort selector mirroring the model
selector: ``Auto / Low / Medium / High / Max``, default ``Auto`` (index 0).

For the spawned ``claude`` subprocess env:
  - ``effort == "auto"`` → BOTH hardening env vars must be ABSENT, so the
    CLI uses its adaptive-thinking default. ``build_backend_env`` adds
    nothing; ``ClaudeWorker.run`` additionally strips any inherited value.
  - ``effort in {low, medium, high, max}`` →
    ``CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING="1"`` +
    ``CLAUDE_CODE_EFFORT_LEVEL=<effort>``.

These tests are fully offline: they only exercise the pure-Python
``build_backend_env`` helper and the module-level constants.
"""

from __future__ import annotations

import sys
import types

import pytest


# ── PySide stub ───────────────────────────────────────────────────────────────
# console.qt_compat tries PySide6 then PySide2; both are absent in CI/headless
# test environments. Install a minimal stub BEFORE importing claude_worker so
# the module-level `from .qt_compat import QtCore` succeeds. The helper we
# exercise here is plain-Python — it does not touch Qt at runtime.

if "PySide6" not in sys.modules and "PySide2" not in sys.modules:
    _pyside6 = types.ModuleType("PySide6")
    _qtcore = types.ModuleType("PySide6.QtCore")
    _qtwidgets = types.ModuleType("PySide6.QtWidgets")
    _qtgui = types.ModuleType("PySide6.QtGui")

    class _QThreadStub:
        def __init__(self, *a, **kw) -> None: ...
        def start(self) -> None: ...

    class _SignalStub:
        def __init__(self, *a, **kw) -> None: ...
        def connect(self, *a, **kw) -> None: ...
        def emit(self, *a, **kw) -> None: ...

    _qtcore.QThread = _QThreadStub
    _qtcore.Signal = _SignalStub
    _pyside6.QtCore = _qtcore
    _pyside6.QtWidgets = _qtwidgets
    _pyside6.QtGui = _qtgui
    sys.modules["PySide6"] = _pyside6
    sys.modules["PySide6.QtCore"] = _qtcore
    sys.modules["PySide6.QtWidgets"] = _qtwidgets
    sys.modules["PySide6.QtGui"] = _qtgui

    _shiboken6 = types.ModuleType("shiboken6")
    _shiboken6.wrapInstance = lambda *a, **kw: None
    sys.modules["shiboken6"] = _shiboken6

from console.claude_worker import (  # noqa: E402
    AVAILABLE_EFFORTS,
    DEFAULT_EFFORT,
    build_backend_env,
)


_DISABLE = "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"
_LEVEL = "CLAUDE_CODE_EFFORT_LEVEL"


def test_default_effort_is_auto() -> None:
    """Default must be ``"auto"`` and the first combo entry must be ("Auto", "auto")."""
    assert DEFAULT_EFFORT == "auto"
    assert AVAILABLE_EFFORTS[0] == ("Auto", "auto")


def test_available_efforts_values() -> None:
    """The selector must expose exactly Auto/Low/Medium/High/Max in order."""
    assert [value for _, value in AVAILABLE_EFFORTS] == [
        "auto",
        "low",
        "medium",
        "high",
        "max",
    ]


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_fixed_levels_set_hardening_vars(effort: str) -> None:
    """Fixed levels force adaptive thinking off at the requested effort."""
    env = build_backend_env("claude-opus-4-8", "anthropic", effort)
    assert env[_DISABLE] == "1"
    assert env[_LEVEL] == effort


def test_auto_omits_hardening_vars() -> None:
    """``effort="auto"`` must add NEITHER hardening key to the env dict."""
    env = build_backend_env("claude-opus-4-8", "anthropic", "auto")
    assert _DISABLE not in env
    assert _LEVEL not in env


def test_default_effort_omits_hardening_vars() -> None:
    """The default (no effort arg) must behave like ``auto`` — no hardening keys."""
    env = build_backend_env("claude-opus-4-8", "anthropic")
    assert _DISABLE not in env
    assert _LEVEL not in env
