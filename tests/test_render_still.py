"""Tests for the single-frame Arnold still recipe (``render_still.py``) and its
``maya_session`` wiring.

The recipe runs INSIDE Maya, so these tests inject a fake ``maya.cmds`` and drive
:func:`render_still.render_still` to completion, capturing the ``arnoldRender`` /
``renderWindowEditor`` calls. Like ``test_review_build``, the load-bearing part is
that the recipe (a) renders via Arnold and writes the Render View to the EXACT
out_path, (b) NEVER playblasts a viewport (the whole point — a playblast over a
live Arnold IPR panel hangs Maya), and (c) restores the render state it touched.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from maya_mcp import render_still
from maya_mcp.server import SessionAction

# Flags accepted by the two write commands on Maya 2027 (from cmds.help). If the
# recipe ever passes a bogus flag, the live bridge raises TypeError — this pins
# the real flag set so the mock cannot hide that (feedback_mock_paths_blindspot).
VALID_ARNOLDRENDER_FLAGS = {
    "camera", "cam", "width", "height", "batch", "seq", "layer", "mode",
    "waitForCompletion", "quiet",
}
VALID_RENDERWINDOWEDITOR_FLAGS = {
    "edit", "e", "query", "q", "writeImage", "snapshotMode", "displayImage",
    "camera", "clear", "control", "resetRegion", "refresh", "autoResize",
}


def _install_fake_maya(monkeypatch, calls):
    cmds = MagicMock(name="cmds")
    cmds.objExists.return_value = True
    cmds.pluginInfo.return_value = True   # mtoa "loaded"
    cmds.getPanel.return_value = None
    cmds.currentTime.side_effect = lambda *a, **k: 7.0 if k.get("query") else None
    cmds.getAttr.side_effect = lambda attr, **k: {
        "defaultRenderGlobals.currentRenderer": "mayaSoftware",
        "defaultResolution.width": 960,
        "defaultResolution.height": 540,
        "defaultArnoldDriver.aiTranslator": "exr",
    }.get(attr)

    def _arnold(*a, **k):
        calls["arnoldRender"].append(k)
    cmds.arnoldRender.side_effect = _arnold

    def _rwe(*a, **k):
        calls["renderWindowEditor"].append((a, k))
    cmds.renderWindowEditor.side_effect = _rwe

    def _setattr(attr, *a, **k):
        calls["setAttr"].append(attr)
    cmds.setAttr.side_effect = _setattr

    maya_mod = types.ModuleType("maya")
    maya_mod.cmds = cmds
    monkeypatch.setitem(sys.modules, "maya", maya_mod)
    monkeypatch.setitem(sys.modules, "maya.cmds", cmds)
    return cmds


def _run(monkeypatch, tmp_path):
    calls = {"arnoldRender": [], "renderWindowEditor": [], "setAttr": []}
    cmds = _install_fake_maya(monkeypatch, calls)
    # Pretend the write produced a file so the report is populated.
    out = str(tmp_path / "DJ_Model_still_v001.png")
    import os
    monkeypatch.setattr(os.path, "exists", lambda p: p == out or True)
    monkeypatch.setattr(os.path, "getsize", lambda p: 4096)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    report = render_still.render_still(out, camera="persp", frame=42,
                                       width=1280, height=720)
    return report, calls, cmds, out


def test_render_still_renders_arnold_and_writes_exact_path(tmp_path, monkeypatch):
    report, calls, cmds, out = _run(monkeypatch, tmp_path)
    # It rendered via Arnold and wrote the Render View to the EXACT out_path.
    assert calls["arnoldRender"], "render_still never called cmds.arnoldRender"
    assert calls["renderWindowEditor"], "render_still never wrote the Render View"
    wrote = [k.get("writeImage") for _, k in calls["renderWindowEditor"]]
    assert out in wrote, f"Render View not written to out_path; writeImage={wrote}"
    assert report["rendered"] == out
    assert report["error"] is None
    assert report["camera"] == "persp"


def test_render_still_never_playblasts(tmp_path, monkeypatch):
    _report, _calls, cmds, _out = _run(monkeypatch, tmp_path)
    # The whole point: a still must NOT playblast a viewport (that is what hangs
    # Maya over a live Arnold IPR panel). review_turntable playblasts; a still renders.
    assert not cmds.playblast.called, "render_still must never call playblast"


def test_render_still_passes_only_valid_flags(tmp_path, monkeypatch):
    _report, calls, _cmds, _out = _run(monkeypatch, tmp_path)
    bad_ar = {f for k in calls["arnoldRender"] for f in k if f not in VALID_ARNOLDRENDER_FLAGS}
    assert not bad_ar, f"arnoldRender got non-existent flags: {bad_ar}"
    bad_rwe = {f for _a, k in calls["renderWindowEditor"] for f in k
               if f not in VALID_RENDERWINDOWEDITOR_FLAGS}
    assert not bad_rwe, f"renderWindowEditor got non-existent flags: {bad_rwe}"


def test_render_still_restores_renderer(tmp_path, monkeypatch):
    _report, calls, _cmds, _out = _run(monkeypatch, tmp_path)
    # currentRenderer is both set to arnold and restored → appears at least twice.
    assert calls["setAttr"].count("defaultRenderGlobals.currentRenderer") >= 2, (
        "render_still must restore defaultRenderGlobals.currentRenderer in finally"
    )


def test_render_still_action_registered():
    assert SessionAction.RENDER_STILL.value == "render_still"


def test_do_render_still_requires_out_path():
    import asyncio
    import json
    from maya_mcp.server import _do_render_still
    out = asyncio.run(_do_render_still({}))
    data = json.loads(out)
    assert "error" in data and "out_path" in data["error"]


def test_render_still_returns_version_code_when_engined(tmp_path, monkeypatch):
    """When Maya is tk-maya engine'd, the report carries version_code {Asset}_{Task}
    so the caller can name the review Version after the task (same as turntable)."""
    calls = {"arnoldRender": [], "renderWindowEditor": [], "setAttr": []}
    _install_fake_maya(monkeypatch, calls)
    import os
    out = str(tmp_path / "still.png")
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os.path, "getsize", lambda p: 2048)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)

    # Fake a tk-maya engine context: entity DJ, task Model.
    fake_ctx = types.SimpleNamespace(
        entity={"name": "DJ"}, task={"name": "Model"}, step={"name": "Model"})
    fake_eng = types.SimpleNamespace(context=fake_ctx)
    sgtk_mod = types.ModuleType("sgtk")
    sgtk_mod.platform = types.SimpleNamespace(current_engine=lambda: fake_eng)
    monkeypatch.setitem(sys.modules, "sgtk", sgtk_mod)

    report = render_still.render_still(out, camera="persp")
    assert report["version_code"] == "DJ_Model"
    assert report["asset"] == "DJ" and report["task"] == "Model"
