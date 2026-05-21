"""
_session_stats.py
=================
Per-session reset machinery + F0 telemetry for the MCP server's `_stats` dict.

Ported from flame-mcp (3C Wave 2). The generic machinery — `persist_timing`,
`persist_turn`, `should_auto_reset`, `apply_idle_reset`, `reset_stats` and the
two tuning constants — is byte-identical across the ecosystem; only
`make_empty_stats()` carries this repo's counter schema.

Problem
-------
`server.py::_stats` accumulates metrics (tool calls, tokens in/out, patterns
learned, …) for the lifetime of the MCP server process. Long-running processes
(an editor keeping the server spawned for days) produce `session_stats()`
outputs that span multiple Claude sessions, making the token-efficiency rating
meaningless.

No MCP-level session signal
---------------------------
MCP over stdio does NOT expose a "Claude session boundary" to the server, so
there is no reliable notification the server can subscribe to that says "a new
Claude session started".

Chosen approach
---------------
Two reset triggers combine to keep the stats honest without touching the MCP
protocol:

1.  **Idle-based auto-reset.** If the gap between the previous call and the new
    one exceeds `idle_reset_seconds` (default 1800 s = 30 min), the counters are
    reset automatically on the new call.
2.  **Explicit reset tool.** A `reset_session_stats()` MCP tool lets the model
    or operator zero the counters deliberately at the start of a new task.

Both triggers update `_stats_reset_at`, surfaced by `session_stats()`.

F0 telemetry
------------
`make_empty_stats()` adds the `turns_total` / `failed_turns` counters that drive
`p_fallo = failed_turns / turns_total` (the per-session failure probability), and
`persist_timing()` streams an enriched copy of each call's timing to a JSONL log
so cross-session baselines survive server restarts.

Public API
----------
make_empty_stats() -> dict
    Canonical zero-value template. Shared by the initializer and the reset paths
    so they cannot drift.
persist_timing(log_path, entry) / persist_turn(log_path, entry)
    Best-effort JSONL append with size-based rotation. Never raises.
should_auto_reset(now, last_call_at, *, idle_reset_seconds) -> bool
    Pure predicate: True when the idle gap exceeds the window.
apply_idle_reset(stats, now, last_call_at, *, idle_reset_seconds)
    Mutates `stats` in place (preserves identity) when an idle reset is due.
reset_stats(stats, now) -> datetime
    Unconditional in-place reset (wired to `reset_session_stats`).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional, Tuple


# Canonical idle threshold. Long enough that a brief step-away does not surprise
# the user with a reset, short enough that a "next morning" session starts clean.
DEFAULT_IDLE_RESET_SECONDS = 30 * 60  # 30 minutes

# Size cap for JSONL telemetry files written by persist_timing / persist_turn.
# ~5 MB is approximately 10 k lines for typical timing entries. When the file
# reaches the cap it is rotated to `<path>.1` (overwriting any previous .1),
# giving one rollover of history without unbounded growth.
TELEMETRY_MAX_BYTES = 5 * 1024 * 1024


def make_empty_stats() -> dict:
    """
    Return a freshly initialised `_stats` dict.

    Kept in sync with the module-level `_stats` reference in `server.py` — any
    new counter added there must be added here too (and vice-versa). The
    `stats_keys_schema_shared` concept invariant locks the two together.

    Returns
    -------
    dict
        Dictionary with every counter zeroed and the timings buffer set to an
        empty list.
    """
    return {
        "exec_calls":       0,   # total tool calls
        "tokens_in":        0,   # tokens in parameters
        "tokens_out":       0,   # tokens in responses
        "rag_calls":        0,   # search_maya_docs calls
        "tokens_saved":     0,   # tokens saved by RAG vs loading full doc
        "patterns_learned": 0,   # patterns added to docs
        "patterns_staged":  0,   # candidates staged by non-trusted models
        "safety_blocks":    0,   # dangerous pattern detections
        "cache_hits":       0,   # RAG cache hits
        # F0 baseline counters: drive p_fallo = failed_turns / turns_total.
        # Incremented inside execute_python only (the error-prone free-form path);
        # dedicated tools have their own exec_calls / safety_blocks counters.
        "turns_total":      0,   # execute_python calls that ran past all gates
        "failed_turns":     0,   # subset of turns_total that returned an error
        "timings":          [],  # ring buffer of recent call timings (max 20)
    }


def persist_timing(log_path: Path, entry: dict) -> None:
    """
    Append `entry` as a single JSON line to `log_path`.

    Best-effort: any OSError, serialization error, or permission issue is
    swallowed silently — the MCP server must not crash on telemetry I/O.
    Creates the parent directory on demand.

    Rotation: when the file reaches `TELEMETRY_MAX_BYTES`, it is renamed to
    `<path>.1` (overwriting any previous `.1`) before the new line is written.
    This keeps one rollover of history without unbounded growth.

    Parameters
    ----------
    log_path : Path
        Destination JSONL file. Parent directory created if missing.
    entry : dict
        Anything JSON-serialisable. Non-serialisable values are coerced via
        ``str`` by ``json.dumps(default=str)`` so the call still succeeds on
        edge cases (e.g. ``datetime``).
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log_path.stat().st_size >= TELEMETRY_MAX_BYTES:
                rotated = log_path.with_suffix(log_path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                log_path.rename(rotated)
        except FileNotFoundError:
            pass
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def persist_turn(log_path: Path, entry: dict) -> None:
    """
    Append `entry` to a turn-level JSONL file. Same contract and rotation
    behaviour as ``persist_timing``; the function is split for clarity at the
    call sites and because a future refactor may want to specialise the two
    streams.
    """
    persist_timing(log_path, entry)


def should_auto_reset(
    now: datetime.datetime,
    last_call_at: Optional[datetime.datetime],
    *,
    idle_reset_seconds: int = DEFAULT_IDLE_RESET_SECONDS,
) -> bool:
    """
    Decide whether an idle-gap reset is due.

    Parameters
    ----------
    now : datetime.datetime
        Timestamp of the new call. Tests inject this explicitly; production
        code passes `datetime.datetime.now()`.
    last_call_at : datetime.datetime | None
        Timestamp of the previous call. `None` on the first call ever — treated
        as "do not reset" because the counters are already fresh.
    idle_reset_seconds : int, keyword-only, default 1800
        Idle window, in seconds. Defaults to 30 minutes.

    Returns
    -------
    bool
        True iff `(now - last_call_at).total_seconds() >= idle_reset_seconds`.
    """
    if last_call_at is None:
        return False
    gap = (now - last_call_at).total_seconds()
    return gap >= idle_reset_seconds


def apply_idle_reset(
    stats: dict,
    now: datetime.datetime,
    last_call_at: Optional[datetime.datetime],
    *,
    idle_reset_seconds: int = DEFAULT_IDLE_RESET_SECONDS,
) -> Tuple[bool, datetime.datetime]:
    """
    Mutate `stats` in place if an idle reset is due.

    Parameters
    ----------
    stats : dict
        The live `_stats` dict. IDENTITY IS PRESERVED: the function calls
        `.clear()` and `.update(make_empty_stats())` so every module that holds
        a reference to the same dict sees the reset without re-binding.
    now, last_call_at, idle_reset_seconds :
        See `should_auto_reset`.

    Returns
    -------
    (did_reset, reset_at) : tuple[bool, datetime.datetime]
        - `did_reset`  — True iff the dict was cleared.
        - `reset_at`   — `now` either way; the caller ignores it unless did_reset.
    """
    if not should_auto_reset(now, last_call_at, idle_reset_seconds=idle_reset_seconds):
        return False, now
    stats.clear()
    stats.update(make_empty_stats())
    return True, now


def reset_stats(stats: dict, now: datetime.datetime) -> datetime.datetime:
    """
    Unconditional reset (wired to the `reset_session_stats` MCP tool).

    Parameters
    ----------
    stats : dict
        The live `_stats` dict. Cleared in place (identity preserved).
    now : datetime.datetime
        Timestamp stamped as the new `_stats_reset_at`.

    Returns
    -------
    datetime.datetime
        The same `now` value, returned for caller convenience.
    """
    stats.clear()
    stats.update(make_empty_stats())
    return now
