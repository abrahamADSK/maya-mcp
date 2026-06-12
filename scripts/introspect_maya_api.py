#!/usr/bin/env python3
"""
introspect_maya_api.py
======================
Walk the live ``maya.cmds`` module and emit a structured JSON description of
every command that actually exists at runtime — never what a docstring, the
RAG corpus, or a chat transcript *claims* exists. The output
(``src/maya_mcp/rag/api_graph.json``) is the source of truth for the F4b AST
dry-run validator (``maya_mcp._ast_validate``), which rejects ``execute_python``
snippets that call a hallucinated ``cmds.<command>`` before the socket
round-trip to Maya happens.

Operational requirement
-----------------------
``maya.cmds`` is ONLY importable inside Maya's Python. This script therefore
CANNOT run under a normal system Python — run it with **mayapy** (Maya's
bundled interpreter), which can initialise ``maya.standalone`` headlessly and
load the full command set without a GUI:

    /Applications/Autodesk/maya2026/Maya.app/Contents/bin/mayapy \\
        scripts/introspect_maya_api.py

On Linux/Windows the mayapy path differs; any mayapy ≥ the supported Maya
version works. Run inside a running Maya session via the command port also
works (the body is import-safe).

Plugins
-------
Plugin-registered commands (``mayaUSDImport``, ``AbcImport``, ``FBXExport`` …)
only appear in ``dir(maya.cmds)`` once their plugin is loaded. A headless
``maya.standalone`` session loads none of them, so a graph built without this
step omits every plugin command and the F4b validator then false-positives on a
legitimate ``cmds.mayaUSDImport(...)`` call (Chat 56 incident). This script
therefore best-effort loads the pipeline I/O + USD + Arnold plugins
(``_PIPELINE_PLUGINS``, which now includes ``mtoa``) before introspecting; a
plugin that is absent on the running build is skipped, never fatal. ``mtoa`` is
wired into the default set so the regenerated graph carries Arnold commands
(``arnoldRender``, ``arnoldExportAss`` …) — without them F4b statically rejects
every corpus-recommended Arnold render call (audit blocker). Append still more
via the ``MAYA_INTROSPECT_PLUGINS`` env var (comma-separated).

Cadence
-------
Re-run once per supported Maya major release (e.g. 2026 -> 2027) and commit the
regenerated ``api_graph.json`` alongside the version bump. Patch releases rarely
change the command surface; rerunning is optional but cheap.

Exit codes
----------
0  graph written
2  ``maya.cmds`` not importable (not running under mayapy / Maya)
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

# Output path: src/maya_mcp/rag/api_graph.json (the validator resolves the same
# location relative to the maya_mcp package).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_PATH = _REPO_ROOT / "src" / "maya_mcp" / "rag" / "api_graph.json"

# Pipeline I/O + USD plugins whose commands the LLM legitimately calls via
# ``cmds.<command>`` (mayaUSDImport, AbcImport, FBXExport …). They are NOT in
# ``dir(cmds)`` until their plugin is loaded, and a headless maya.standalone
# session loads none of them — so introspecting without loading these omits the
# whole plugin command surface and F4b false-positives on real calls (Chat 56).
# Best-effort: a plugin absent on the running build is skipped, never fatal.
# Extend at runtime via the MAYA_INTROSPECT_PLUGINS env var (comma-separated).
_PIPELINE_PLUGINS = (
    "mayaUsdPlugin",   # mayaUSDImport / mayaUSDExport / mayaUsdLayerEditor / …
    "AbcImport",       # Alembic import
    "AbcExport",       # Alembic export
    "fbxmaya",         # FBXImport / FBXExport / FBXProperties / …
    "objExport",       # OBJ import/export
    "mtoa",            # Arnold: arnoldRender / arnoldExportAss / arnoldRenderView / …
    # NB: glTF (libgltfsceneimport) and OBJ are FILE TRANSLATORS invoked via
    # cmds.file(type='glTF Import'/'OBJ') — they register no cmds.<command> of
    # their own, so glTF needs no entry here (cmds.file is core). objExport is
    # listed only because its load is cheap and harmless.
    # mtoa is wired in (was previously env-var-only) so the regenerated graph
    # carries Arnold commands; its headless load is licence-gated and a little
    # slow, but the per-plugin try/except below keeps an unlicensed box from
    # failing the run — mtoa simply lands in `plugins_failed`.
)


def _load_pipeline_plugins() -> tuple[list[str], list[str]]:
    """Best-effort load of ``_PIPELINE_PLUGINS`` (+ ``MAYA_INTROSPECT_PLUGINS``).

    Each plugin is loaded independently inside its own ``try``; one that is
    absent on this Maya build or fails to initialise headlessly is recorded as
    failed, never aborts the run. Returns ``(loaded, failed)`` for ``_meta`` so
    the graph documents exactly which plugin surfaces it covers.
    """
    import os

    import maya.cmds as cmds  # noqa: E402 — only importable inside Maya

    plugins = list(_PIPELINE_PLUGINS)
    plugins += [
        p.strip()
        for p in os.environ.get("MAYA_INTROSPECT_PLUGINS", "").split(",")
        if p.strip()
    ]

    loaded: list[str] = []
    failed: list[str] = []
    for plugin in plugins:
        try:
            if not cmds.pluginInfo(plugin, query=True, loaded=True):
                cmds.loadPlugin(plugin, quiet=True)
            loaded.append(plugin)
        except Exception:  # noqa: BLE001 — absent/incompatible plugin is non-fatal
            failed.append(plugin)
    return loaded, failed


def _collect_commands() -> dict:
    """Return ``{name: {}}`` for every public callable in ``maya.cmds``.

    The empty per-command dict is intentional: F4b validates command
    *existence* (the false-positive-free, high-value check), mirroring
    flame-mcp's F4b which validates symbol existence, not usage. The dict
    leaves room for a future ``flags`` list without a schema migration.
    """
    import maya.cmds as cmds  # noqa: E402 — only importable inside Maya

    commands = {}
    for name in dir(cmds):
        if name.startswith("_"):
            continue
        if callable(getattr(cmds, name, None)):
            commands[name] = {}
    return commands


def main() -> int:
    try:
        import maya.standalone  # noqa: F401

        # Headless init so dir(cmds) returns the full command surface even
        # when launched as a plain `mayapy script.py` (no GUI / no session).
        try:
            maya.standalone.initialize(name="python")
        except Exception:
            # Already initialised (e.g. running inside a live Maya) — fine.
            pass
    except ImportError:
        # Running inside a live Maya session without the standalone module
        # path? Fall through; the cmds import below is the real gate.
        pass

    # Load pipeline plugins so their commands land in dir(cmds) before we walk
    # it. Guarded: if cmds is not importable here, _collect_commands below emits
    # the canonical "run under mayapy" error and exits 2.
    try:
        plugins_loaded, plugins_failed = _load_pipeline_plugins()
    except ImportError:
        plugins_loaded, plugins_failed = [], list(_PIPELINE_PLUGINS)

    try:
        commands = _collect_commands()
    except ImportError:
        sys.stderr.write(
            "error: maya.cmds is not importable. Run this script with mayapy "
            "(Maya's bundled interpreter), not a system Python.\n"
        )
        return 2

    try:
        version = _maya_version()
    except Exception:
        version = "unknown"

    graph = {
        "_meta": {
            "maya_version": version,
            "commands_total": len(commands),
            "plugins_loaded": sorted(plugins_loaded),
            "plugins_failed": sorted(plugins_failed),
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "introspector": "scripts/introspect_maya_api.py",
        },
        "commands": dict(sorted(commands.items())),
    }

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(
        json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    sys.stderr.write(
        f"wrote {len(commands)} commands (Maya {version}) -> {_OUTPUT_PATH}\n"
        f"  plugins loaded: {', '.join(sorted(plugins_loaded)) or '(none)'}\n"
        f"  plugins failed: {', '.join(sorted(plugins_failed)) or '(none)'}\n"
    )
    return 0


def _maya_version() -> str:
    import maya.cmds as cmds

    return str(cmds.about(version=True))


if __name__ == "__main__":
    raise SystemExit(main())
