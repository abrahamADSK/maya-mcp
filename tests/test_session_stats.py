"""
test_session_stats.py
=====================
Unit tests for the per-session stats reset helpers + F0 telemetry
(`maya_mcp._session_stats`) and their server.py wiring. Ported from flame-mcp
in 3C Wave 2.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path


from maya_mcp._session_stats import (
    DEFAULT_IDLE_RESET_SECONDS,
    TELEMETRY_MAX_BYTES,
    apply_idle_reset,
    make_empty_stats,
    persist_timing,
    persist_turn,
    reset_stats,
    should_auto_reset,
)


def _dt(hour: int = 10, minute: int = 0, second: int = 0) -> datetime.datetime:
    """Tiny helper to build datetimes with fewer kwargs at the call site."""
    return datetime.datetime(2026, 5, 21, hour, minute, second)


# ── make_empty_stats ────────────────────────────────────────────────────────

def test_empty_stats_has_all_canonical_keys() -> None:
    """Zero template must carry exactly the keys the server consumes."""
    stats = make_empty_stats()
    expected = {
        "exec_calls", "tokens_in", "tokens_out", "rag_calls",
        "tokens_saved", "patterns_learned", "patterns_staged",
        "safety_blocks", "cache_hits",
        # F0: p_fallo counters (3C Wave 2).
        "turns_total", "failed_turns",
        "timings",
    }
    assert set(stats.keys()) == expected


def test_empty_stats_counters_are_zero() -> None:
    """Every numeric counter starts at zero and timings is an empty list."""
    stats = make_empty_stats()
    for key, value in stats.items():
        if key == "timings":
            assert value == []
        else:
            assert value == 0, f"counter {key} not zeroed"


def test_empty_stats_includes_p_fallo_counters() -> None:
    """F0 — turns_total and failed_turns are present and zero so p_fallo
    starts as 0/0 (undefined → reported as '—' by the consumer)."""
    stats = make_empty_stats()
    assert stats["turns_total"] == 0
    assert stats["failed_turns"] == 0


def test_empty_stats_matches_server_schema() -> None:
    """The schema invariant in code form: the server's live _stats dict must
    carry exactly the keys make_empty_stats produces (it is initialised from
    it, so this guards against a future divergent manual edit)."""
    from maya_mcp import server
    assert set(server._stats.keys()) == set(make_empty_stats().keys())


# ── persist_timing / persist_turn ──────────────────────────────────────────

def test_persist_timing_writes_one_jsonl_line(tmp_path: Path) -> None:
    """A single call appends exactly one well-formed JSON line."""
    log = tmp_path / "timings.jsonl"
    persist_timing(log, {"op": "exec", "total_ms": 12})

    contents = log.read_text(encoding="utf-8").splitlines()
    assert len(contents) == 1
    assert json.loads(contents[0]) == {"op": "exec", "total_ms": 12}


def test_persist_timing_appends_across_calls(tmp_path: Path) -> None:
    """Successive calls append; existing content is preserved."""
    log = tmp_path / "timings.jsonl"
    persist_timing(log, {"op": "exec", "n": 1})
    persist_timing(log, {"op": "rag", "n": 2})

    lines = log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_persist_timing_creates_parent_directory(tmp_path: Path) -> None:
    """Parent directory is created on demand — caller need not pre-mkdir."""
    log = tmp_path / "nested" / "dir" / "timings.jsonl"
    persist_timing(log, {"op": "exec"})
    assert log.exists()


def test_persist_timing_rotates_when_oversized(tmp_path: Path) -> None:
    """When the log reaches TELEMETRY_MAX_BYTES it is rotated to .1 and the
    new line lands in a fresh file. A previous .1 is overwritten."""
    log = tmp_path / "timings.jsonl"
    rotated = tmp_path / "timings.jsonl.1"
    rotated.write_text("STALE\n", encoding="utf-8")
    log.write_bytes(b"X" * (TELEMETRY_MAX_BYTES + 1))

    persist_timing(log, {"op": "exec", "after": "rotation"})

    assert "STALE" not in rotated.read_text(encoding="utf-8")
    new_lines = log.read_text(encoding="utf-8").splitlines()
    assert len(new_lines) == 1
    assert json.loads(new_lines[0])["after"] == "rotation"


def test_persist_timing_swallows_io_errors(tmp_path: Path) -> None:
    """An unwritable path must NOT raise — telemetry never crashes callers."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    log = blocker / "timings.jsonl"
    persist_timing(log, {"op": "exec"})  # must not raise


