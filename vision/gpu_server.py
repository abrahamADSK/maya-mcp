#!/usr/bin/env python3
"""
gpu_server.py — FastAPI inference server for Hunyuan3D-2.

Runs on the GPU machine and exposes REST endpoints
for shape generation (image-to-3D, text-to-3D) and texture painting.
Replaces the previous SSH-based communication.

Endpoints:
    POST /api/generate-shape    — image → mesh.glb
    POST /api/generate-text     — text prompt → mesh.glb
    POST /api/texture-mesh      — mesh + image → textured mesh
    GET  /api/health            — health check
    GET  /api/jobs/{job_id}     — job status + download

Usage:
    # Direct (development)
    .venv/bin/python gpu_server.py --port 8000

    # Production (behind Caddy)
    .venv/bin/python gpu_server.py --host 127.0.0.1 --port 8000

Environment:
    GPU_API_KEY       — required API key for authentication
    GPU_MODELS_DIR    — Hunyuan3D model weights (default: ~/ai-studio/vision/hf_models)
    GPU_WORK_DIR      — working directory for outputs (default: ~/ai-studio/reference/3d_output)
"""

import asyncio
import hashlib
import os
import secrets
import shutil
import tempfile
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Header, UploadFile
from fastapi.responses import FileResponse, JSONResponse

# ── Configuration ────────────────────────────────────────────────────────────

API_KEY = os.environ.get("GPU_API_KEY", "")
MODELS_DIR = os.environ.get(
    "GPU_MODELS_DIR",
    os.path.expanduser("~/ai-studio/vision/hf_models"),
)
WORK_DIR = Path(
    os.environ.get(
        "GPU_WORK_DIR",
        os.path.expanduser("~/ai-studio/reference/3d_output"),
    )
)
VISION_DIR = Path(
    os.environ.get(
        "GPU_VISION_DIR",
        os.path.expanduser("~/ai-studio/vision"),
    )
)

# ── Job tracking ─────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}


def _new_job(job_type: str, detail: str = "") -> str:
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "id": job_id,
        "type": job_type,
        "status": "running",
        "detail": detail,
        "created": time.time(),
        "output_dir": None,
        "files": [],
        "error": None,
        "log": [],
    }
    return job_id


def _job_log(job_id: str, msg: str):
    if job_id in _jobs:
        _jobs[job_id]["log"].append(msg)


def _job_done(job_id: str, output_dir: str, files: list[str]):
    if job_id in _jobs:
        _jobs[job_id]["status"] = "completed"
        _jobs[job_id]["output_dir"] = output_dir
        _jobs[job_id]["files"] = files


def _job_fail(job_id: str, error: str):
    if job_id in _jobs:
        _jobs[job_id]["status"] = "failed"
        _jobs[job_id]["error"] = error


# ── Authentication ───────────────────────────────────────────────────────────


def _verify_api_key(x_api_key: Optional[str]):
    """Verify API key if one is configured."""
    if not API_KEY:
        return  # No API key configured — open access (LAN only)
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Pipeline loaders (lazy, cached) ─────────────────────────────────────────

_shape_pipeline = None
_paint_pipeline = None


def _get_shape_pipeline():
    global _shape_pipeline
    if _shape_pipeline is None:
        from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

        candidates = [
            (MODELS_DIR, "hunyuan3d-dit-v2-0-fast"),
            (MODELS_DIR, "hunyuan3d-dit-v2-0-turbo"),
            (MODELS_DIR, "hunyuan3d-dit-v2-0"),
        ]
        for base, sub in candidates:
            path = os.path.join(base, sub)
            model_file = os.path.join(path, "model.fp16.safetensors")
            if os.path.isdir(path) and os.path.exists(model_file):
                print(f"[Shape] Loading from local: {path}")
                _shape_pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
                    base, subfolder=sub
                )
                return _shape_pipeline

        print("[Shape] Loading from HuggingFace Hub...")
        _shape_pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
            "tencent/Hunyuan3D-2", subfolder="hunyuan3d-dit-v2-0-turbo"
        )
    return _shape_pipeline


def _get_paint_pipeline():
    global _paint_pipeline
    if _paint_pipeline is None:
        from hy3dgen.texgen.pipelines import Hunyuan3DPaintPipeline

        if os.path.isdir(MODELS_DIR):
            print(f"[Paint] Loading from local: {MODELS_DIR}")
            _paint_pipeline = Hunyuan3DPaintPipeline.from_pretrained(
                MODELS_DIR, subfolder="hunyuan3d-paint-v2-0-turbo"
            )
        else:
            print("[Paint] Loading from HuggingFace Hub...")
            _paint_pipeline = Hunyuan3DPaintPipeline.from_pretrained(
                "tencent/Hunyuan3D-2", subfolder="hunyuan3d-paint-v2-0-turbo"
            )
    return _paint_pipeline


