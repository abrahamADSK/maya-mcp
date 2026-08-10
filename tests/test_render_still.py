"""Tests for the single-frame Arnold still recipe (``render_still.py``) and its
``maya_session`` wiring.

Architecture under test (Chat 94): Maya only EXPORTS a ``.ass`` with the colour
policy and the output path baked in; ``kick`` renders it out of process. The
load-bearing properties are that the recipe (a) never renders in Maya — no
``arnoldRender``, no Render View dump, since that path writes a scene-linear
file and ignores colour management (measured 127 vs 188 on a flat 0.5 patch),
(b) aims Arnold at the EXACT out_path with no frame padding, (c) restores every
attribute it touches, and (d) that the server invokes kick the way the
catcher-passes skill settled on.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from maya_mcp import render_still
from maya_mcp.server import SessionAction

# Flags accepted by arnoldExportAss on Maya 2027. `mask`, `startFrame`/`sf` and
# `endFrame`/`ef` are deliberately NOT used: they make mtoa write a
# frame-numbered .ass and drop nodes the scene still references (the denoiser),
# which aborts kick — Chat 94.
VALID_EXPORTASS_FLAGS = {
    "f", "filename", "cam", "camera", "selected", "s",
    "lightLinks", "shadowLinks", "boundingBox", "expandProcedurals",
    "fullPath", "compressed", "asciiAss", "forceTranslateShadingEngines",
}
FORBIDDEN_EXPORTASS_FLAGS = {"mask", "startFrame", "sf", "endFrame", "ef", "frameStep", "fs"}

# Chat 94 regression: the recipe used to pass outputTarget="renderView", which
# Maya REJECTS — "outputTarget value can either be 'renderer' or 'playblast'".
# Behind a bare except that made the whole review view transform a silent no-op
# and the mocks never noticed, so the accepted VALUES are pinned here too
# (feedback_mock_paths_blindspot / feedback_silent_except_fabricated_story).
VALID_OUTPUT_TARGETS = {"renderer", "playblast"}
VALID_CMPREFS_FLAGS = {
    "edit", "e", "query", "q", "cmEnabled", "outputTransformEnabled",
    "outputTransformName", "outputTransformNames", "outputTarget",
    "viewTransformName", "viewTransformNames", "renderingSpaceName",
}

FAKE_PLUGIN = "/opt/Autodesk/Arnold/mtoa/2027/plug-ins/mtoa.bundle"
FAKE_KICK = "/opt/Autodesk/Arnold/mtoa/2027/bin/kick"


def _install_fake_maya(monkeypatch, calls):
    cmds = MagicMock(name="cmds")
    cmds.objExists.return_value = True
    cmds.getPanel.return_value = None
    cmds.currentTime.side_effect = lambda *a, **k: 7.0 if k.get("query") else None

    def _plugininfo(_name, **k):
        return FAKE_PLUGIN if k.get("path") else True
    cmds.pluginInfo.side_effect = _plugininfo

    cmds.getAttr.side_effect = lambda attr, **k: {
        "defaultRenderGlobals.currentRenderer": "mayaSoftware",
        "defaultRenderGlobals.imageFilePrefix": "previousPrefix",
        "defaultRenderGlobals.animation": 1,
        "defaultArnoldDriver.aiTranslator": "exr",
        "defaultArnoldDriver.colorManagement": 0,
    }.get(attr)

    cmds.attributeQuery.return_value = ["Raw:Use View Transform:Use Output Transform"]

    def _export(*a, **k):
        calls["arnoldExportAss"].append(k)
    cmds.arnoldExportAss.side_effect = _export

    def _setattr(attr, *a, **k):
        calls["setAttr"].append((attr, a[0] if a else None))
    cmds.setAttr.side_effect = _setattr

    def _cmprefs(*a, **k):
        calls.setdefault("colorManagementPrefs", []).append(k)
        if k.get("query") or k.get("q"):
            if k.get("outputTransformNames"):
                return ["ACES 1.0 SDR-video (sRGB)", "Un-tone-mapped (sRGB)"]
            if k.get("outputTransformName"):
                return "ACES 1.0 SDR-video (sRGB)"
            if k.get("outputTransformEnabled"):
                return False
        return None
    cmds.colorManagementPrefs.side_effect = _cmprefs

    maya_mod = types.ModuleType("maya")
    maya_mod.cmds = cmds
    monkeypatch.setitem(sys.modules, "maya", maya_mod)
    monkeypatch.setitem(sys.modules, "maya.cmds", cmds)
    return cmds


def _first_set(calls, attr):
    """Value of the FIRST setAttr on ``attr`` — i.e. the mutation.

    A plain dict() of the calls would report the LAST value, which is the
    restore written by the finally block, so it would silently assert nothing.
    """
    for a, v in calls["setAttr"]:
        if a == attr:
            return v
    return None


def _run(monkeypatch, tmp_path, view_transform="Un-tone-mapped (sRGB)"):
    calls = {"arnoldExportAss": [], "setAttr": []}
    cmds = _install_fake_maya(monkeypatch, calls)
    out = str(tmp_path / "DJ_Model_still_v001.png")
    ass = str(tmp_path / "still.ass")
    import os
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    report = render_still.export_still_ass(out, ass, camera="persp", frame=42,
                                           view_transform=view_transform)
    return report, calls, cmds, out, ass


def test_export_writes_the_ass_for_the_requested_camera_and_frame(tmp_path, monkeypatch):
    report, calls, cmds, _out, ass = _run(monkeypatch, tmp_path)
    assert report["error"] is None
    assert report["ass"] == ass
    assert report["camera"] == "persp"
    assert calls["arnoldExportAss"], "the recipe must export a .ass"
    kwargs = calls["arnoldExportAss"][0]
    assert kwargs.get("f") == ass
    assert kwargs.get("cam") == "persp"
    cmds.currentTime.assert_any_call(42.0)


def test_recipe_never_renders_inside_maya(tmp_path, monkeypatch):
    """The whole point of Chat 94: no arnoldRender, no Render View dump.

    Dumping the Render View writes a scene-linear file and ignores colour
    management, and it opens a Qt window on a box that crashes on hide.
    """
    _report, _calls, cmds, _out, _ass = _run(monkeypatch, tmp_path)
    cmds.arnoldRender.assert_not_called()
    cmds.renderWindowEditor.assert_not_called()
    cmds.playblast.assert_not_called()


def test_exportass_flags_are_real_and_avoid_the_frame_numbering_trap(tmp_path, monkeypatch):
    _report, calls, _cmds, _out, _ass = _run(monkeypatch, tmp_path)
    used = {f for k in calls["arnoldExportAss"] for f in k}
    bad = used - VALID_EXPORTASS_FLAGS
    assert not bad, f"arnoldExportAss got non-existent flags: {bad}"
    forbidden = used & FORBIDDEN_EXPORTASS_FLAGS
    assert not forbidden, (
        f"arnoldExportAss must not be given {forbidden} — they make mtoa write a "
        f"frame-numbered .ass and drop referenced nodes (Chat 94)"
    )


def test_output_lands_on_the_exact_path_with_no_frame_padding(tmp_path, monkeypatch):
    """Arnold builds the name from imageFilePrefix + the translator extension."""
    _report, calls, _cmds, out, _ass = _run(monkeypatch, tmp_path)
    assert _first_set(calls, "defaultRenderGlobals.imageFilePrefix") == out[: -len(".png")]
    assert _first_set(calls, "defaultArnoldDriver.aiTranslator") == "png"
    # animation OFF, else Maya appends frame padding and the file misses out_path
    anim = [v for a, v in calls["setAttr"] if a == "defaultRenderGlobals.animation"]
    assert anim and anim[0] == 0, "animation must be switched off for a single still"


def test_jpg_out_path_selects_the_jpeg_translator(tmp_path, monkeypatch):
    calls = {"arnoldExportAss": [], "setAttr": []}
    _install_fake_maya(monkeypatch, calls)
    import os
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    render_still.export_still_ass(str(tmp_path / "s.jpg"), str(tmp_path / "s.ass"))
    assert _first_set(calls, "defaultArnoldDriver.aiTranslator") == "jpeg"


def test_kick_is_resolved_from_the_loaded_plugin_not_hardcoded(tmp_path, monkeypatch):
    report, _calls, _cmds, _out, _ass = _run(monkeypatch, tmp_path)
    assert report["kick"] == FAKE_KICK


def test_colormanagementprefs_never_gets_a_bogus_output_target(tmp_path, monkeypatch):
    """Chat 94 regression — the bug that shipped.

    ``outputTarget="renderView"`` is not a thing: Maya accepts only "renderer"
    and "playblast" and RAISES otherwise. The raise was swallowed by a bare
    except, so the review view transform silently never applied while the mock
    suite stayed green. Pin both the flags and the accepted values.
    """
    _report, calls, _cmds, _out, _ass = _run(monkeypatch, tmp_path)
    cm_calls = calls.get("colorManagementPrefs", [])
    assert cm_calls, "the recipe must bake colour management for a preview still"

    bad_flags = {f for k in cm_calls for f in k if f not in VALID_CMPREFS_FLAGS}
    assert not bad_flags, f"colorManagementPrefs got non-existent flags: {bad_flags}"

    bad_targets = {k["outputTarget"] for k in cm_calls
                   if "outputTarget" in k and k["outputTarget"] not in VALID_OUTPUT_TARGETS}
    assert not bad_targets, (
        f"colorManagementPrefs got rejected outputTarget value(s): {bad_targets} — "
        f"Maya accepts only {sorted(VALID_OUTPUT_TARGETS)}"
    )


def test_driver_is_told_to_use_the_output_transform(tmp_path, monkeypatch):
    """Mapped BY NAME — the enum indices vary by mtoa version."""
    _report, calls, _cmds, _out, _ass = _run(monkeypatch, tmp_path)
    assert _first_set(calls, "defaultArnoldDriver.colorManagement") == 2  # Use Output Transform


def test_reports_whether_the_view_transform_applied(tmp_path, monkeypatch):
    """A silent colour failure must be visible in the report, never assumed."""
    report, _calls, _cmds, _out, _ass = _run(monkeypatch, tmp_path)
    assert report["view_transform_applied"] is True

    # When Maya does NOT list the requested view, the flag stays False instead
    # of the recipe pretending it pinned it.
    unlisted, _c, _cm, _o, _a = _run(monkeypatch, tmp_path,
                                     view_transform="No Such View")
    assert unlisted["view_transform_applied"] is False


def test_restores_everything_it_mutates(tmp_path, monkeypatch):
    """The docstring promises the scene is left as found — pin each attribute.

    Verified in-vivo (Chat 94) by seeding a distinct pre-state and diffing the
    snapshotted fields back; this is the offline guard for the same claim.
    """
    _report, calls, cmds, _out, _ass = _run(monkeypatch, tmp_path)
    attrs = [a for a, _v in calls["setAttr"]]
    for attr in ("defaultRenderGlobals.currentRenderer",
                 "defaultRenderGlobals.imageFilePrefix",
                 "defaultRenderGlobals.animation",
                 "defaultArnoldDriver.aiTranslator",
                 "defaultArnoldDriver.colorManagement"):
        assert attrs.count(attr) >= 2, f"{attr} is mutated but never restored in finally"
    # the frame is put back too
    cmds.currentTime.assert_any_call(7.0)
    # and colour management is restored, not left pinned on the user's scene
    cm_edits = [k for k in calls.get("colorManagementPrefs", [])
                if k.get("edit") and ("outputTransformEnabled" in k
                                      or "outputTransformName" in k)]
    assert len(cm_edits) >= 3, "colour management must be restored in finally"


def test_render_still_action_registered():
    assert SessionAction.RENDER_STILL.value == "render_still"


def test_do_render_still_requires_out_path():
    import asyncio
    import json
    from maya_mcp.server import _do_render_still
    out = asyncio.run(_do_render_still({}))
    data = json.loads(out)
    assert "error" in data and "out_path" in data["error"]


def test_returns_version_code_when_engined(tmp_path, monkeypatch):
    """When Maya is tk-maya engine'd, the report carries version_code {Asset}_{Task}
    so the caller can name the review Version after the task (same as turntable)."""
    calls = {"arnoldExportAss": [], "setAttr": []}
    _install_fake_maya(monkeypatch, calls)
    import os
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)

    fake_ctx = types.SimpleNamespace(
        entity={"name": "DJ"}, task={"name": "Model"}, step={"name": "Model"})
    fake_eng = types.SimpleNamespace(context=fake_ctx)
    sgtk_mod = types.ModuleType("sgtk")
    sgtk_mod.platform = types.SimpleNamespace(current_engine=lambda: fake_eng)
    monkeypatch.setitem(sys.modules, "sgtk", sgtk_mod)

    report = render_still.export_still_ass(str(tmp_path / "s.png"),
                                           str(tmp_path / "s.ass"), camera="persp")
    assert report["version_code"] == "DJ_Model"
    assert report["asset"] == "DJ" and report["task"] == "Model"


def test_server_invokes_kick_the_proven_way(tmp_path, monkeypatch):
    """kick argv regression: -dw -dp, and NEVER -o.

    imageFilePrefix is baked into the .ass; passing -o on top made kick loop
    re-rendering until the call timed out (Chat 94). The invocation mirrors the
    catcher-passes skill's.
    """
    import asyncio
    import json
    import os
    from maya_mcp import server

    out = str(tmp_path / "still.png")
    ass = str(tmp_path / "tmp.ass")
    report = {"ass": ass, "kick": FAKE_KICK, "error": None, "camera": "persp",
              "frame": 1.0, "view_transform_applied": True, "asset": None,
              "task": None, "version_code": None}

    async def _fake_exec(_code, _ctx, _label, **_kw):
        return json.dumps(report)
    captured = {}

    async def _fake_run(cmd, timeout=60):
        captured["argv"] = cmd
        return 0, "", ""

    monkeypatch.setattr(server, "_execute_with_heartbeat", _fake_exec)
    monkeypatch.setattr(server, "_run_cmd", _fake_run)
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os.path, "getsize", lambda p: 200_000)
    monkeypatch.setattr(os, "remove", lambda p: None)

    data = json.loads(asyncio.run(server._do_render_still(
        {"out_path": out, "width": 960, "height": 540, "aa_samples": 4})))

    argv = captured["argv"]
    assert argv[0] == FAKE_KICK
    assert "-o" not in argv, "kick must not get -o — imageFilePrefix is baked in the .ass"
    for flag in ("-dw", "-dp"):
        assert flag in argv, f"kick must be invoked with {flag}"
    assert argv[argv.index("-as") + 1] == "4"
    assert argv[argv.index("-r") + 1: argv.index("-r") + 3] == ["960", "540"]
    assert data["rendered"] == out
    assert data["size_kb"] == 195


def test_server_reports_when_kick_writes_nothing(tmp_path, monkeypatch):
    """A silent failure must surface as an error, not as a bogus success."""
    import asyncio
    import json
    import os
    from maya_mcp import server

    ass = str(tmp_path / "tmp.ass")
    report = {"ass": ass, "kick": FAKE_KICK, "error": None}

    async def _fake_exec(_code, _ctx, _label, **_kw):
        return json.dumps(report)

    async def _fake_run(_cmd, timeout=60):
        return 1, "", "ERROR | licence check failed"

    monkeypatch.setattr(server, "_execute_with_heartbeat", _fake_exec)
    monkeypatch.setattr(server, "_run_cmd", _fake_run)
    monkeypatch.setattr(os.path, "exists", lambda p: p.endswith(".ass"))
    monkeypatch.setattr(os, "remove", lambda p: None)

    data = json.loads(asyncio.run(server._do_render_still(
        {"out_path": str(tmp_path / "nope.png")})))
    assert "error" in data and "no file" in data["error"]
    assert "licence" in data["kick_stderr"]
