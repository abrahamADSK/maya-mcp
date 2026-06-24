"""
convert.py
==========
SPZ -> PLY conversion seam for World Labs Marble splat assets.

Why this exists
---------------
The Marble API hands back Gaussian splats in **SPZ** (Niantic's compact format,
~10x smaller than PLY) — *not* PLY. Arnold's point-cloud / splat path in Maya
consumes **PLY**, so a converter is required before import (Phase B).

Investigation (2026-06) — is there a clean pip ``import``-and-call path?
------------------------------------------------------------------------
No stable one, as of this writing:

* **Niantic ``spz``** (github.com/nianticlabs/spz) ships nanobind Python
  bindings, but it is **not** a plain ``pip install spz`` — it needs a git
  clone + C++/nanobind build, and its load/save-to-PLY Python API is not yet
  API-stable.
* **PyPI ``spz``** (Jackneill, v0.0.1, 2026-01) is too immature; its PLY
  round-trip surface is undocumented.
* **``3dgsconverter``** (pip-installable) *does* convert SPZ <-> PLY, but it is
  a **CLI**, not a library you import.

Decision
--------
Phase A implements a **documented subprocess seam** over a configurable
converter binary (``WORLDLABS_SPZ_CONVERTER`` env var, else the first of
:data:`_CONVERTER_CANDIDATES` found on ``PATH``). This keeps Phase A unblocked
and fully testable (the seam is unit-tested with a faked subprocess; a real
conversion test is ``skipif``-gated on a binary + sample being present).

TODO (Phase B)
--------------
Adopt the native Niantic ``spz`` Python binding behind a lazy import once its
``load()`` / PLY-save API stabilizes, keeping this subprocess path as a
fallback. Do **not** add it as a hard dependency.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger("maya_mcp.worldlabs.convert")

# World Labs assets use an OpenCV camera frame (+x left, +y down, +z forward).
# Maya/DCC frames are +y up / +z back, so importing a World asset requires
# scaling Y and Z by -1. This is the documented seam for the Phase B importer;
# convert_spz_to_ply does NOT re-orient the cloud (the converter preserves
# coordinates — the flip belongs at import time).
OPENCV_TO_DCC_SCALE = (1.0, -1.0, -1.0)

# Env var naming an explicit SPZ->PLY converter executable (absolute path or a
# name on PATH). Takes precedence over the auto-detected candidates.
_CONVERTER_ENV = "WORLDLABS_SPZ_CONVERTER"

# CLI converter candidates, in preference order (see module docstring).
_CONVERTER_CANDIDATES = ("spz", "3dgsconverter")


class SpzConversionError(RuntimeError):
    """Raised when a converter is available but the conversion itself failed."""


class SpzConversionUnavailable(RuntimeError):
    """Raised when no SPZ->PLY converter binary can be located."""


def _resolve_converter_bin(explicit: Optional[str] = None) -> Optional[str]:
    """Return a usable converter executable path, or ``None`` if none found.

    Resolution order: explicit argument -> ``$WORLDLABS_SPZ_CONVERTER`` ->
    :data:`_CONVERTER_CANDIDATES` on ``PATH``.
    """
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get(_CONVERTER_ENV, "").strip()
    if env:
        candidates.append(env)
    candidates.extend(_CONVERTER_CANDIDATES)

    for cand in candidates:
        if os.path.isabs(cand) and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


def _build_command(converter_bin: str, spz_path: Path, ply_path: Path) -> list[str]:
    """Build the conversion command line for the resolved converter.

    ``3dgsconverter`` uses ``-i/-o/-f``; a generic ``spz``-style CLI is invoked
    as ``spz convert <in> <out>``. The exact invocation is documented here so a
    different converter can be wired by editing one place.
    """
    name = Path(converter_bin).name.lower()
    if "3dgsconverter" in name:
        return [converter_bin, "-i", str(spz_path), "-o", str(ply_path), "-f", "3dgs"]
    return [converter_bin, "convert", str(spz_path), str(ply_path)]


def convert_spz_to_ply(
    spz_path: Union[str, Path],
    ply_path: Optional[Union[str, Path]] = None,
    *,
    converter_bin: Optional[str] = None,
    timeout: float = 300.0,
) -> Path:
    """Convert an SPZ Gaussian-splat file to PLY and return the output path.

    Parameters
    ----------
    spz_path:
        Source ``.spz`` file (must exist).
    ply_path:
        Destination ``.ply``. Defaults to ``spz_path`` with a ``.ply`` suffix.
    converter_bin:
        Explicit converter executable; otherwise auto-resolved (see
        :func:`_resolve_converter_bin`).
    timeout:
        Subprocess timeout in seconds.

    Raises
    ------
    FileNotFoundError
        If ``spz_path`` does not exist.
    SpzConversionUnavailable
        If no converter binary can be located.
    SpzConversionError
        If the converter runs but fails / times out / produces no output.
    """
    src = Path(spz_path)
    if not src.is_file():
        raise FileNotFoundError(f"SPZ file not found: {src}")

    out = Path(ply_path) if ply_path is not None else src.with_suffix(".ply")
    out.parent.mkdir(parents=True, exist_ok=True)

    binary = _resolve_converter_bin(converter_bin)
    if binary is None:
        raise SpzConversionUnavailable(
            "No SPZ->PLY converter found. Install one of "
            f"{_CONVERTER_CANDIDATES} (e.g. `pip install 3dgsconverter`) or set "
            f"{_CONVERTER_ENV}=/path/to/converter. Phase B will add the native "
            "Niantic `spz` Python binding behind a lazy import."
        )

    cmd = _build_command(binary, src, out)
    logger.info("converting SPZ->PLY: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise SpzConversionError(
            f"converter failed (rc={exc.returncode}): {(exc.stderr or '').strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SpzConversionError(
            f"converter timed out after {timeout}s"
        ) from exc

    if not out.is_file():
        raise SpzConversionError(
            f"converter reported success but {out} was not written "
            f"(stdout={(proc.stdout or '').strip()!r})"
        )
    return out
