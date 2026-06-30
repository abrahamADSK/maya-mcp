"""Tests for the server-side PNG→.mov fallback assembler (``_review_encode``).

When Maya's movie encoder is unavailable, ``review_turntable`` writes a PNG
sequence; the server then assembles the ``.mov`` with ffmpeg so the tool always
returns a ``.mov`` (Chat 79). These tests cover the pure helpers (fallback
detection + ffmpeg arg construction) and the best-effort guards of the
assembler, with ffmpeg / the filesystem / the subprocess all mocked — no real
ffmpeg or Maya needed.
"""

from __future__ import annotations

from maya_mcp import _review_encode


def test_is_png_fallback_detects_image_format():
    assert _review_encode.is_png_fallback({"format": {"format": "image", "compression": "png"}})
    assert not _review_encode.is_png_fallback({"format": {"format": "qt"}})
    assert not _review_encode.is_png_fallback({"format": {"format": "avfoundation"}})
    # an error result is never a successful PNG fallback
    assert not _review_encode.is_png_fallback({"error": "boom", "format": {"format": "image"}})
    assert not _review_encode.is_png_fallback({})
    assert not _review_encode.is_png_fallback("not a dict")


def test_png_base_strips_extension():
    assert _review_encode.png_base("/a/b/DJ_Model_v001.mov") == "/a/b/DJ_Model_v001"


def test_ffmpeg_mov_cmd_builds_expected_args():
    cmd = _review_encode.ffmpeg_mov_cmd("ffmpeg", "/o/tt.mov", start=1, end=100, fps=25)
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-framerate") + 1] == "25"
    assert cmd[cmd.index("-start_number") + 1] == "1"
    assert cmd[cmd.index("-i") + 1] == "/o/tt.%04d.png"
    assert cmd[cmd.index("-frames:v") + 1] == "100"     # end - start + 1
    assert "yuv420p" in cmd
    assert cmd[-1] == "/o/tt.mov"


def test_assemble_returns_false_when_ffmpeg_absent(monkeypatch):
    monkeypatch.setattr(_review_encode.shutil, "which", lambda _x: None)
    assert _review_encode.assemble_mov_from_pngs("/o/tt.mov", 1, 100, 25) is False


def test_assemble_returns_false_when_first_frame_missing(monkeypatch):
    monkeypatch.setattr(_review_encode.shutil, "which", lambda _x: "/usr/bin/ffmpeg")
    monkeypatch.setattr(_review_encode.os.path, "exists", lambda _p: False)
    assert _review_encode.assemble_mov_from_pngs("/o/tt.mov", 1, 100, 25) is False


def test_assemble_success(monkeypatch, tmp_path):
    out = str(tmp_path / "tt.mov")
    monkeypatch.setattr(_review_encode.shutil, "which", lambda _x: "/usr/bin/ffmpeg")
    monkeypatch.setattr(_review_encode.os.path, "exists", lambda _p: True)
    captured: dict = {}

    class _Done:
        returncode = 0

    monkeypatch.setattr(_review_encode.subprocess, "run",
                        lambda cmd, **kw: captured.update(cmd=cmd) or _Done())
    assert _review_encode.assemble_mov_from_pngs(out, 1, 48, 25) is True
    assert captured["cmd"][0] == "/usr/bin/ffmpeg"
    assert captured["cmd"][-1] == out


def test_assemble_returns_false_on_ffmpeg_error(monkeypatch, tmp_path):
    out = str(tmp_path / "tt.mov")
    monkeypatch.setattr(_review_encode.shutil, "which", lambda _x: "/usr/bin/ffmpeg")
    monkeypatch.setattr(_review_encode.os.path, "exists", lambda _p: True)

    class _Fail:
        returncode = 1

    monkeypatch.setattr(_review_encode.subprocess, "run", lambda *a, **k: _Fail())
    assert _review_encode.assemble_mov_from_pngs(out, 1, 48, 25) is False
