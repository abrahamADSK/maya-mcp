"""Tests for the deterministic review-turntable recipe (``review_build.py``).

The recipe runs INSIDE Maya, so these tests inject a fake ``maya.cmds`` and
drive :func:`review_build.review_turntable` to completion, capturing the kwargs
of the *write* ``playblast`` call.

The load-bearing assertion is the **valid-flag whitelist**: every keyword the
recipe passes to ``cmds.playblast`` must be a real flag of the command. A plain
mock would happily accept a bogus flag (the Maya bridge does NOT — it raised
``TypeError: Invalid flag 'maintainRatio'`` in-vivo on Maya 2027, which the
v1.18.0 release shipped). Encoding the real flag set here is what closes that
mock blindspot (memory ``feedback_mock_paths_blindspot``).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from maya_mcp import review_build

# Long-flag names accepted by ``cmds.playblast`` in Maya 2027, taken verbatim
# from ``cmds.help('playblast')`` on a live box. ``maintainRatio`` is NOT here —
# that is the whole point of the guard.
VALID_PLAYBLAST_FLAGS = {
    "query", "activeEditor", "compression", "clearCache", "completeFilename",
    "codecOptions", "cameraSetup", "combineSound", "editorPanelName", "endTime",
    "exposure", "filename", "format", "forceOverwrite", "framePadding", "frame",
    "gamma", "height", "indexFromZero", "options", "showOrnaments", "offScreen",
    "offScreenViewportUpdate", "percent", "partialSave", "quality",
    "replaceAudioOnly", "replaceEndTime", "replaceFilename", "rawFrameNumbers",
    "replaceStartTime", "sound", "saveDepth", "sequenceTime", "startTime",
    "throwOnError", "useSequencerSounds", "useTraxSounds", "viewer", "width",
    "widthHeight",
}


def _install_fake_maya(monkeypatch, record):
    """Inject a fake ``maya.cmds`` and record the write-``playblast`` kwargs."""
    cmds = MagicMock(name="cmds")
    cmds.ls.return_value = []
    cmds.exactWorldBoundingBox.return_value = [-1.0, 0.0, -1.0, 1.0, 5.0, 1.0]
    # query returns the prior time unit; the set-call returns nothing.
    cmds.currentUnit.side_effect = lambda *a, **k: "film" if k.get("query") else None
    cmds.camera.return_value = ("reviewTurntableCam", "reviewTurntableCamShape")
    cmds.group.return_value = "reviewTurntablePivot"
    cmds.getAttr.return_value = 1.417  # horizontalFilmAperture
    cmds.getPanel.side_effect = (
        lambda *a, **k: ["modelPanel4"] if k.get("type") == "modelPanel" else "modelPanel4"
    )

    def _model_editor(*a, **k):
        if k.get("query") and k.get("camera"):
            return "persp"
        if k.get("query") and k.get("rendererName"):
            return "vp2Renderer"
        return None

    cmds.modelEditor.side_effect = _model_editor
    cmds.objExists.return_value = True

    # Dedicated temporary VP2.0 window (Chat 77): create returns the name, an
    # `exists=True` query returns False (so nothing is pre-deleted / the finally
    # is a no-op in the fake), and the throw-away modelPanel create returns a
    # stable name while its edit calls return nothing.
    def _window(*a, **k):
        if k.get("exists"):
            return False
        return a[0] if a else "reviewTurntableWin"

    cmds.window.side_effect = _window

    def _model_panel(*a, **k):
        if k.get("edit"):
            return None
        return "reviewTurntablePanel"

    cmds.modelPanel.side_effect = _model_panel

    def _playblast(*a, **k):
        if k.get("query"):
            if k.get("format"):
                return ["qt", "avfoundation", "image"]
            if k.get("compression"):
                return ["H.264"]
            return []
        record["pb"] = dict(k)  # the actual write call
        return k.get("filename")

    cmds.playblast.side_effect = _playblast

    maya_mod = types.ModuleType("maya")
    maya_mod.cmds = cmds
    monkeypatch.setitem(sys.modules, "maya", maya_mod)
    monkeypatch.setitem(sys.modules, "maya.cmds", cmds)
    return cmds


def test_review_turntable_passes_only_valid_playblast_flags(tmp_path, monkeypatch):
    record: dict = {}
    _install_fake_maya(monkeypatch, record)
    out = str(tmp_path / "tt.mov")

    res = review_build.review_turntable(
        out, start=1, end=48, fps=25, width=1920, height=1080, objects=["smoke"]
    )

    # completed without raising and returned the deliverable
    assert res["mov"] == out
    assert res["resolution"] == [1920, 1080]
    assert res["fps"] == 25

    pb = record["pb"]
    # REGRESSION GUARD: maintainRatio is not a real cmds.playblast flag (Maya 2027).
    assert "maintainRatio" not in pb
    bad = set(pb) - VALID_PLAYBLAST_FLAGS
    assert not bad, f"review_turntable passed non-existent playblast flags: {bad}"


def test_review_turntable_uses_temp_vp2_window(tmp_path, monkeypatch):
    """Captures from a dedicated temporary VP2.0 window, not the docked viewport.

    A fresh window has no Arnold IPR / render-override attached, so the playblast
    cannot capture an Arnold render (its HUD burned into the buffer — Chat 77),
    and it gives a clean 16:9 source (no stretch). offScreen stays False so the
    visible window keeps a valid render context (the offScreen buffer drew nothing
    when Maya was occluded → empty .mov, Chat 74); editorPanelName is pinned to the
    temp panel so the capture is exactly that VP2.0 window.
    """
    record: dict = {}
    cmds = _install_fake_maya(monkeypatch, record)
    out = str(tmp_path / "tt.mov")

    review_build.review_turntable(
        out, start=1, end=48, fps=25, width=1920, height=1080, objects=["smoke"]
    )

    # a throw-away window + model panel was created and pinned to VP2.0
    assert cmds.window.called
    assert cmds.modelPanel.called
    assert any(
        kw.get("rendererName") == "vp2Renderer"
        for _, kw in cmds.modelEditor.call_args_list
    )

    pb = record["pb"]
    assert pb["offScreen"] is False
    assert pb["editorPanelName"] == "reviewTurntablePanel"  # the temp window panel
    assert pb["widthHeight"] == (1920, 1080)
    assert pb["filename"] == out


def test_review_turntable_errors_on_empty_scene(tmp_path, monkeypatch):
    """No geometry and no selection → a clean error, not a crash."""
    record: dict = {}
    cmds = _install_fake_maya(monkeypatch, record)
    cmds.ls.return_value = []  # nothing selected, no meshes, no assemblies
    out = str(tmp_path / "tt.mov")

    res = review_build.review_turntable(out, objects=None)
    assert "error" in res
    assert "pb" not in record  # never reached the playblast


def test_review_turntable_frames_meshes_not_lights(tmp_path, monkeypatch):
    """Default framing (no objects/selection) frames the MESH geometry, NOT a
    huge non-geometry assembly. Chat 72: the DJ asset was framed together with a
    GI_skydome (aiSkyDomeLight, ~±1000 bbox), which shrank the model to a speck →
    an empty-looking turntable. The bounding box must come from the meshes only.
    """
    record: dict = {}
    cmds = _install_fake_maya(monkeypatch, record)

    def _ls(*a, **k):
        if k.get("type") == "mesh":
            return ["|DJ:Mesh|DJ:MeshShape"]
        if k.get("assemblies"):
            # the skydome transform IS a top-level assembly — must be ignored
            return ["DJ:Mesh", "transform1", "persp", "top", "front", "side"]
        return []  # nothing selected

    cmds.ls.side_effect = _ls
    cmds.listRelatives.side_effect = (
        lambda *a, **k: ["|DJ:Mesh"] if k.get("parent") else None
    )
    framed: dict = {}

    def _bbox(objs):
        framed["objs"] = objs
        return [-0.7, 0.0, -0.4, 0.7, 2.0, 0.4]

    cmds.exactWorldBoundingBox.side_effect = _bbox

    review_build.review_turntable(
        str(tmp_path / "tt.mov"), start=1, end=4, fps=25,
        width=1920, height=1080, objects=None,
    )

    assert "|DJ:Mesh" in framed["objs"]
    assert "transform1" not in framed["objs"]  # the skydome must NOT drive framing


def test_review_turntable_filters_nongeometry_selection(tmp_path, monkeypatch):
    """Chat 74 crash: a SELECTED non-geometry node (``tmpArnoldMayaUsdOptions``,
    an Arnold/USD options node) has the empty world-bbox sentinel; a camera built
    from it sits at ~1e20 and crashed the onscreen playblast. The recipe must
    filter the non-geometry selection out, and — with no renderable mesh anywhere
    — refuse cleanly WITHOUT reaching the playblast.
    """
    record: dict = {}
    cmds = _install_fake_maya(monkeypatch, record)

    def _ls(*a, **k):
        if k.get("sl"):
            return ["tmpArnoldMayaUsdOptions"]   # the selected options node
        if k.get("type") == "mesh":
            return []                            # node is no mesh; scene has none
        return []

    cmds.ls.side_effect = _ls
    cmds.listRelatives.return_value = None       # no mesh descendants anywhere

    res = review_build.review_turntable(str(tmp_path / "tt.mov"), objects=None)
    assert "error" in res
    assert "pb" not in record                    # NEVER reached the playblast → no crash


def test_review_turntable_refuses_degenerate_bbox(tmp_path, monkeypatch):
    """Crash-proofing backstop: even if something is framed, an empty bbox
    sentinel ``[1e20…-1e20]`` (max < min) must be refused before the camera is
    built — that camera is what crashed Maya's playblast (Chat 74)."""
    record: dict = {}
    cmds = _install_fake_maya(monkeypatch, record)
    # ``geo`` passes the mesh filter (default listRelatives MagicMock is truthy),
    # but exactWorldBoundingBox returns the empty sentinel.
    cmds.exactWorldBoundingBox.return_value = [1e20, 1e20, 1e20, -1e20, -1e20, -1e20]

    res = review_build.review_turntable(str(tmp_path / "tt.mov"), objects=["geo"])
    assert "error" in res
    assert "degenerate" in res["error"].lower()
    assert "pb" not in record                    # never reached the playblast


