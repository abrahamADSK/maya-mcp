"""Per-tool chaining hints for maya-mcp (mirrors the fpt-mcp pattern).

Design doc: fpt-mcp/docs/O3_NEXT_SUGGESTED_ACTIONS.md (not repeated here).
maya-mcp emits hints for:

- ``maya_vision3d`` dispatcher — the natural workflow is
  ``generate_image → poll (repeated) → download → execute_python import``.
- ``maya_create_primitive`` — after a primitive exists, offer material
  assignment as the typical next step.
- ``maya_import_file`` — after a successful import, offer save_scene so
  the imported geometry doesn't live only in memory.

Each step's response lets the next step pre-fill its key params. The
``select_server`` / ``health`` Vision3D actions and other Maya direct
tools have no interesting follow-up; only the high-value chains ship
rules.

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


_PRIMITIVE_TYPES = {"cube", "sphere", "cylinder", "cone", "plane", "torus"}


def _suggest_after_maya_create_primitive(response: dict[str, Any]) -> list[Suggestion]:
    """Rule — after creating a primitive, offer material assignment.

    Trigger: response carries ``name`` (non-empty) and ``type`` matching a
    known primitive kind. Error responses (``error`` key present) are
    short-circuited.
    """
    if "error" in response:
        return []
    obj_name = response.get("name")
    obj_type = response.get("type")
    if not isinstance(obj_name, str) or not obj_name:
        return []
    if obj_type not in _PRIMITIVE_TYPES:
        return []
    return [{
        "tool": "maya_assign_material",
        "reason": f"Assign a material to the new {obj_type} '{obj_name}'.",
        "params_hint": {
            "object_name": obj_name,
            "material_type": "aiStandardSurface",
        },
    }]


def _suggest_after_maya_import_file(response: dict[str, Any]) -> list[Suggestion]:
    """Rule — after a non-empty import, offer save_scene.

    Trigger: response carries ``imported`` > 0. Imports that land zero
    new transforms (empty file, plugin failure that swallows its own
    error) produce no hint.
    """
    if "error" in response:
        return []
    imported = response.get("imported")
    if not isinstance(imported, int) or imported <= 0:
        return []
    reason = (
        f"Save the scene so the imported {imported} object(s) persist."
        if imported > 1
        else "Save the scene so the imported object persists."
    )
    return [{
        "tool": "maya_session",
        "reason": reason,
        "params_hint": {"action": "save_scene"},
    }]


def _suggest_after_maya_create_camera(response: dict[str, Any]) -> list[Suggestion]:
    """Rule — after creating a camera, offer a viewport capture through it.

    Trigger: response carries a non-empty ``camera`` string. Useful for
    "did the framing land where I wanted?" feedback loops in shot-layout
    work.
    """
    if "error" in response:
        return []
    cam = response.get("camera")
    if not isinstance(cam, str) or not cam:
        return []
    return [{
        "tool": "maya_viewport_capture",
        "reason": f"Preview the scene through the new camera '{cam}'.",
        "params_hint": {
            "camera": cam,
            "output_path": f"/tmp/{cam}_preview.png",
        },
    }]


def _suggest_after_maya_create_light(response: dict[str, Any]) -> list[Suggestion]:
    """Rule — after creating a light, offer an intensity keyframe.

    Trigger: response carries a ``light`` string and matching ``type``.
    The hint seeds a frame-1 intensity keyframe as the starting point
    of a light animation — users typically follow up with another
    frame at a later time for the actual interpolation.
    """
    if "error" in response:
        return []
    light = response.get("light")
    light_type = response.get("type")
    if not isinstance(light, str) or not light:
        return []
    return [{
        "tool": "maya_set_keyframe",
        "reason": (
            f"Set the initial intensity keyframe for the new {light_type} light "
            f"'{light}' (animation groundwork)."
        ),
        "params_hint": {
            "object_name": light,
            "attribute": "intensity",
            "value": 1.0,
            "frame": 1,
        },
    }]


# tool_name → callable(parsed_response_dict) -> list[Suggestion]
SUGGESTION_RULES: dict[str, Callable[[dict[str, Any]], list[Suggestion]]] = {
    "maya_vision3d": _suggest_after_maya_vision3d,
    "maya_create_primitive": _suggest_after_maya_create_primitive,
    "maya_import_file": _suggest_after_maya_import_file,
    "maya_create_camera": _suggest_after_maya_create_camera,
    "maya_create_light": _suggest_after_maya_create_light,
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
