"""Resolve the ShotGrid project for the Maya console session, from the engine.

When Maya is launched via Toolkit (``tank``) into a Task/Asset/Shot, the
``tk-maya`` engine carries an AUTHORITATIVE context (project + entity + task).
The console reads its project id at launch and binds the spawned ``claude``'s
fpt-mcp ShotGrid operations to it via ``SHOTGRID_PROJECT_ID`` — no guessing,
mirroring how fpt-mcp's own console resolves a project (Chat 69).

Plain (non-tank) Maya has no engine context → ``"0"`` ("no project"), so a
project-scoped fpt-mcp create fails loudly instead of writing to a stale ``.env``
default. ``sgtk`` is imported lazily inside the function so this module imports
fine outside Maya (and in CI).
"""
from __future__ import annotations


def resolve_engine_project() -> int | None:
    """Best-effort ShotGrid project id from the running Toolkit engine, or None.

    MUST be called on Maya's main thread (it touches the sgtk engine). Never
    raises — any problem (no sgtk, no engine, no project) maps to ``None``.
    """
    try:
        import sgtk

        eng = sgtk.platform.current_engine()
        ctx = getattr(eng, "context", None) if eng is not None else None
        proj = getattr(ctx, "project", None) if ctx is not None else None
        if proj and proj.get("id"):
            return int(proj["id"])
    except Exception:
        pass
    return None


def project_env(project_id) -> dict:
    """``SHOTGRID_PROJECT_ID`` override for the spawned ``claude`` subprocess.

    A resolved (engine) project → that project; otherwise ``"0"`` ("no project")
    so a project-scoped fpt-mcp create fails loudly rather than hitting a stale
    ``.env`` default. Coercion guards against a non-numeric value.
    """
    if project_id:
        try:
            n = int(project_id)
            if n > 0:
                return {"SHOTGRID_PROJECT_ID": str(n)}
        except (TypeError, ValueError):
            pass
    return {"SHOTGRID_PROJECT_ID": "0"}
