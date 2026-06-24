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


def test_review_turntable_pins_offscreen_vp2_and_resolution(tmp_path, monkeypatch):
    """The anti-hang guarantees: offScreen + the captured panel pinned + 16:9."""
    record: dict = {}
    _install_fake_maya(monkeypatch, record)
    out = str(tmp_path / "tt.mov")

    review_build.review_turntable(
        out, start=1, end=48, fps=25, width=1920, height=1080, objects=["smoke"]
    )

    pb = record["pb"]
    assert pb["offScreen"] is True
    assert pb["editorPanelName"] == "modelPanel4"
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