def test_persist_timing_handles_non_serialisable_values(tmp_path: Path) -> None:
    """Non-JSON-native values are coerced via str (default=str)."""
    log = tmp_path / "timings.jsonl"
    persist_timing(log, {"op": "exec", "ts": datetime.datetime(2026, 5, 13, 12, 0, 0)})

    parsed = json.loads(log.read_text(encoding="utf-8"))
    assert parsed["op"] == "exec"
    assert "2026-05-13" in parsed["ts"]


def test_persist_turn_delegates_to_persist_timing(tmp_path: Path) -> None:
    """The turn-level helper shares the timing contract."""
    log = tmp_path / "turns.jsonl"
    persist_turn(log, {"model": "claude-opus", "exit_code": 0})

    parsed = json.loads(log.read_text(encoding="utf-8"))
    assert parsed == {"model": "claude-opus", "exit_code": 0}


# ── should_auto_reset ───────────────────────────────────────────────────────

def test_should_auto_reset_false_on_first_call() -> None:
    assert should_auto_reset(_dt(), None) is False


def test_should_auto_reset_false_within_idle_window() -> None:
    assert should_auto_reset(_dt(10, 30, 0), _dt(10, 29, 0)) is False


def test_should_auto_reset_true_at_exact_threshold() -> None:
    now = _dt(10, 30, 0)
    last = now - datetime.timedelta(seconds=DEFAULT_IDLE_RESET_SECONDS)
    assert should_auto_reset(now, last) is True


def test_should_auto_reset_true_past_threshold() -> None:
    assert should_auto_reset(_dt(12, 0, 0), _dt(10, 0, 0)) is True


def test_should_auto_reset_custom_threshold() -> None:
    now = _dt(10, 0, 10)
    last = _dt(10, 0, 0)
    assert should_auto_reset(now, last, idle_reset_seconds=5) is True
    assert should_auto_reset(now, last, idle_reset_seconds=15) is False


# ── apply_idle_reset ────────────────────────────────────────────────────────

def test_apply_idle_reset_does_nothing_within_window() -> None:
    stats = make_empty_stats()
    stats["exec_calls"] = 7
    stats["tokens_in"] = 1234

    did, _ = apply_idle_reset(stats, _dt(10, 5, 0), _dt(10, 0, 0))

    assert did is False
    assert stats["exec_calls"] == 7
    assert stats["tokens_in"] == 1234


def test_apply_idle_reset_zeros_counters_past_window() -> None:
    stats = make_empty_stats()
    original_id = id(stats)
    stats["exec_calls"] = 42
    stats["turns_total"] = 10
    stats["failed_turns"] = 3
    stats["timings"].append({"op": "exec", "total_ms": 12})

    did, reset_at = apply_idle_reset(stats, _dt(12, 0, 0), _dt(10, 0, 0))

    assert did is True
    assert reset_at == _dt(12, 0, 0)
    assert id(stats) == original_id, "dict identity must be preserved"
    assert stats["exec_calls"] == 0
    assert stats["turns_total"] == 0
    assert stats["failed_turns"] == 0
    assert stats["timings"] == []


def test_apply_idle_reset_preserves_identity_for_module_refs() -> None:
    """server.py takes a module-level reference to `_stats`; the helper must
    mutate in place so that reference does not go stale."""
    stats = make_empty_stats()
    external_ref = stats
    stats["exec_calls"] = 5

    apply_idle_reset(stats, _dt(12, 0, 0), _dt(10, 0, 0))

    assert external_ref is stats
    assert external_ref["exec_calls"] == 0


