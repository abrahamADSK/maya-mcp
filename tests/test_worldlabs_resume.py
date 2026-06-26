"""Tests for the World Labs resume sidecar.

Pure ``resume.py`` (file I/O only, no clock) is tested against ``tmp_path``;
the ``generate`` / ``download`` / ``status`` wiring is tested with a mocked
client (no network, no credits), mirroring ``test_worldlabs_tool.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from maya_mcp.worldlabs import resume
from maya_mcp.worldlabs import tool as tool_mod


# ---------------------------------------------------------------------------
# Pure resume.py — sidecar read/write
# ---------------------------------------------------------------------------

def test_write_then_read_sidecar_merges(tmp_path):
    resume.write_sidecar(tmp_path, now_iso="2026-06-26T10:00:00Z",
                         operation_id="op1", status="generating")
    resume.write_sidecar(tmp_path, now_iso="2026-06-26T10:05:00Z",
                         world_id="w1", status="downloaded")
    data = resume.read_sidecar(tmp_path)
    assert data["operation_id"] == "op1"   # preserved across the merge
    assert data["world_id"] == "w1"
    assert data["status"] == "downloaded"
    assert data["created_at"] == "2026-06-26T10:00:00Z"  # stamped once
    assert data["updated_at"] == "2026-06-26T10:05:00Z"


def test_write_sidecar_ignores_none(tmp_path):
    resume.write_sidecar(tmp_path, now_iso="t", operation_id="op1")
    resume.write_sidecar(tmp_path, now_iso="t2", operation_id=None, world_id="w1")
    assert resume.read_sidecar(tmp_path)["operation_id"] == "op1"  # not cleared


def test_read_sidecar_absent_returns_none(tmp_path):
    assert resume.read_sidecar(tmp_path) is None


# ---------------------------------------------------------------------------
# Pure resume.py — scan_state resume rules
# ---------------------------------------------------------------------------

def test_scan_needs_generate_when_empty(tmp_path):
    assert resume.scan_state(tmp_path)["status"] == "needs_generate"


def test_scan_needs_download_with_op_no_files(tmp_path):
    resume.write_sidecar(tmp_path, now_iso="t", operation_id="op9")
    s = resume.scan_state(tmp_path)
    assert s["status"] == "needs_download"
    assert s["operation_id"] == "op9"
    assert "expires" in s["next_step"]  # TTL warning surfaced


def test_scan_needs_convert_when_spz_present(tmp_path):
    (tmp_path / "DJ.v001.spz").write_bytes(b"x")
    s = resume.scan_state(tmp_path)
    assert s["status"] == "needs_convert"
    assert s["exists"]["spz"].endswith("DJ.v001.spz")


def test_scan_ready_to_build_when_ply_present(tmp_path):
    (tmp_path / "DJ.v001.spz").write_bytes(b"x")
    (tmp_path / "DJ.v001.ply").write_bytes(b"y")
    (tmp_path / "DJ.v001.png").write_bytes(b"z")
    s = resume.scan_state(tmp_path)
    assert s["status"] == "ready_to_build"
    assert s["exists"]["ply"].endswith(".ply")
    assert s["exists"]["pano"].endswith(".png")


def test_scan_picks_newest_version(tmp_path):
    (tmp_path / "DJ.v001.ply").write_bytes(b"a")
    (tmp_path / "DJ.v002.ply").write_bytes(b"b")
    assert resume.scan_state(tmp_path)["exists"]["ply"].endswith("v002.ply")


# ---------------------------------------------------------------------------
# Tool wiring — sidecar written by generate / download, read by status
# ---------------------------------------------------------------------------

def _mock_client(**attrs):
    c = MagicMock()
    for k, v in attrs.items():
        setattr(c, k, v)
    return c


def test_generate_writes_sidecar_with_op_id(tmp_path, monkeypatch):
    client = _mock_client()
    client.generate.return_value = "op-abc"
    monkeypatch.setattr(tool_mod, "_client", lambda: client)
    out = json.loads(tool_mod.generate(
        "https://x/i.png", "world", confirm=True, work_dir=str(tmp_path),
    ))
    assert out["status"] == "started"
    sc = resume.read_sidecar(tmp_path)
    assert sc["operation_id"] == "op-abc"
    assert sc["status"] == "generating"
    assert sc["image"] == "https://x/i.png"


def test_generate_without_work_dir_writes_no_sidecar(tmp_path, monkeypatch):
    client = _mock_client()
    client.generate.return_value = "op-abc"
    monkeypatch.setattr(tool_mod, "_client", lambda: client)
    tool_mod.generate("https://x/i.png", "world", confirm=True)  # no work_dir
    assert resume.read_sidecar(tmp_path) is None


def test_download_updates_sidecar_with_world_id(tmp_path, monkeypatch):
    op = MagicMock()
    op.done = True
    op.response.world_id = "world-77"
    client = _mock_client()
    client.poll.return_value = op
    client.download_assets.return_value = {
        "splats_full_res": tmp_path / "DJ.v001.spz",
        "pano": tmp_path / "DJ.v001.png",
    }
    monkeypatch.setattr(tool_mod, "_client", lambda: client)
    out = json.loads(tool_mod.download("op-abc", str(tmp_path)))
    assert out["status"] == "ok"
    sc = resume.read_sidecar(tmp_path)
    assert sc["world_id"] == "world-77"
    assert sc["operation_id"] == "op-abc"
    assert sc["status"] == "downloaded"


def test_status_action_reports_state(tmp_path):
    (tmp_path / "DJ.v001.spz").write_bytes(b"x")
    out = json.loads(tool_mod.status(str(tmp_path)))
    assert out["status"] == "needs_convert"
