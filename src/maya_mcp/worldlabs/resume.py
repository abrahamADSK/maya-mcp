"""Resume sidecar for the World Labs pipeline.

The pipeline is several discrete steps — generate → poll → download → convert
→ build — and the expensive one (``generate``) spends credits. To make a failed
or interrupted run resumable WITHOUT re-generating, the work area carries a
small JSON sidecar recording the ``operation_id`` (the re-download token) and
the ``world_id``; the resumable state is otherwise derived from which artifacts
already exist on disk.

Resume rules (:func:`scan_state`):

* a ``.ply`` exists        → ready to build (no World Labs call)
* a ``.spz`` exists        → convert it (local, no World Labs call)
* the sidecar has an op id  → poll/download (World Labs call; the operation
                              expires ~1 h after creation — past that the
                              download token is gone and a re-generate is the
                              only option)
* nothing                  → generate

Only the narrow "generated but the SPZ never landed, and >1 h elapsed" case
forces a paid re-generate; every other interruption resumes from disk.

The module is pure: file I/O only, no network and no clock — the caller injects
the timestamp (``now_iso``) so writes stay deterministic and unit-testable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

SIDECAR_NAME = ".worldlabs.json"

# World Labs operations carry an ``expires_at`` ~1 h after creation; documented
# here so the resume hint can warn that the download token is time-bounded.
OPERATION_TTL_SECONDS = 3600


def sidecar_path(work_dir: str | Path) -> Path:
    return Path(work_dir) / SIDECAR_NAME


def read_sidecar(work_dir: str | Path) -> Optional[dict[str, Any]]:
    """Return the parsed sidecar, or ``None`` if absent/unreadable."""
    p = sidecar_path(work_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_sidecar(work_dir: str | Path, *, now_iso: str, **fields: Any) -> dict[str, Any]:
    """Merge ``fields`` into the work-area sidecar and persist it.

    ``None`` values are ignored (so a partial update never clears a field). The
    first write stamps ``created_at``; every write stamps ``updated_at``.
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    data = read_sidecar(work) or {}
    data.setdefault("created_at", now_iso)
    data["updated_at"] = now_iso
    data.update({k: v for k, v in fields.items() if v is not None})
    sidecar_path(work).write_text(json.dumps(data, indent=2))
    return data


def _newest(work: Path, suffix: str) -> Optional[str]:
    """Newest versioned artifact with ``suffix`` (``{name}.v###`` sorts last)."""
    hits = sorted(work.glob(f"*{suffix}"))
    return str(hits[-1]) if hits else None


def scan_state(work_dir: str | Path) -> dict[str, Any]:
    """Derive the resumable state from the sidecar + the artifacts on disk."""
    work = Path(work_dir)
    sidecar = read_sidecar(work) or {}
    spz = _newest(work, ".spz")
    ply = _newest(work, ".ply")
    pano = _newest(work, ".png")
    exists = {"spz": spz, "ply": ply, "pano": pano}

    if ply:
        status = "ready_to_build"
        nxt = "build the Maya scene from the PLY (+ pano); no World Labs call."
    elif spz:
        status = "needs_convert"
        nxt = "convert the SPZ to PLY (local; no World Labs call)."
    elif sidecar.get("operation_id"):
        status = "needs_download"
        nxt = (
            f"poll then download with operation_id={sidecar['operation_id']!r} "
            "(the operation expires ~1 h after creation; past that a paid "
            "re-generate is required)."
        )
    else:
        status = "needs_generate"
        nxt = "generate a new world (spends credits)."

    return {
        "status": status,
        "operation_id": sidecar.get("operation_id"),
        "world_id": sidecar.get("world_id"),
        "exists": exists,
        "next_step": nxt,
    }
