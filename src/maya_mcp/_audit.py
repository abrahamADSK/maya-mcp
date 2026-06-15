"""
_audit.py
=========
Durable, append-only audit log of tool executions for the maya-mcp server.

Purpose
-------
``execute_python`` runs arbitrary Python inside Maya and the dedicated mutation
tools (``maya_create_primitive``, ``maya_transform``, …) mutate production
scenes. Once a call returns there is no durable record on disk of *what the
model executed and what happened*. This module produces that record: a
forensic, accountability-oriented stream — distinct from the F0 efficiency
telemetry in ``logs/timings.jsonl`` (which records only execute_python turns and
deliberately omits the code/params).

Design (proposals/maya-durable-audit-log.md, Option B)
------------------------------------------------------
- **Opt-in, OFF by default.** The whole path is a no-op unless the environment
  variable ``MAYA_AUDIT_LOG`` is truthy (``1`` / ``true`` / ``yes`` / ``on``).
  When disabled there is zero perf, disk, or privacy impact and no behaviour
  change.
- **Reuses the tested persistence substrate.** Records are appended via
  ``maya_mcp._session_stats.persist_timing`` — the ecosystem-standard
  best-effort JSONL append with 5 MB + ``.1`` rotation. The rotation constant
  and the swallow-all-I/O-errors contract stay single-sourced there; this
  module never re-implements them.
- **Size/PII discipline.** For ``execute_python`` the model-supplied ``code`` is
  stored TRUNCATED to ``DEFAULT_MAX_CODE_CHARS`` characters plus a SHA-256 of
  the *full* code (so identical scripts correlate without storing megabytes) and
  the full length. Maya result payloads are NEVER stored.

Record shape
------------
Each entry is one JSON line::

    {
      "ts":      "<ISO-8601 local time, seconds>",
      "tool":    "maya_session" | "maya_transform" | ...,
      "action":  "execute_python" | "delete" | "launch" | "-",
      "status":  "ok" | "error" | "safety_blocked" | "ast_rejected",
      "model":   "<configured model>",
      "backend": "<configured backend>",
      "params":  { ...sanitised params... }
    }

Public API
----------
audit_enabled() -> bool
    True iff the ``MAYA_AUDIT_LOG`` toggle is set. Read live so the toggle can
    be flipped per process launch and exercised by tests.
sanitize_params(params, *, max_code_chars) -> dict
    Coerce params to a plain JSON-friendly dict; truncate + hash ``code``.
build_record(tool, action, params, status, *, model, backend, ts, max_code_chars) -> dict
    Assemble a full record (sanitises params, stamps the timestamp).
status_from_output(output) -> str
    Derive an audit status from a handler's returned string/list payload.
write_record(log_path, record) -> None
    Append the record via the shared rotating persistence helper.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from maya_mcp._session_stats import persist_timing

# ── Status vocabulary (the closed set the brief mandates) ────────────────────
AUDIT_OK = "ok"
AUDIT_ERROR = "error"
AUDIT_SAFETY_BLOCKED = "safety_blocked"
AUDIT_AST_REJECTED = "ast_rejected"

VALID_STATUSES = frozenset(
    {AUDIT_OK, AUDIT_ERROR, AUDIT_SAFETY_BLOCKED, AUDIT_AST_REJECTED}
)

# Truncation budget for free-form ``execute_python`` code. The full code is
# captured only as a SHA-256 digest + length alongside the truncated head.
DEFAULT_MAX_CODE_CHARS = 2000

# Accepted truthy spellings for the MAYA_AUDIT_LOG toggle (mirrors the
# GPU_VERIFY_TLS / suggestions kill-switch idiom already used in the repo).
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def audit_enabled() -> bool:
    """Return True iff durable audit logging is switched on.

    Controlled by the ``MAYA_AUDIT_LOG`` environment variable. Default OFF:
    any value outside :data:`_TRUTHY` (including unset/empty) disables the log.
    Read live (not cached) so a process launched with the toggle picks it up
    and tests can flip it with ``monkeypatch.setenv``.
    """
    return os.environ.get("MAYA_AUDIT_LOG", "").strip().lower() in _TRUTHY


def _to_plain_params(params: Any) -> dict:
    """Coerce ``params`` into a plain dict suitable for JSON serialisation.

    Handles the three shapes the server hands us: ``None`` (no params), a
    Pydantic model (the standalone @mcp.tool inputs — converted via
    ``model_dump``), and a plain dict (the dispatcher actions). Any other type
    is wrapped under a ``value`` key rather than dropped, so the audit never
    silently loses information.
    """
    if params is None:
        return {}
    if hasattr(params, "model_dump"):
        try:
            return dict(params.model_dump())
        except Exception:
            return {"repr": str(params)}
    if isinstance(params, dict):
        return dict(params)
    return {"value": str(params)}


def sanitize_params(
    params: Any, *, max_code_chars: int = DEFAULT_MAX_CODE_CHARS
) -> dict:
    """Return a JSON-friendly copy of ``params`` with ``code`` summarised.

    For the free-form ``execute_python`` path the ``code`` field is replaced by
    its first ``max_code_chars`` characters and enriched with:

    - ``code_sha256`` — SHA-256 of the *full* code (stable correlation key);
    - ``code_len`` — length of the *full* code;
    - ``code_truncated`` — present and True only when truncation actually
      occurred.

    All other params (small, already-validated values) pass through unchanged.
    """
    plain = _to_plain_params(params)
    code = plain.get("code")
    if isinstance(code, str):
        plain["code_sha256"] = hashlib.sha256(code.encode("utf-8")).hexdigest()
        plain["code_len"] = len(code)
        if len(code) > max_code_chars:
            plain["code"] = code[:max_code_chars]
            plain["code_truncated"] = True
    return plain


def build_record(
    tool: str,
    action: str,
    params: Any,
    status: str,
    *,
    model: str = "unknown",
    backend: str = "anthropic",
    ts: Optional[str] = None,
    max_code_chars: int = DEFAULT_MAX_CODE_CHARS,
) -> dict:
    """Assemble a single audit record.

    Parameters
    ----------
    tool : str
        The MCP tool name (e.g. ``maya_session``, ``maya_transform``).
    action : str
        The sub-action for dispatcher tools (e.g. ``execute_python``,
        ``delete``, ``launch``) or ``"-"`` for direct standalone tools.
    params : Any
        Raw params (dict, Pydantic model, or None) — sanitised before storage.
    status : str
        One of :data:`VALID_STATUSES`. An out-of-set value is defensively
        coerced to ``error`` rather than allowed to corrupt the record.
    model, backend : str
        Enrichment mirroring ``_track_timing`` so the audit and timing streams
        share a vocabulary.
    ts : str, optional
        ISO-8601 timestamp; defaults to local now() at second resolution.
    max_code_chars : int
        Truncation budget forwarded to :func:`sanitize_params`.
    """
    if status not in VALID_STATUSES:
        status = AUDIT_ERROR
    return {
        "ts": ts or datetime.datetime.now().isoformat(timespec="seconds"),
        "tool": tool,
        "action": action,
        "status": status,
        "model": model,
        "backend": backend,
        "params": sanitize_params(params, max_code_chars=max_code_chars),
    }


def status_from_output(output: Any) -> str:
    """Best-effort derivation of an audit status from a handler's return value.

    The maya-mcp handlers serialise their result to a JSON string (the bridge
    json.dumps-es the ``result`` dict) and format failures as
    ``"Maya error: …"`` / ``"Unexpected error: …"`` strings. This inspects that
    payload:

    - a JSON object carrying ``safety_warning`` → ``safety_blocked``;
    - ``ast_warning`` → ``ast_rejected``;
    - ``error`` → ``error``;
    - an error-prefixed string (``Maya error``/``Unexpected error``/``ERROR:``)
      → ``error``;
    - anything else → ``ok``.

    ``viewport_capture`` returns a list; it is handled so the helper is robust,
    although the dispatcher/decorator wiring only feeds it strings.
    """
    if isinstance(output, list):
        for item in output:
            if isinstance(item, str) and _is_error_string(item):
                return AUDIT_ERROR
        return AUDIT_OK
    if not isinstance(output, str):
        return AUDIT_OK
    stripped = output.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(output)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            if "safety_warning" in data:
                return AUDIT_SAFETY_BLOCKED
            if "ast_warning" in data:
                return AUDIT_AST_REJECTED
            if "error" in data:
                return AUDIT_ERROR
            return AUDIT_OK
    if _is_error_string(output):
        return AUDIT_ERROR
    return AUDIT_OK


def _is_error_string(text: str) -> bool:
    """True when ``text`` is one of the handler/bridge error-string shapes."""
    return text.startswith(("Maya error", "Unexpected error", "ERROR:"))


def write_record(log_path: Path, record: dict) -> None:
    """Append ``record`` to ``log_path`` via the shared rotating persister.

    Delegates to ``_session_stats.persist_timing`` so the 5 MB + ``.1``
    rotation, parent-dir creation, serialise-safe ``default=str`` dump, and
    best-effort (never-raises) contract are reused verbatim — no duplication.
    """
    persist_timing(log_path, record)


def read_records(
    log_path: Path,
    *,
    limit: int = 50,
    tool: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """Return recent audit records, **newest first**, with optional filters.

    Read companion to :func:`write_record` powering the ``operation_history``
    read action. Reads the rotated ``.1`` sibling (older) then the active log
    (newer), so a rotation boundary never hides recent history. The substrate's
    best-effort contract is mirrored on the read side: a missing/unreadable file
    yields ``[]`` and a malformed line is skipped — this never raises into a tool
    call.

    Parameters
    ----------
    log_path : Path
        The active audit log (``logs/audit.jsonl``); its ``.1`` sibling is read
        automatically when present.
    limit : int
        Cap on the number of (filtered) records returned, counted from the
        newest. ``limit <= 0`` means no cap.
    tool, action, status : str, optional
        Exact-match filters on the corresponding record fields; ``None`` (the
        default) does not filter that field.
    """
    # Oldest-on-disk first so the concatenated stream is chronological, then
    # reverse for newest-first output.
    files = [log_path.with_name(log_path.name + ".1"), log_path]
    records: list[dict] = []
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue  # skip a torn/partial line, never raise
                    if not isinstance(rec, dict):
                        continue
                    if tool is not None and rec.get("tool") != tool:
                        continue
                    if action is not None and rec.get("action") != action:
                        continue
                    if status is not None and rec.get("status") != status:
                        continue
                    records.append(rec)
        except OSError:
            continue  # missing/unreadable file → contribute nothing
    records.reverse()
    if limit and limit > 0:
        records = records[:limit]
    return records