# ── Inference functions (run in thread pool) ─────────────────────────────────


def _run_shape_from_image(image_path: str, output_dir: str, job_id: str) -> dict:
    """Image → 3D shape generation (blocking, runs in thread)."""
    import torch
    from PIL import Image
    import numpy as np

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    _job_log(job_id, "[1/4] Loading shape pipeline...")
    pipeline = _get_shape_pipeline()

    _job_log(job_id, f"[2/4] Loading image: {image_path}")
    image = Image.open(image_path).convert("RGBA")

    # Background removal if needed
    alpha = np.array(image)[:, :, 3]
    if (alpha < 10).sum() / alpha.size < 0.05:
        try:
            from hy3dgen.rembg import BackgroundRemover

            _job_log(job_id, "      Removing background...")
            image = BackgroundRemover()(image)
        except ImportError:
            pass

    _job_log(job_id, "[3/4] Generating 3D shape (~3-8 min)...")
    result = pipeline(image=image)
    mesh = result[0]
    _job_log(
        job_id,
        f"      Generated: {len(mesh.vertices):,} verts | {len(mesh.faces):,} faces",
    )

    _job_log(job_id, "[4/4] Saving mesh.glb...")
    glb_path = output / "mesh.glb"
    mesh.export(str(glb_path))
    size_kb = glb_path.stat().st_size // 1024

    torch.cuda.empty_cache()

    return {"mesh_path": str(glb_path), "mesh_size_kb": size_kb}


def _run_shape_from_text(text_prompt: str, output_dir: str, job_id: str) -> dict:
    """Text → 3D shape generation (blocking, runs in thread)."""
    import torch

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    _job_log(job_id, "[1/3] Loading shape pipeline...")
    pipeline = _get_shape_pipeline()

    _job_log(job_id, f"[2/3] Generating from text: '{text_prompt}' (~3-8 min)...")
    result = pipeline(text=text_prompt)
    mesh = result[0]
    _job_log(
        job_id,
        f"      Generated: {len(mesh.vertices):,} verts | {len(mesh.faces):,} faces",
    )

    _job_log(job_id, "[3/3] Saving mesh.glb...")
    glb_path = output / "mesh.glb"
    mesh.export(str(glb_path))
    size_kb = glb_path.stat().st_size // 1024

    torch.cuda.empty_cache()

    return {"mesh_path": str(glb_path), "mesh_size_kb": size_kb}


def _run_texture(
    mesh_path: str, image_path: str, output_dir: str, job_id: str
) -> dict:
    """Texture painting (blocking, runs in thread)."""
    import torch
    import trimesh
    from PIL import Image

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    _job_log(job_id, "[1/4] Loading paint pipeline...")
    pipeline = _get_paint_pipeline()

    _job_log(job_id, f"[2/4] Loading mesh: {mesh_path}")
    mesh = trimesh.load(mesh_path, force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)

    _job_log(job_id, f"[3/4] Texturing with image (~2-5 min)...")
    image = Image.open(image_path)
    textured = pipeline(mesh, image)

    _job_log(job_id, "[4/4] Saving results...")
    files = []

    glb_out = output / "textured.glb"
    textured.export(str(glb_out))
    files.append("textured.glb")

    obj_out = output / "mesh_uv.obj"
    textured.export(str(obj_out))
    files.append("mesh_uv.obj")

    tex_out = output / "texture_baked.png"
    tex_saved = False
    try:
        mat = textured.visual.material
        if hasattr(mat, "image") and mat.image is not None:
            mat.image.save(str(tex_out))
            tex_saved = True
    except Exception:
        pass
    if not tex_saved:
        try:
            tv = textured.visual
            if hasattr(tv, "to_texture"):
                tv.to_texture().image.save(str(tex_out))
                tex_saved = True
        except Exception:
            pass
    if tex_saved:
        files.append("texture_baked.png")

    torch.cuda.empty_cache()

    return {"files": files, "output_dir": str(output)}


# ── Background task runner ───────────────────────────────────────────────────


