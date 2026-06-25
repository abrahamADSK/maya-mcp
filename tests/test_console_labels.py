"""Tests for the console's action-aware progress labels.

The Qt console turns each streamed tool call into a human-friendly progress
line. Dispatcher tools (``maya_session`` / ``maya_vision3d`` / ``maya_worldlabs``)
take an ``action`` param, so the label must refine by ``(tool, action)`` — e.g.
``maya_vision3d action=poll`` → "Polling Vision3D progress" — so a long poll /
publish / turntable loop keeps a specific heartbeat in the thinking bubble.
When the action has not yet streamed in, it must fall back to a flat label
(never crash). These are offline: pure-Python label logic, no Qt at runtime.
"""

from __future__ import annotations

import sys
import types

# ── PySide stub (see test_effort_selector.py) ────────────────────────────────
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

from console.claude_worker import ClaudeWorker  # noqa: E402


def _worker() -> ClaudeWorker:
    return ClaudeWorker("hi")


def test_dispatcher_action_refines_label():
    w = _worker()
    assert (
        w._label_for_tool("mcp__maya-mcp__maya_vision3d", {"action": "poll"})
        == "Polling Vision3D progress"
    )
    assert (
        w._label_for_tool("mcp__maya-mcp__maya_session", {"action": "review_turntable"})
        == "Rendering review turntable"
    )
    assert (
        w._label_for_tool("mcp__maya-mcp__maya_session", {"action": "publish"})
        == "Publishing asset (native Toolkit)"
    )
    assert (
        w._label_for_tool("mcp__maya-mcp__maya_worldlabs", {"action": "build"})
        == "Building World Labs environment in Maya"
    )


def test_dispatcher_without_known_action_falls_back_to_flat_label():
    w = _worker()
    # action not streamed in yet → no crash, a generic label
    label = w._label_for_tool("mcp__maya-mcp__maya_session", None)
    assert isinstance(label, str) and label
    # unknown action → also falls back, not a KeyError
    label2 = w._label_for_tool("mcp__maya-mcp__maya_session", {"action": "zzz"})
    assert isinstance(label2, str) and label2


def test_short_tool_name_strips_mcp_prefix():
    w = _worker()
    assert w._short_tool_name("mcp__maya-mcp__maya_session") == "maya_session"
    assert w._short_tool_name("mcp__fpt-mcp__sg_find") == "sg_find"
    assert w._short_tool_name("maya_session") == "maya_session"
