"""
client.py
=========
``WorldLabsClient`` — a self-contained REST client for the World Labs **Marble**
World API (``https://api.worldlabs.ai``).

It mirrors the submit -> poll -> download shape, timeout discipline, and
error-handling style of the existing Vision3D REST client in
``maya_mcp.server`` (``_build_http_client`` / ``_do_v3d_*`` /
``_download_file``), but lives in its own module and is **synchronous**:
generation is a long-running, caller-loops-on-poll flow that reads naturally as
blocking code (``upload_image`` / ``generate`` / ``poll`` / ``wait`` /
``download_assets``). Phase B can wrap it for the async MCP dispatcher.

Auth
----
Every API request carries the ``WLT-Api-Key`` header. The key is taken from the
``WORLDLABS_API_KEY`` environment variable unless supplied explicitly. Signed
upload/download URLs (on the storage host, not ``api.worldlabs.ai``) are issued
through a *separate* client that does **not** attach the API key, since signed
URLs reject unexpected auth headers.

Cost guardrail ("previo")
-------------------------
:meth:`WorldLabsClient.generate` refuses to spend credits unless called with
``confirm=True``. Without confirmation it raises
:class:`GenerationNotConfirmedError`, naming the model and the approximate
credit cost. Only a confirmed call issues the request, and each confirmed
generation is recorded via :meth:`WorldLabsClient.log_generation`.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

import httpx

from .models import (
    GenerateRequest,
    ImagePromptMediaAsset,
    ImagePromptUri,
    Operation,
    World,
    WorldPrompt,
)

logger = logging.getLogger("maya_mcp.worldlabs")

_DEFAULT_BASE_URL = "https://api.worldlabs.ai"
_API_PREFIX = "/marble/v1"
_API_KEY_ENV = "WORLDLABS_API_KEY"
_AUTH_HEADER = "WLT-Api-Key"

# Approximate credit cost per generation (docs.worldlabs.ai/api/pricing,
# 2026-06). Surfaced in the confirmation guardrail so the user sees the spend
# before it happens. This client never bills — World Labs meters server-side —
# so the figures are advisory.
_APPROX_CREDIT_COST: dict[str, int] = {
    "marble-1.0-draft": 150,
    "marble-1.0": 1500,
    "marble-1.1": 1500,
    "marble-1.1-plus": 3000,  # base 1500 + up to 1500 for variable world size
}


# ── Exceptions ─────────────────────────────────────────────────────────────


class WorldLabsError(Exception):
    """Base class for all World Labs connector errors."""


class MissingAPIKeyError(WorldLabsError):
    """Raised when no API key is available (neither argument nor env var)."""


class GenerationNotConfirmedError(WorldLabsError):
    """Raised by :meth:`WorldLabsClient.generate` when ``confirm`` is not True.

    This is the cost guardrail: it fires *before* any network request, so no
    credits are spent. ``model`` and ``approx_credits`` are exposed for callers
    that want to render their own confirmation prompt.
    """

    def __init__(self, model: str, approx_credits: Optional[int] = None) -> None:
        self.model = model
        self.approx_credits = approx_credits
        cost = f"~{approx_credits} credits" if approx_credits else "credits"
        super().__init__(
            f"Confirmation required: generating a world with model '{model}' will "
            f"spend {cost} on World Labs. This is a paid, non-refundable operation. "
            f"Re-call generate(..., confirm=True) to proceed."
        )


class WorldLabsAPIError(WorldLabsError):
    """Raised when the API (or a signed-URL request) returns a 4xx/5xx status."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        body: str = "",
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.url = url
        snippet = f" — {body[:300]}" if body else ""
        super().__init__(f"World Labs API error {status_code} ({message}) at {url}{snippet}")


# ── Client ─────────────────────────────────────────────────────────────────


