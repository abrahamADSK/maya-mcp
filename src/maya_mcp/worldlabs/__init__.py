"""
maya_mcp.worldlabs
==================
Self-contained connector for the World Labs **Marble** World API
(``https://api.worldlabs.ai``) — image-to-3D-world generation that returns
Gaussian splats (SPZ), an equirectangular panorama, and a collider mesh.

Phase A scope (this package): an API client + asset/format seams only. It does
**not** touch the MCP server, install scripts, or docs — wiring into the
``maya_*`` tool surface is Phase B.

Public API
----------
- :class:`WorldLabsClient` and its error types
- :func:`convert_spz_to_ply` (SPZ -> PLY seam) and its error types
- The Pydantic request/response models
"""

from __future__ import annotations

from .client import (
    GenerationNotConfirmedError,
    MissingAPIKeyError,
    WorldLabsAPIError,
    WorldLabsClient,
    WorldLabsError,
)
from .convert import (
    OPENCV_TO_DCC_SCALE,
    SpzConversionError,
    SpzConversionUnavailable,
    convert_spz_to_ply,
)
from .models import (
    GenerateRequest,
    ImageryAssets,
    ImagePromptMediaAsset,
    ImagePromptUri,
    MeshAssets,
    Operation,
    OperationError,
    SplatAssets,
    World,
    WorldAssets,
    WorldPrompt,
)

__all__ = [
    # client
    "WorldLabsClient",
    "WorldLabsError",
    "WorldLabsAPIError",
    "MissingAPIKeyError",
    "GenerationNotConfirmedError",
    # convert
    "convert_spz_to_ply",
    "SpzConversionError",
    "SpzConversionUnavailable",
    "OPENCV_TO_DCC_SCALE",
    # models
    "GenerateRequest",
    "WorldPrompt",
    "ImagePromptUri",
    "ImagePromptMediaAsset",
    "Operation",
    "OperationError",
    "World",
    "WorldAssets",
    "SplatAssets",
    "ImageryAssets",
    "MeshAssets",
]