def test_apply_idle_reset_ignores_first_call() -> None:
    stats = make_empty_stats()
    stats["exec_calls"] = 3

    did, _ = apply_idle_reset(stats, _dt(23, 59, 0), None)

    assert did is False
    assert stats["exec_calls"] == 3


# ── reset_stats (explicit) ──────────────────────────────────────────────────

def test_reset_stats_clears_unconditionally() -> None:
    stats = make_empty_stats()
    stats["exec_calls"] = 10
    stats["timings"].append({"op": "exec"})

    reset_at = reset_stats(stats, _dt(10, 0, 1))

    assert reset_at == _dt(10, 0, 1)
    assert stats["exec_calls"] == 0
    assert stats["timings"] == []


def test_reset_stats_preserves_identity() -> None:
    stats = make_empty_stats()
    external_ref = stats
    stats["exec_calls"] = 99

    reset_stats(stats, _dt())

    assert external_ref is stats
    assert external_ref["exec_calls"] == 0


# ── server.py wiring (F0 turns_total / failed_turns) ─────────────────────────

def test_execute_python_increments_turns_total_on_success(monkeypatch) -> None:
    """A successful execute_python past the safety gate counts one turn and
    zero failures, and persists a timing entry to the ring buffer."""
    from maya_mcp import server

    monkeypatch.setattr(server.bridge, "execute", lambda code, as_json=False: "OK")
    server._stats.update(make_empty_stats())

    asyncio.run(server._do_execute_python({"code": "result = 1 + 1"}))

    assert server._stats["turns_total"] == 1
    assert server._stats["failed_turns"] == 0
    assert len(server._stats["timings"]) == 1
    assert server._stats["timings"][-1]["error"] is False


def test_execute_python_increments_failed_turns_on_error(monkeypatch) -> None:
    """A bridge error counts one turn AND one failed turn → p_fallo = 1."""
    from maya_mcp import server

    def boom(code, as_json=False):
        raise server.MayaBridgeError("boom")

    monkeypatch.setattr(server.bridge, "execute", boom)
    server._stats.update(make_empty_stats())

    out = asyncio.run(server._do_execute_python({"code": "result = bad()"}))

    assert "Maya error" in out
    assert server._stats["turns_total"] == 1
    assert server._stats["failed_turns"] == 1
    assert server._stats["timings"][-1]["error"] is True


def test_safety_block_does_not_count_as_turn(monkeypatch) -> None:
    """A blocked-by-safety call never reaches the bridge, so it is NOT a turn
    (turns_total counts only code that ran past every gate)."""
    from maya_mcp import server

    monkeypatch.setattr(server.bridge, "execute", lambda code, as_json=False: "OK")
    server._stats.update(make_empty_stats())

    asyncio.run(server._do_execute_python({"code": "cmds.delete('*')"}))

    assert server._stats["turns_total"] == 0
    assert server._stats["safety_blocks"] == 1


def test_reset_session_stats_tool_zeroes_counters(monkeypatch) -> None:
    """The reset_session_stats tool clears the live counters in place."""
    from maya_mcp import server

    server._stats.update(make_empty_stats())
    server._stats["exec_calls"] = 12
    server._stats["turns_total"] = 5

    out = asyncio.run(server.reset_session_stats_tool())

    assert json.loads(out)["status"] == "reset"
    assert server._stats["exec_calls"] == 0
    assert server._stats["turns_total"] == 0


def test_session_stats_reports_p_fallo(monkeypatch) -> None:
    """session_stats surfaces p_fallo derived from the turn counters."""
    from maya_mcp import server

    server._stats.update(make_empty_stats())
    server._stats["turns_total"] = 4
    server._stats["failed_turns"] = 1

    out = asyncio.run(server.session_stats_tool())
    parsed = json.loads(out)

    assert parsed["execute_python_turns"] == 4
    assert parsed["failed_turns"] == 1
    assert parsed["p_fallo"] == "25%"