class WorldLabsClient:
    """Synchronous REST client for the World Labs Marble World API.

    Parameters
    ----------
    api_key:
        WLT API key. Defaults to ``$WORLDLABS_API_KEY``. Resolved lazily — a
        missing key only raises :class:`MissingAPIKeyError` when an API call is
        actually made.
    base_url:
        API base. Defaults to ``https://api.worldlabs.ai``.
    timeout:
        Per-request timeout for API calls (float seconds or an ``httpx.Timeout``).
        Uploads/downloads use a separate, more generous I/O budget.
    verify:
        TLS verification toggle (passed through to httpx).
    transport:
        Optional ``httpx.BaseTransport`` — primarily a test seam so a
        ``MockTransport`` can intercept both API and signed-URL requests.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        *,
        verify: bool = True,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        resolved = api_key if api_key is not None else os.environ.get(_API_KEY_ENV, "")
        self.api_key = (resolved or "").strip()
        self.base_url = base_url.rstrip("/")
        self.verify = verify
        self._transport = transport

        if timeout is None:
            timeout = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)
        elif isinstance(timeout, (int, float)):
            timeout = httpx.Timeout(float(timeout))
        self.timeout = timeout

        # Generous budget for large asset transfers (full_res SPZ can be big).
        self._io_timeout = httpx.Timeout(connect=10.0, read=900.0, write=900.0, pool=10.0)

        self._api: Optional[httpx.Client] = None
        self._ext: Optional[httpx.Client] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _api_client(self) -> httpx.Client:
        """Lazily build the API-host client (carries the WLT-Api-Key header)."""
        if self._api is None:
            if not self.api_key:
                raise MissingAPIKeyError(
                    f"No World Labs API key. Pass api_key=... or set {_API_KEY_ENV}."
                )
            self._api = httpx.Client(
                base_url=self.base_url,
                headers={_AUTH_HEADER: self.api_key},
                timeout=self.timeout,
                verify=self.verify,
                transport=self._transport,
            )
        return self._api

    def _ext_client(self) -> httpx.Client:
        """Lazily build the client for signed storage URLs (no API key header)."""
        if self._ext is None:
            self._ext = httpx.Client(
                timeout=self._io_timeout,
                verify=self.verify,
                transport=self._transport,
            )
        return self._ext

    def close(self) -> None:
        """Close both underlying httpx clients."""
        for client in (self._api, self._ext):
            if client is not None:
                client.close()
        self._api = None
        self._ext = None

    def __enter__(self) -> "WorldLabsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _check(resp: httpx.Response, context: str) -> None:
        """Raise :class:`WorldLabsAPIError` on a 4xx/5xx response."""
        if resp.status_code >= 400:
            try:
                body = resp.text
            except Exception:  # pragma: no cover - defensive
                body = ""
            raise WorldLabsAPIError(
                resp.status_code, context, body=body, url=str(resp.url)
            )

    # ── media upload ─────────────────────────────────────────────────────

    def upload_image(self, path: Union[str, Path]) -> str:
        """Upload a local image and return its ``media_asset_id``.

        Two steps, mirroring the documented workflow:
        1. ``POST /marble/v1/media-assets:prepare_upload`` -> signed ``upload_url``
        2. ``PUT`` the file bytes to that URL (signed host, no API key).
        """
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(f"Image not found: {src}")

        extension = src.suffix.lstrip(".").lower() or "png"
        payload = {
            "file_name": src.name[:64],
            "kind": "image",
            "extension": extension,
        }

        resp = self._api_client().post(
            f"{_API_PREFIX}/media-assets:prepare_upload", json=payload
        )
        self._check(resp, "media-assets:prepare_upload")
        data = resp.json()

        media_asset = data.get("media_asset") or {}
        upload_info = data.get("upload_info") or {}
        media_asset_id = media_asset.get("media_asset_id")
        upload_url = upload_info.get("upload_url")
        if not media_asset_id or not upload_url:
            raise WorldLabsAPIError(
                resp.status_code,
                "prepare_upload response missing media_asset_id/upload_url",
                body=resp.text,
                url=str(resp.url),
            )

        method = str(upload_info.get("upload_method") or "PUT").upper()
        headers = dict(upload_info.get("required_headers") or {})
        headers.setdefault(
            "Content-Type",
            mimetypes.guess_type(src.name)[0] or "application/octet-stream",
        )

        up = self._ext_client().request(
            method, upload_url, content=src.read_bytes(), headers=headers
        )
        self._check(up, "signed media upload")
        logger.info("uploaded %s -> media_asset_id=%s", src.name, media_asset_id)
        return media_asset_id

    # ── generation ───────────────────────────────────────────────────────

    def _build_image_prompt(
        self, image_path_or_uri: Union[str, Path]
    ) -> Union[ImagePromptUri, ImagePromptMediaAsset]:
        """Turn a path or URI into the right ``image_prompt`` variant.

        An http(s) string is referenced by URI; anything else is treated as a
        local file and uploaded first (yielding a ``media_asset`` reference).
        """
        value = str(image_path_or_uri)
        if value.startswith("http://") or value.startswith("https://"):
            return ImagePromptUri(uri=value)
        return ImagePromptMediaAsset(media_asset_id=self.upload_image(value))

    def generate(
        self,
        image_path_or_uri: Union[str, Path],
        model: str = "marble-1.1",
        display_name: Optional[str] = None,
        text_prompt: Optional[str] = None,
        *,
        confirm: bool = False,
    ) -> str:
        """Submit an image-to-world generation and return the ``operation_id``.

        COST GUARDRAIL: unless ``confirm=True``, this raises
        :class:`GenerationNotConfirmedError` *before any network call* — no
        credits are spent. With ``confirm=True`` it uploads the image if needed,
        issues ``POST /marble/v1/worlds:generate``, logs the confirmed spend via
        :meth:`log_generation`, and returns the operation id to poll.
        """
        if not confirm:
            raise GenerationNotConfirmedError(model, _APPROX_CREDIT_COST.get(model))

        image_prompt = self._build_image_prompt(image_path_or_uri)
        request = GenerateRequest(
            display_name=display_name,
            model=model,
            world_prompt=WorldPrompt(image_prompt=image_prompt, text_prompt=text_prompt),
        )

        resp = self._api_client().post(
            f"{_API_PREFIX}/worlds:generate",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        self._check(resp, "worlds:generate")
        operation = Operation.model_validate(resp.json())
        self.log_generation(model, operation.operation_id)
        return operation.operation_id

    def log_generation(self, model: str, operation_id: str) -> str:
        """Record a confirmed, credit-spending generation. Returns the timestamp.

        Mirrors maya-mcp's module-logger style. The timestamp is an ISO-8601
        placeholder for Phase A; Phase B should route confirmed spends into the
        durable audit substrate (``maya_mcp._audit`` /
        ``_session_stats.persist_timing`` -> ``logs/audit.jsonl``).
        """
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        logger.info(
            "world generation confirmed: model=%s operation_id=%s ts=%s",
            model,
            operation_id,
            ts,
        )
        return ts

    # ── polling ──────────────────────────────────────────────────────────

    def poll(self, operation_id: str) -> Operation:
        """Single poll of ``GET /marble/v1/operations/{operation_id}``."""
        resp = self._api_client().get(f"{_API_PREFIX}/operations/{operation_id}")
        self._check(resp, f"operations/{operation_id}")
        return Operation.model_validate(resp.json())

    def wait(
        self,
        operation_id: str,
        *,
        interval: float = 15.0,
        timeout: float = 1800.0,
        on_status: Optional[Callable[[Operation], None]] = None,
    ) -> Operation:
        """Poll until the operation is done (or ``timeout`` seconds elapse).

        Generation typically takes ~5 min, so the default cadence is a 15 s
        poll for up to 30 min. ``on_status`` is invoked with each polled
        :class:`Operation` for incremental progress; without it, status is
        logged. Raises ``TimeoutError`` if the deadline passes first.
        """
        deadline = time.monotonic() + timeout
        while True:
            op = self.poll(operation_id)
            if on_status is not None:
                on_status(op)
            else:
                logger.info(
                    "operation %s: %s", operation_id, "done" if op.done else "running"
                )
            if op.done:
                return op
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"operation {operation_id} not done after {timeout}s"
                )
            time.sleep(interval)

    # ── account ──────────────────────────────────────────────────────────

    def get_credit_balance(self) -> dict[str, Any]:
        """Return the raw ``GET /marble/v1/credits`` payload (credit balance).

        The endpoint is documented (docs.worldlabs.ai/api/pricing) but its exact
        response field names are not published, so the parsed JSON dict is
        returned verbatim rather than wrapped in a strict model.
        """
        resp = self._api_client().get(f"{_API_PREFIX}/credits")
        self._check(resp, "credits")
        result: dict[str, Any] = resp.json()
        return result

    # ── downloads ────────────────────────────────────────────────────────

    def _resolve_asset_urls(
        self, world: World
    ) -> dict[str, tuple[Optional[str], str]]:
        """Map asset selectors to ``(url, filename)`` pairs for a World."""
        assets = world.assets
        splats = assets.splats if assets else None
        imagery = assets.imagery if assets else None
        mesh = assets.mesh if assets else None
        spz = splats.spz_urls if splats else {}
        return {
            "splats_100k": (spz.get("100k"), "splats_100k.spz"),
            "splats_500k": (spz.get("500k"), "splats_500k.spz"),
            "splats_full_res": (spz.get("full_res"), "splats_full_res.spz"),
            "pano": (imagery.pano_url if imagery else None, "pano.png"),
            "mesh": (mesh.collider_mesh_url if mesh else None, "collider_mesh.glb"),
        }

    def _download_url(self, url: str, target: Path) -> None:
        """Stream a signed URL to ``target`` (no API key header)."""
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._ext_client().stream("GET", url) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise WorldLabsAPIError(
                    resp.status_code, "asset download", body=resp.text, url=str(resp.url)
                )
            with open(target, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        logger.info("downloaded %s -> %s", url, target)

    def download_assets(
        self,
        world: Union[World, Operation],
        dest_dir: Union[str, Path],
        which: tuple[str, ...] = ("splats_full_res", "pano", "mesh"),
    ) -> dict[str, Path]:
        """Download selected assets of a completed World to ``dest_dir``.

        ``world`` may be a :class:`World` or a done :class:`Operation` (its
        ``response`` is used). Valid selectors: ``splats_100k``,
        ``splats_500k``, ``splats_full_res``, ``pano``, ``mesh``. Selectors
        whose URL is absent on this World are skipped (logged), so the returned
        dict only contains assets actually written.
        """
        resolved = world.response if isinstance(world, Operation) else world
        if resolved is None:
            raise WorldLabsError(
                "operation has no World response yet (is done=true?)"
            )

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        plan = self._resolve_asset_urls(resolved)

        out: dict[str, Path] = {}
        for key in which:
            spec = plan.get(key)
            if spec is None:
                logger.warning("unknown asset selector %r; skipping", key)
                continue
            url, filename = spec
            if not url:
                logger.warning("asset %r not available on this world; skipping", key)
                continue
            target = dest / filename
            self._download_url(url, target)
            out[key] = target
        return out