async def _run_in_background(job_id: str, func, *args):
    """Run a blocking inference function in a thread, update job status."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, func, *args)
        output_dir = result.get("output_dir") or str(
            Path(result.get("mesh_path", "")).parent
        )
        files = result.get("files", [])
        if not files and "mesh_path" in result:
            files = ["mesh.glb"]
        _job_done(job_id, output_dir, files)
    except Exception as e:
        _job_fail(job_id, f"{e}\n{traceback.format_exc()}")


# ── FastAPI app ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[GPU Server] Models dir: {MODELS_DIR}")
    print(f"[GPU Server] Work dir:   {WORK_DIR}")
    print(f"[GPU Server] API key:    {'configured' if API_KEY else 'NONE (open access)'}")
    yield


app = FastAPI(
    title="Hunyuan3D-2 GPU Server",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health():
    """Health check — returns GPU info if available."""
    info = {"status": "ok", "api_key_required": bool(API_KEY)}
    try:
        import torch

        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
            info["vram_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / 1e9, 1
            )
    except ImportError:
        info["gpu"] = "torch not available"
    return info


@app.post("/api/generate-shape")
async def generate_shape(
    image: UploadFile = File(...),
    output_subdir: str = Form("0"),
    x_api_key: Optional[str] = Header(None),
):
    """Upload an image, get a 3D mesh back (async job)."""
    _verify_api_key(x_api_key)

    job_id = _new_job("shape-image", f"subdir={output_subdir}")
    out_dir = WORK_DIR / output_subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save uploaded image
    image_path = out_dir / "input.png"
    content = await image.read()
    image_path.write_bytes(content)

    # Launch inference in background
    asyncio.create_task(
        _run_in_background(job_id, _run_shape_from_image, str(image_path), str(out_dir), job_id)
    )

    return {"job_id": job_id, "status": "running", "poll": f"/api/jobs/{job_id}"}


@app.post("/api/generate-text")
async def generate_text(
    text_prompt: str = Form(...),
    output_subdir: str = Form("0"),
    x_api_key: Optional[str] = Header(None),
):
    """Generate 3D mesh from text prompt (async job)."""
    _verify_api_key(x_api_key)

    job_id = _new_job("shape-text", f"prompt={text_prompt[:50]}")
    out_dir = WORK_DIR / output_subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    asyncio.create_task(
        _run_in_background(job_id, _run_shape_from_text, text_prompt, str(out_dir), job_id)
    )

    return {"job_id": job_id, "status": "running", "poll": f"/api/jobs/{job_id}"}


@app.post("/api/texture-mesh")
async def texture_mesh(
    mesh: UploadFile = File(...),
    image: UploadFile = File(...),
    output_subdir: str = Form("0"),
    x_api_key: Optional[str] = Header(None),
):
    """Upload mesh + image, get textured mesh back (async job)."""
    _verify_api_key(x_api_key)

    job_id = _new_job("texture", f"subdir={output_subdir}")
    out_dir = WORK_DIR / output_subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    mesh_path = out_dir / "mesh.glb"
    mesh_content = await mesh.read()
    mesh_path.write_bytes(mesh_content)

    image_path = out_dir / "input.png"
    image_content = await image.read()
    image_path.write_bytes(image_content)

    asyncio.create_task(
        _run_in_background(
            job_id, _run_texture, str(mesh_path), str(image_path), str(out_dir), job_id
        )
    )

    return {"job_id": job_id, "status": "running", "poll": f"/api/jobs/{job_id}"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, x_api_key: Optional[str] = Header(None)):
    """Poll job status. When completed, includes download links."""
    _verify_api_key(x_api_key)

    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = {
        "id": job["id"],
        "type": job["type"],
        "status": job["status"],
        "elapsed_s": round(time.time() - job["created"], 1),
        "log": job["log"],
    }

    if job["status"] == "completed":
        result["files"] = [
            {"name": f, "download": f"/api/jobs/{job_id}/files/{f}"}
            for f in job["files"]
        ]
    elif job["status"] == "failed":
        result["error"] = job["error"]

    return result


@app.get("/api/jobs/{job_id}/files/{filename}")
async def download_file(
    job_id: str, filename: str, x_api_key: Optional[str] = Header(None)
):
    """Download a result file from a completed job."""
    _verify_api_key(x_api_key)

    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="Job not yet completed")
    if filename not in job["files"]:
        raise HTTPException(status_code=404, detail=f"File '{filename}' not in job")

    file_path = Path(job["output_dir"]) / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(str(file_path), filename=filename)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hunyuan3D-2 GPU API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()

    uvicorn.run(
        "gpu_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
