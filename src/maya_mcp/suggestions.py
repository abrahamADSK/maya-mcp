"""Per-tool chaining hints for maya-mcp (mirrors the fpt-mcp pattern).

Design doc: fpt-mcp/docs/O3_NEXT_SUGGESTED_ACTIONS.md (not repeated here).
maya-mcp emits hints only for the Vision3D dispatcher, where the natural
workflow is:

    generate_image  ─▶  poll (repeated)  ─▶  download  ─▶  execute_python import

Each step's response lets the next step pre-fill its key params. The
``select_server`` / ``health`` actions have no interesting follow-up; only
the pipeline actions ship rules.

The feature is strictly additive: tools without an entry in
``SUGGESTION_RULES`` see no change, rule errors are swallowed, responses
already carrying ``next_suggested_actions`` are returned untouched.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, TypedDict


class Suggestion(TypedDict, total=False):
    tool: str
    reason: str
    params_hint: dict[str, Any]


def _suggest_after_maya_vision3d(response: dict[str, Any]) -> list[Suggestion]:
    """Three-step Vision3D chain emitted from the single dispatcher tool.

    The dispatcher returns different shapes per action, so the rule reads
    shape-diagnostic keys (``status``, ``job_id``, ``output_dir``) rather
    than needing the original ``action`` value.
    """
    if "error" in response:
        return []

    status = response.get("status")
    job_id = response.get("job_id")

    # generate_image / generate_text: `status == "started"` + job_id.
    if status == "started" and job_id:
        return [{
            "tool": "maya_vision3d",
            "reason": "Poll the job to follow the 3D-generation progress.",
            "params_hint": {"action": "poll", "params": {"job_id": job_id}},
        }]

    # poll: `status == "completed"` + files list populated.
    if status == "completed" and response.get("files"):
        return [{
            "tool": "maya_vision3d",
            "reason": "Download the completed job's meshes to disk.",
            "params_hint": {"action": "download", "params": {"output_subdir": "<subdir>"}},
        }]

    # download: `status == "ok"` + output_dir present.
    output_dir = response.get("output_dir")
    if status == "ok" and output_dir and response.get("textured"):
        return [{
            "tool": "maya_session",
            "reason": "Import the textured mesh into the current Maya scene.",
            "params_hint": {
                "action": "execute_python",
                "params": {
                    "code": (
                        f"import maya.cmds as cmds; "
                        f"cmds.file('{output_dir}/textured.glb', i=True)"
                    ),
                },
            },
        }]

    return []


# tool_name → callable(parsed_response_dict) -> list[Suggestion]
SUGGESTION_RULES: dict[str, Callable[[dict[str, Any]], list[Suggestion]]] = {
    "maya_vision3d": _suggest_after_maya_vision3d,
}


def _suggestions_disabled() -> bool:
    """Kill switch. Set MAYA_MCP_DISABLE_SUGGESTIONS=1 to bypass hints."""
    return os.environ.get("MAYA_MCP_DISABLE_SUGGESTIONS", "").strip() in ("1", "true", "yes")


def maybe_annotate_with_suggestions(tool_name: str, response: str) -> str:
    """Return ``response`` possibly enriched with ``next_suggested_actions``.

    maya-mcp tool functions serialize to JSON strings, so the helper takes
    a string, parses it, and re-serializes. See fpt-mcp/suggestions.py for
    the shared contract.

    Guarantees:
    - Invalid JSON / non-object response → returned verbatim.
    - Unknown tool_name → returned verbatim.
    - Response already contains ``next_suggested_actions`` → returned verbatim.
    - Rule callable raises → original response returned (hints must never
      break the tool).
    """
    if _suggestions_disabled():
        return response
    rule = SUGGESTION_RULES.get(tool_name)
    if rule is None:
        return response

    try:
        parsed = json.loads(response)
    except (ValueError, TypeError):
        return response
    if not isinstance(parsed, dict):
        return response
    if "next_suggested_actions" in parsed:
        return response

    try:
        suggestions = rule(parsed) or []
    except Exception:
        return response

    if not suggestions:
        return response

    parsed["next_suggested_actions"] = suggestions[:3]
    return json.dumps(parsed, default=str)
