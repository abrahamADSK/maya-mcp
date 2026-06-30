"""
_review_encode.py
=================
Server-side ``.mov`` assembly for ``review_turntable``'s PNG-sequence fallback.

When Maya's movie encoder (QuickTime / avfoundation) is unavailable, the
``review_build`` playblast falls back to writing a **PNG sequence** and stops
there — no ``.mov``. The maya-mcp **server** process does have ``ffmpeg`` on the
system, so it assembles the ``.mov`` from those PNGs after the recipe returns,
so ``review_turntable`` always delivers a ``.mov`` (Chat 79).

The pure helpers here (fallback detection + ffmpeg arg construction) are
unit-tested; the actual subprocess call (:func:`assemble_mov_from_pngs`) is
invoked from the server off the event loop. Best-effort throughout: if ffmpeg is
absent, no frames were written, or the encode fails, the caller keeps the
original PNG-sequence result rather than raising.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def is_png_fallback(result: dict) -> bool:
    """True when ``review_turntable`` fell back to a PNG sequence (no encoder).

    Mirrors the recipe's ``used`` payload: ``format={"format": "image", …}`` is
    written only when neither ``qt`` nor ``avfoundation`` was available.
    """
    if not isinstance(result, dict) or result.get("error"):
        return False
    fmt = result.get("format")
    return isinstance(fmt, dict) and fmt.get("format") == "image"


def png_base(out_path: str) -> str:
    """The playblast PNG basename — ``review_build`` sets ``filename`` to
    ``splitext(out_path)[0]`` for the image fallback, so frames land at
    ``<base>.<NNNN>.png``."""
    return os.path.splitext(str(out_path))[0]


def ffmpeg_mov_cmd(ffmpeg: str, out_path: str, start: int, end: int, fps: int,
                   pad: int = 4) -> list[str]:
    """Build the ffmpeg arg list assembling ``<base>.%0<pad>d.png`` → ``out_path``.

    ``-pix_fmt yuv420p`` keeps the H.264 broadly playable; ``-frames:v`` bounds
    the encode to the rendered range so a stray PNG can't extend the clip.
    """
    base = png_base(out_path)
    return [
        ffmpeg, "-y",
        "-framerate", str(int(fps)),
        "-start_number", str(int(start)),
        "-i", f"{base}.%0{int(pad)}d.png",
        "-frames:v", str(int(end) - int(start) + 1),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out_path),
    ]


def assemble_mov_from_pngs(out_path: str, start: int, end: int, fps: int) -> bool:
    """Assemble the PNG sequence into ``out_path`` via ffmpeg.

    Returns ``True`` only when a ``.mov`` was produced. Returns ``False`` (caller
    keeps the PNG-sequence result) when ffmpeg is absent, the first frame is not
    on disk, or the encode fails/timeouts — never raises.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    base = png_base(out_path)
    if not os.path.exists(f"{base}.{int(start):04d}.png"):
        return False
    cmd = ffmpeg_mov_cmd(ffmpeg, out_path, start, end, fps)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0 and os.path.exists(out_path)
