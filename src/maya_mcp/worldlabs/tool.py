"""
tool.py
=======
Action logic for the ``maya_worldlabs`` MCP dispatcher, kept out of ``server.py``
so it is unit-testable without Maya. Each public function returns a JSON string
(the dispatcher's contract, mirroring the ``maya_vision3d`` handlers).

NON-Maya actions (pure HTTP + local subprocess): ``health``, ``generate``,
``poll``, ``download``, ``convert``. The Maya-side ``build`` action is wired in
``server.py`` (it needs the Command Port bridge); :func:`build_maya_code` here
just assembles the code string to send.

Cost guardrail ("confirm before spend", the chosen invariant): :func:`generate`
refuses to spend credits unless ``confirm=True`` — the client raises
``GenerationNotConfirmedError`` *before any network call*; we surface a
``confirmation_required`` payload the LLM shows the user.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import (
    GenerationNotConfirmedError,
    MissingAPIKeyError,
    WorldLabsClient,
    WorldLabsError,
)
from .convert import (
    SpzConversionError,
    SpzConversionUnavailable,
    convert_spz_to_ply,
)
from .models import Operation
from . import resume


def _client() -> WorldLabsClient:
    """Build a client (reads ``WORLDLABS_API_KEY`` from env)."""
    return WorldLabsClient()


def health() -> str:
    """Validate the API key is present and (best-effort) report credit balance."""
    # WorldLabsClient.__init__ never raises MissingAPIKeyError — it resolves the
    # key lazily; only _api_client() raises it on the first actual call.  Check
    # the resolved key directly so health() correctly reports absence.
    client = _client()
    if not client.api_key:
        return json.dumps({
            "error": "WORLDLABS_API_KEY not set",
            "hint": "Export WORLDLABS_API_KEY in the environment (.env). The key "
                    "is auth, not an endpoint — it is persisted, unlike the "
                    "per-session Vision3D URL.",
        })
    out: dict[str, object] = {"status": "ok", "api_key": "present", "base_url": client.base_url}
    try:
        out["credits"] = client.get_credit_balance()
    except Exception as exc:  # noqa: BLE001 - balance is best-effort, never fatal
        out["credits"] = {"warning": f"unavailable: {exc}"}
    return json.dumps(out, indent=2)


def generate(
    image: str,
    output_subdir: str,
    model: str = "marble-1.1",
    display_name: Optional[str] = None,
    text_prompt: Optional[str] = None,
    confirm: bool = False,
    work_dir: Optional[str] = None,
) -> str:
    """Submit an image→world generation. Spends credits ONLY with confirm=True.

    When ``work_dir`` is given (the Toolkit work area, resolved by the caller),
    the ``operation_id`` and inputs are persisted to a resume sidecar there so an
    interrupted run can resume the download without re-generating.
    """
    try:
        client = _client()
    except MissingAPIKeyError:
        return json.dumps({"error": "WORLDLABS_API_KEY not set"})
    try:
        op_id = client.generate(
            image, model=model, display_name=display_name,
            text_prompt=text_prompt, confirm=confirm,
        )
    except GenerationNotConfirmedError as exc:
        return json.dumps({
            "status": "confirmation_required",
            "model": exc.model,
            "approx_credits": exc.approx_credits,
            "message": str(exc),
            "next_step": "Show the user the credit cost; on approval re-call "
                         "generate with confirm=true.",
        })
    except WorldLabsError as exc:
        return json.dumps({"error": str(exc)})
    except FileNotFoundError as exc:
        # upload_image raises FileNotFoundError for missing local paths; it is
        # not a subclass of WorldLabsError, so it must be caught separately.
        return json.dumps({"error": f"image not found: {exc}"})
    if work_dir:
        resume.write_sidecar(
            work_dir,
            now_iso=datetime.now(timezone.utc).isoformat(),
            operation_id=op_id,
            model=model,
            image=image,
            text_prompt=text_prompt,
            status="generating",
        )
    return json.dumps({
        "status": "started",
        "operation_id": op_id,
        "output_subdir": output_subdir,
        "work_dir": work_dir,
        "next_step": f"poll with operation_id={op_id!r} until done (~5 min).",
    })


def poll(operation_id: str) -> str:
    """Poll a generation operation; on completion report the available assets."""
    try:
        client = _client()
    except MissingAPIKeyError:
        return json.dumps({"error": "WORLDLABS_API_KEY not set"})
    try:
        op: Operation = client.poll(operation_id)
    except WorldLabsError as exc:
        return json.dumps({"error": str(exc)})
    result = {"operation_id": op.operation_id, "done": op.done, "cost": op.cost}
    if op.error is not None:
        result["error"] = {"code": op.error.code, "message": op.error.message}
    if op.done and op.response is not None:
        world = op.response
        assets = world.assets
        result["world_id"] = world.world_id
        result["available"] = {
            "splats_spz": sorted((assets.splats.spz_urls or {}).keys())
            if (assets and assets.splats) else [],
            "pano": bool(assets and assets.imagery and assets.imagery.pano_url),
            "mesh": bool(assets and assets.mesh and assets.mesh.collider_mesh_url),
        }
        result["next_step"] = "download (splats_full_res + pano), then convert."
    elif not op.done:
        result["next_step"] = "still running — poll again."
    return json.dumps(result, indent=2)


def download(
    operation_id: str,
    dest_dir: str,
    which: tuple = ("splats_full_res", "pano"),
) -> str:
    """Download assets of a completed operation to ``dest_dir``."""
    try:
        client = _client()
    except MissingAPIKeyError:
        return json.dumps({"error": "WORLDLABS_API_KEY not set"})
    try:
        op = client.poll(operation_id)
        if not op.done:
            return json.dumps({
                "status": "not_ready",
                "operation_id": operation_id,
                "next_step": "poll until done before download.",
            })
        paths = client.download_assets(op, dest_dir, which=tuple(which))
    except WorldLabsError as exc:
        return json.dumps({"error": str(exc)})
    out = {k: str(v) for k, v in paths.items()}
    spz = out.get("splats_full_res") or out.get("splats_500k") or out.get("splats_100k")
    world_id = op.response.world_id if op.response else None
    resume.write_sidecar(
        dest_dir,
        now_iso=datetime.now(timezone.utc).isoformat(),
        operation_id=operation_id,
        world_id=world_id,
        status="downloaded",
        downloaded=out,
    )
    return json.dumps({
        "status": "ok",
        "downloaded": out,
        "next_step": (f"convert the SPZ to PLY: spz_path={spz!r}" if spz
                      else "no SPZ downloaded — check 'which'."),
    }, indent=2)


def convert(spz_path: str, ply_path: Optional[str] = None) -> str:
    """Convert a downloaded SPZ splat to PLY (Arnold-readable) via gsbox."""
    try:
        out = convert_spz_to_ply(spz_path, ply_path)
    except FileNotFoundError as exc:
        return json.dumps({"error": f"SPZ not found: {exc}"})
    except SpzConversionUnavailable as exc:
        return json.dumps({"error": str(exc),
                           "hint": "Install gsbox (or set WORLDLABS_SPZ_CONVERTER)."})
    except SpzConversionError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({
        "status": "ok",
        "ply_path": str(out),
        "next_step": "build the Maya scene (load PLY + pano dome + eye camera).",
    })


def status(work_dir: str) -> str:
    """Report the resumable state of a World Labs work area.

    Reads the resume sidecar and scans the directory for ``.spz``/``.ply``/
    ``.png`` artifacts, returning where the pipeline left off and the next step
    (``needs_generate`` / ``needs_download`` / ``needs_convert`` /
    ``ready_to_build``) so an interrupted run resumes without re-generating.
    """
    return json.dumps(resume.scan_state(work_dir), indent=2)


def build_maya_code(
    ply_path: str,
    pano_path: Optional[str] = None,
    eye_height: float = 1.5,
    proxy_step: int = 1,
    relight: bool = False,
) -> str:
    """Assemble the in-Maya code (maya_build.py source + a build_environment call).

    The dispatcher sends the returned string to Maya via the Command Port bridge.
    """
    src = (Path(__file__).parent / "maya_build.py").read_text()
    call = (
        "\nimport json as _json\n"
        f"result = _json.dumps(build_environment("
        f"ply_path={ply_path!r}, pano_path={pano_path!r}, "
        f"eye_height={float(eye_height)!r}, proxy_step={int(proxy_step)!r}, "
        f"relight={bool(relight)!r}))\n"
    )
    return src + call
