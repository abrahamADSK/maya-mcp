"""
test_worldlabs_convert.py
=========================
Tests for the SPZ -> PLY conversion seam in
``src/maya_mcp/worldlabs/convert.py``.

The conversion is delegated to an external converter binary (subprocess seam),
so these tests fake ``subprocess.run`` to exercise the command-building and
error paths WITHOUT a real converter installed. A real end-to-end conversion
test is provided but ``skipif``-gated on a converter binary + a sample SPZ file
being present (so it is skipped on machines that have neither).
"""

from __future__ import annotations

import subprocess
import types

import pytest

from maya_mcp.worldlabs import convert as convert_mod
from maya_mcp.worldlabs import (
    SpzConversionError,
    SpzConversionUnavailable,
    convert_spz_to_ply,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


def _fake_run_factory(write_output: bool = True, returncode: int = 0):
    """Build a fake ``subprocess.run`` that records the cmd and optionally writes
    the declared output file."""
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        if write_output:
            # Output path is either the ``-o`` arg (3dgsconverter) or the last arg.
            if "-o" in cmd:
                out = cmd[cmd.index("-o") + 1]
            else:
                out = cmd[-1]
            with open(out, "wb") as fh:
                fh.write(b"PLY\nfake")
        return types.SimpleNamespace(returncode=returncode, stdout="ok", stderr="")

    return _fake_run, captured


# ── Tests ──────────────────────────────────────────────────────────────────


def test_missing_input_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        convert_spz_to_ply(tmp_path / "nope.spz")


def test_no_converter_available_raises(tmp_path, monkeypatch):
    spz = tmp_path / "world.spz"
    spz.write_bytes(b"SPZ-bytes")
    monkeypatch.setattr(convert_mod, "_resolve_converter_bin", lambda explicit=None: None)

    with pytest.raises(SpzConversionUnavailable):
        convert_spz_to_ply(spz)


def test_builds_3dgsconverter_command(tmp_path, monkeypatch):
    spz = tmp_path / "world.spz"
    spz.write_bytes(b"SPZ-bytes")
    monkeypatch.setattr(
        convert_mod, "_resolve_converter_bin", lambda explicit=None: "/usr/bin/3dgsconverter"
    )
    fake_run, captured = _fake_run_factory()
    monkeypatch.setattr(convert_mod.subprocess, "run", fake_run)

    out = convert_spz_to_ply(spz)

    assert out == spz.with_suffix(".ply")
    assert out.is_file()
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/3dgsconverter"
    assert "-i" in cmd and "-o" in cmd and "-f" in cmd
    assert cmd[cmd.index("-i") + 1] == str(spz)


def test_builds_generic_spz_command(tmp_path, monkeypatch):
    spz = tmp_path / "world.spz"
    spz.write_bytes(b"SPZ-bytes")
    ply = tmp_path / "out.ply"
    monkeypatch.setattr(
        convert_mod, "_resolve_converter_bin", lambda explicit=None: "/usr/local/bin/spz"
    )
    fake_run, captured = _fake_run_factory()
    monkeypatch.setattr(convert_mod.subprocess, "run", fake_run)

    out = convert_spz_to_ply(spz, ply)

    assert out == ply
    assert out.is_file()
    assert captured["cmd"] == ["/usr/local/bin/spz", "convert", str(spz), str(ply)]


def test_converter_failure_raises(tmp_path, monkeypatch):
    spz = tmp_path / "world.spz"
    spz.write_bytes(b"SPZ-bytes")
    monkeypatch.setattr(
        convert_mod, "_resolve_converter_bin", lambda explicit=None: "/usr/bin/3dgsconverter"
    )

    def _boom(cmd, **kw):
        raise subprocess.CalledProcessError(returncode=2, cmd=cmd, stderr="bad spz")

    monkeypatch.setattr(convert_mod.subprocess, "run", _boom)

    with pytest.raises(SpzConversionError) as exc:
        convert_spz_to_ply(spz)
    assert "bad spz" in str(exc.value)


def test_converter_success_but_no_output_raises(tmp_path, monkeypatch):
    spz = tmp_path / "world.spz"
    spz.write_bytes(b"SPZ-bytes")
    monkeypatch.setattr(
        convert_mod, "_resolve_converter_bin", lambda explicit=None: "/usr/bin/3dgsconverter"
    )
    fake_run, _ = _fake_run_factory(write_output=False)
    monkeypatch.setattr(convert_mod.subprocess, "run", fake_run)

    with pytest.raises(SpzConversionError):
        convert_spz_to_ply(spz)


def test_converter_timeout_raises(tmp_path, monkeypatch):
    spz = tmp_path / "world.spz"
    spz.write_bytes(b"SPZ-bytes")
    monkeypatch.setattr(
        convert_mod, "_resolve_converter_bin", lambda explicit=None: "/usr/bin/spz"
    )

    def _slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 0))

    monkeypatch.setattr(convert_mod.subprocess, "run", _slow)

    with pytest.raises(SpzConversionError):
        convert_spz_to_ply(spz, timeout=1.0)


def test_opencv_to_dcc_scale_constant():
    """The documented coordinate-flip seam is exposed for the Phase B importer."""
    assert convert_mod.OPENCV_TO_DCC_SCALE == (1.0, -1.0, -1.0)


# ── Real conversion (skipped unless a converter + sample SPZ are present) ───

import os  # noqa: E402 — used only by the skipif gate below

_REAL_BIN = convert_mod._resolve_converter_bin()
_REAL_SAMPLE = os.environ.get("WORLDLABS_SPZ_SAMPLE", "")


@pytest.mark.skipif(
    not _REAL_BIN or not _REAL_SAMPLE or not os.path.isfile(_REAL_SAMPLE),
    reason=(
        "real SPZ->PLY conversion needs a converter on PATH (or "
        "WORLDLABS_SPZ_CONVERTER) AND a sample file in WORLDLABS_SPZ_SAMPLE"
    ),
)
def test_real_spz_to_ply_conversion(tmp_path):
    out = convert_spz_to_ply(_REAL_SAMPLE, tmp_path / "real.ply")
    assert out.is_file()
    assert out.stat().st_size > 0