def test_review_turntable_restores_playback_clock(tmp_path, monkeypatch):
    """The recipe must restore the current frame AND the playback range so it
    never leaves the scene on the end frame. Leaving it there re-evaluated a
    model's own keyframes and displaced an unkeyed manual pose (feet on the
    ground reverted to its keyed value) → the asset appeared moved after every
    turntable (Chat 79). The orbit keys live only on the throw-away pivot, so the
    recipe leaves NO keyframes behind; restoring the clock leaves the model as
    found.
    """
    record: dict = {}
    cmds = _install_fake_maya(monkeypatch, record)
    cmds.currentTime.side_effect = lambda *a, **k: 7.0 if k.get("query") else None
    cmds.playbackOptions.side_effect = lambda *a, **k: 5.0 if k.get("query") else None

    review_build.review_turntable(
        str(tmp_path / "tt.mov"), start=1, end=48, fps=25, objects=["smoke"]
    )

    # the original frame (7.0) was restored after the playblast
    assert any(c.args and c.args[0] == 7.0 for c in cmds.currentTime.call_args_list), \
        "currentTime was not restored to the original frame"
    # the original playback range (5.0) was restored — a non-query set call
    assert any(
        not kw.get("query") and kw.get("minTime") == 5.0
        for _, kw in cmds.playbackOptions.call_args_list
    ), "playback range was not restored"
