"""
models.py
=========
Pydantic models for the World Labs **Marble** World API (``api.worldlabs.ai``).

All models declare ``extra="forbid"`` so a typo in a request field or an
unexpected key in a parsed response is rejected loudly instead of silently
ignored. The field set mirrors ``docs.worldlabs.ai`` as of 2026-06; response
models carry the documented fields with permissive defaults so a partial
payload still validates, while still rejecting *unknown* top-level keys.

Covered surfaces
----------------
- ``GenerateRequest``           — body for ``POST /marble/v1/worlds:generate``
- ``ImagePromptUri`` /
  ``ImagePromptMediaAsset``     — the two ``image_prompt.source`` variants
- ``Operation``                 — the long-running operation envelope
- ``World`` + ``*Assets``       — the World object returned in ``Operation.response``

Coordinate system note
----------------------
World Labs assets use an OpenCV camera frame (+x left, +y down, +z forward).
Re-orienting into a Maya/DCC frame (scale Y and Z by -1) is a *Phase B* import
concern; see :data:`maya_mcp.worldlabs.convert.OPENCV_TO_DCC_SCALE`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ── image_prompt variants (discriminated on ``source``) ────────────────────


class ImagePromptUri(BaseModel):
    """An ``image_prompt`` that references a publicly reachable image URI."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["uri"] = "uri"
    uri: str = Field(description="Publicly reachable https URI of the source image.")


class ImagePromptMediaAsset(BaseModel):
    """An ``image_prompt`` that references a previously uploaded media asset."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["media_asset"] = "media_asset"
    media_asset_id: str = Field(
        description="ID returned by media-assets:prepare_upload after the PUT upload."
    )


# Discriminated union: Pydantic picks the variant by the literal ``source`` key.
ImagePrompt = Annotated[
    Union[ImagePromptUri, ImagePromptMediaAsset],
    Field(discriminator="source"),
]


class WorldPrompt(BaseModel):
    """The ``world_prompt`` block of a generate request (image-driven)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["image"] = "image"
    image_prompt: ImagePrompt
    text_prompt: Optional[str] = Field(
        default=None, description="Optional text guidance accompanying the image."
    )


class GenerateRequest(BaseModel):
    """Body for ``POST /marble/v1/worlds:generate``.

    ``model`` is kept a free ``str`` (validated non-empty) rather than a
    ``Literal`` so newly released Marble models do not break the client; the
    documented values are ``marble-1.1`` and ``marble-1.1-plus`` (plus the
    legacy ``marble-1.0`` / ``marble-1.0-draft``).
    """

    # ``protected_namespaces=()`` silences Pydantic's ``model_*`` guard for the
    # legitimately-named ``model`` field.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    display_name: Optional[str] = Field(default=None, max_length=64)
    model: str = Field(default="marble-1.1", min_length=1)
    world_prompt: WorldPrompt
    seed: Optional[int] = Field(default=None, ge=0, le=4_294_967_295)
    tags: Optional[list[str]] = None


# ── World / asset response models ──────────────────────────────────────────


class SplatAssets(BaseModel):
    """Gaussian-splat asset URLs (SPZ format, keyed by resolution tier)."""

    model_config = ConfigDict(extra="forbid")

    spz_urls: dict[str, str] = Field(
        default_factory=dict,
        description="Maps '100k' / '500k' / 'full_res' to signed SPZ download URLs.",
    )


class ImageryAssets(BaseModel):
    """2D imagery assets (equirectangular panorama)."""

    model_config = ConfigDict(extra="forbid")

    pano_url: Optional[str] = Field(
        default=None, description="Equirectangular panorama PNG (2560x1280, LDR)."
    )


class MeshAssets(BaseModel):
    """Mesh assets (collision proxy)."""

    model_config = ConfigDict(extra="forbid")

    collider_mesh_url: Optional[str] = Field(
        default=None, description="Collider mesh GLB URL."
    )


class WorldAssets(BaseModel):
    """The ``assets`` block of a :class:`World`."""

    model_config = ConfigDict(extra="forbid")

    splats: Optional[SplatAssets] = None
    imagery: Optional[ImageryAssets] = None
    mesh: Optional[MeshAssets] = None
    caption: Optional[str] = None
    thumbnail_url: Optional[str] = None


class World(BaseModel):
    """A generated World, returned inside ``Operation.response`` when done."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    world_id: str
    display_name: Optional[str] = None
    world_marble_url: Optional[str] = None
    assets: Optional[WorldAssets] = None
    model: Optional[str] = None
    tags: Optional[list[str]] = None
    permission: Optional[dict[str, Any]] = None
    world_prompt: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ── Operation envelope ─────────────────────────────────────────────────────


class OperationError(BaseModel):
    """The ``error`` block of an :class:`Operation` (populated on failure)."""

    model_config = ConfigDict(extra="forbid")

    code: Optional[int] = None
    message: Optional[str] = None


class Operation(BaseModel):
    """Long-running operation envelope for ``worlds:generate`` / ``operations/{id}``.

    Poll ``GET /marble/v1/operations/{operation_id}`` until ``done`` is True;
    ``response`` then holds the :class:`World`.
    """

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    done: bool = False
    response: Optional[World] = None
    error: Optional[OperationError] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    cost: Optional[int] = None
