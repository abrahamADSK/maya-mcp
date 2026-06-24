"""Regression guard for the console system prompt's Maya tool hierarchy.

The console hard-codes a tool inventory in ``build_system_prompt`` that is
injected via ``--append-system-prompt``. It drifted: it listed pre-dispatcher
flat names (``maya_execute_python``, ``maya_launch``, ``vision3d_health`` …) and
never mentioned the ``maya_session`` dispatcher, ``review_turntable`` or
``maya_worldlabs``. So the console LLM, told to "review turntable", improvised a
playblast with ``execute_python`` — which produced an empty frame and bloated
the request — instead of calling the deterministic ``maya_session
action=review_turntable``. This test pins the hierarchy to the real tools.
"""

from __future__ import annotations

import sys
import types

# ── PySide stub (see tests/test_effort_selector.py) ──────────────────────────
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

from console.claude_worker import build_system_prompt  # noqa: E402


def _maya_prompt() -> str:
    return build_system_prompt({"maya-mcp": {}, "fpt-mcp": {}})


def test_prompt_lists_current_maya_dispatcher_hierarchy():
    p = _maya_prompt()
    # the dispatcher + the actions that were missing and caused improvisation
    assert "maya_session" in p
    assert "review_turntable" in p
    assert "action=publish" in p
    assert "maya_worldlabs" in p
    assert "search_maya_docs" in p


def test_prompt_drops_stale_flat_tool_names():
    p = _maya_prompt()
    # pre-dispatcher flat names that no longer exist as standalone tools — their
    # presence is what mislead the LLM toward execute_python.
    for stale in ("maya_execute_python", "maya_list_scene", "maya_new_scene",
                  "vision3d_health", "shape_generate_remote"):
        assert stale not in p, f"stale flat tool name still in system prompt: {stale}"


def test_prompt_forbids_hand_built_playblast():
    p = _maya_prompt().lower()
    # the behavioural rule that prevents the empty-frame / main-thread-hang bug
    assert "never" in p and "playblast" in p
    assert "review_turntable" in p
