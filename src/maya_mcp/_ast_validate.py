"""
_ast_validate.py
================
F4b (3C Wave 4) — AST dry-run walker for ``maya_session(execute_python)``.

When the LLM emits Python through ``execute_python``, this module statically
walks the AST, finds every ``cmds.<command>`` / ``maya.cmds.<command>`` call,
and validates the command name against ``rag/api_graph.json`` (produced by
``scripts/introspect_maya_api.py`` from a live ``maya.cmds``). If a command
does not exist, the call is rejected with an actionable, suggestion-bearing
error BEFORE the Command Port round-trip to Maya — turning a runtime
``RuntimeError: cmds.polyCubez is not callable`` into a single-digit-millisecond
static catch.

Mirrors flame-mcp's F4b in spirit: it validates command **existence**, the
false-positive-free, high-value check. It deliberately does NOT validate
*usage* (flag names, argument arity, return-value handling) — that is the job
of the RAG layer and the anti-pattern corpus. Erring toward "valid" keeps the
false-positive rate at zero for real commands.

Scope
-----
CAN catch:
- ``cmds.polyCubez(...)`` (typo / hallucinated command).
- ``maya.cmds.fooBarBaz(...)`` (invented command).

CANNOT (and is not meant to) catch:
- ``cmds.polyCube(width=5)`` — ``polyCube`` exists; the trap is the long flag
  name (``width=`` vs ``w=``). That is a usage error → anti-pattern corpus.
- ``mc.polyCube(...)`` where the module was aliased to something other than
  ``cmds`` — out of scope, treated as "not a maya command reference" so we
  never false-positive on an unrelated local named ``mc``.
- Anything on the RESULT of a command (``cmds.ls()[0].strip()``) — the chain
  past the command is on a plain string/list, not the cmds module.

Graceful degradation
--------------------
When ``rag/api_graph.json`` is missing or empty (fresh clone, CI, or the
operator hasn't run the introspector under mayapy yet), ``validate_python``
returns an empty issue list with ``graph_loaded=False`` — the walker becomes a
no-op and ``execute_python`` lets the call through. F4b is opt-in extra
protection, never a hard prerequisite.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Graph path & cache
# ---------------------------------------------------------------------------

# _ast_validate.py lives in src/maya_mcp/; the graph sits in src/maya_mcp/rag/.
_API_GRAPH_PATH = Path(__file__).resolve().parent / "rag" / "api_graph.json"

_GRAPH_CACHE: Optional[dict] = None
_GRAPH_CACHE_PATH: Optional[Path] = None


def _load_graph(path: Path = _API_GRAPH_PATH) -> dict:
    """Load and cache ``rag/api_graph.json``.

    Returns an empty dict when the file is missing or unparseable — the walker
    then degrades to a no-op.
    """
    global _GRAPH_CACHE, _GRAPH_CACHE_PATH
    if _GRAPH_CACHE is not None and _GRAPH_CACHE_PATH == path:
        return _GRAPH_CACHE
    _GRAPH_CACHE_PATH = path
    if not path.exists():
        _GRAPH_CACHE = {}
        return _GRAPH_CACHE
    try:
        _GRAPH_CACHE = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _GRAPH_CACHE = {}
    return _GRAPH_CACHE


def _reset_graph_cache() -> None:
    """Test-only hook: force the next ``_load_graph`` call to re-read."""
    global _GRAPH_CACHE, _GRAPH_CACHE_PATH
    _GRAPH_CACHE = None
    _GRAPH_CACHE_PATH = None


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnknownCommand:
    """One unresolved ``cmds.<command>`` reference in the parsed source."""

    command: str          # e.g. "polyCubez"
    line: int             # 1-based
    col: int              # 0-based
    suggestion: Optional[str] = None  # nearest valid command or None


@dataclass
class AstValidation:
    """Result of ``validate_python`` — list of issues + summary fields."""

    issues: List[UnknownCommand] = field(default_factory=list)
    graph_loaded: bool = True  # False when the graph was missing/empty

    @property
    def ok(self) -> bool:
        """True when the source is safe to send to Maya as far as F4b can tell."""
        return not self.issues


# ---------------------------------------------------------------------------
# AST walker
# ---------------------------------------------------------------------------


def _command_of(node: ast.Attribute) -> Optional[str]:
    """Return the maya command name if ``node`` is ``cmds.<X>`` or
    ``maya.cmds.<X>``, else None.

    Only the attribute *directly* on the ``cmds`` module counts as a command —
    ``cmds.ls().strip`` has ``ls`` as the command and ``strip`` (on the result)
    is correctly ignored because its ``.value`` is a Call, not the cmds module.
    """
    value = node.value
    # cmds.<X>
    if isinstance(value, ast.Name) and value.id == "cmds":
        return node.attr
    # maya.cmds.<X>
    if (
        isinstance(value, ast.Attribute)
        and value.attr == "cmds"
        and isinstance(value.value, ast.Name)
        and value.value.id == "maya"
    ):
        return node.attr
    return None


class _CmdsCallCollector(ast.NodeVisitor):
    """Collect every ``cmds.<command>`` / ``maya.cmds.<command>`` reference."""

    def __init__(self) -> None:
        self.references: List[Tuple[str, int, int]] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        command = _command_of(node)
        if command is not None:
            self.references.append((command, node.lineno, node.col_offset))
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _suggest(command: str, valid: Set[str]) -> Optional[str]:
    """Return the closest valid command name, or None when nothing is similar."""
    matches = get_close_matches(command, valid, n=1, cutoff=0.6)
    return matches[0] if matches else None


def validate_python(
    source: str,
    graph: Optional[dict] = None,
    *,
    graph_path: Path = _API_GRAPH_PATH,
) -> AstValidation:
    """Walk ``source`` and report unknown ``cmds.<command>`` references.

    Args:
        source: Python the LLM intends to run via ``execute_python``.
        graph: Optional pre-loaded graph dict (mainly for tests). If None,
            loads from ``graph_path`` with caching.
        graph_path: Override the default graph location.

    Returns:
        An :class:`AstValidation`. Empty issues means "safe as far as F4b can
        tell". ``graph_loaded=False`` means validation was unavailable (missing
        graph) and the caller must NOT read the empty list as a green light.
    """
    if graph is None:
        graph = _load_graph(graph_path)
    valid = set((graph or {}).get("commands", {}))
    if not valid:
        return AstValidation(issues=[], graph_loaded=False)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Let the Command Port surface the SyntaxError; F4b stays out of the way.
        return AstValidation(issues=[], graph_loaded=True)

    collector = _CmdsCallCollector()
    collector.visit(tree)

    issues: List[UnknownCommand] = []
    seen: Set[Tuple[str, int, int]] = set()
    for command, line, col in collector.references:
        if (command, line, col) in seen:
            continue
        seen.add((command, line, col))
        if command in valid:
            continue
        issues.append(
            UnknownCommand(
                command=command, line=line, col=col,
                suggestion=_suggest(command, valid),
            )
        )

    return AstValidation(issues=issues, graph_loaded=True)


def format_issues(validation: AstValidation) -> str:
    """Return a human-readable, multi-line message describing each issue."""
    if not validation.issues:
        return ""
    lines = [
        "❌ AST dry-run rejected the snippet — unknown maya.cmds command(s):",
        "",
    ]
    for issue in validation.issues:
        suggestion = (
            f" → did you mean `cmds.{issue.suggestion}`?"
            if issue.suggestion
            else ""
        )
        lines.append(
            f"  · cmds.{issue.command} (line {issue.line}, col {issue.col}){suggestion}"
        )
    lines.extend([
        "",
        "If you are CERTAIN the command exists (e.g. api_graph.json is stale and",
        "you are on a newer Maya), either:",
        "  - regenerate src/maya_mcp/rag/api_graph.json via",
        "    `mayapy scripts/introspect_maya_api.py`, OR",
        "  - set `ast_dry_run: false` in config.json to bypass F4b.",
    ])
    return "\n".join(lines)
