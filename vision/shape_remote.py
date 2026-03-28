#!/usr/bin/env python3
"""
shape_remote.py — Generación de geometría 3D con Hunyuan3D-2 Shape.
Ejecutar en el servidor GPU remoto dentro del entorno virtual del proyecto.

Uso:
    .venv/bin/python shape_remote.py --image ref.jpg --output ./output/
    .venv/bin/python shape_remote.py --test

Genera:
    mesh.glb  — geometría sin UVs ni textura, lista para texture_remote.py

Variables de entorno:
    GPU_MODELS_DIR  — directorio donde están los pesos del modelo (default: ~/ai-studio/vision/hf_models)
"""

import argparse
import os
import sys
import traceback
from pathlib import Path


def run_test():
    """Verifica que el entorno tiene los pesos del modelo de shape."""
    try:
        import torch
        print(f"PyTorch   : {torch.__version__}")
        print(f"CUDA      : {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"GPU       : {torch.cuda.get_device_name(0)}")
            vram = torch.cuda.get_device_properties(0).total_memory / 1e9
            print(f"VRAM      : {vram:.1f} GB")
            if vram < 10:
                print("ADVERTENCIA: Se recomiendan ≥10 GB VRAM para shape generation.")
        else:
            print("ERROR: CUDA no disponible.")
            sys.exit(1)
    except ImportError:
        print("ERROR: PyTorch no instalado.")
        sys.exit(1)

    try:
        from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline  # noqa: F401
        print("ShapeGen  : importado OK")
    except ImportError as e:
        print(f"ERROR al importar Hunyuan3D-2 ShapeGen: {e}")
        print("Asegúrate de haber ejecutado: cd Hunyuan3D-2 && pip install -e .")
        sys.exit(1)

    try:
        from hy3dgen.rembg import BackgroundRemover  # noqa: F401
        print("rembg     : importado OK")
    except ImportError:
        print("ADVERTENCIA: rembg no disponible — la imagen debe tener fondo transparente.")

    print("\n✓ Hunyuan3D-2 Shape listo para generar geometría.")


def _load_pipeline():
    """Carga el pipeline de shape generation, prefiriendo ruta local."""
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

    # Directorio con los pesos del modelo. Contiene los subdirectorios:
    #   hunyuan3d-dit-v2-0-fast / hunyuan3d-dit-v2-0-turbo / hunyuan3d-dit-v2-0
    # Prioridad: fast > turbo > full (velocidad) con fallback a HF Hub
    HF_PAINT_TURBO = os.environ.get(
        'GPU_MODELS_DIR',
        os.path.expanduser('~/ai-studio/vision/hf_models')
    )

    candidates = [
        (HF_PAINT_TURBO, 'hunyuan3d-dit-v2-0-fast'),    # más rápido
        (HF_PAINT_TURBO, 'hunyuan3d-dit-v2-0-turbo'),   # equilibrado
        (HF_PAINT_TURBO, 'hunyuan3d-dit-v2-0'),          # calidad máxima
    ]

    for base, sub in candidates:
        path = os.path.join(base, sub)
        model_file = os.path.join(path, 'model.fp16.safetensors')
        if os.path.isdir(path) and os.path.exists(model_file):
            print(f"      Cargando desde ruta local: {path}")
            return Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(base, subfolder=sub)
        elif os.path.isdir(path):
            print(f"      Directorio existe pero faltan pesos: {path} — saltando")

    # Fallback: HF Hub (descarga ~4-6 GB la primera vez)
    print("      Cargando desde HuggingFace Hub (primera vez puede tardar)...")
    return Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        'tencent/Hunyuan3D-2',
        subfolder='hunyuan3d-dit-v2-0-turbo'
    )


def generate_shape(image_path: str, output_dir: str) -> str:
    """
    Genera geometría 3D a partir de una imagen de referencia.
    Devuelve la ruta al mesh.glb generado.

    El pipeline hace background removal automático si la imagen es RGB.
    Output: mesh.glb sin UVs ni textura (~32K verts, ~65K faces).
    """
    import torch
    from PIL import Image

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Cargar pipeline ──────────────────────────────────────────────────
    print("[1/4] Cargando pipeline Hunyuan3D-2 Shape...")
    pipeline = _load_pipeline()
    print("      Pipeline cargado.")

    # ── 2. Cargar imagen ────────────────────────────────────────────────────
    print(f"[2/4] Cargando imagen: {image_path}")
    image = Image.open(image_path).convert("RGBA")

    # Background removal si la imagen no tiene canal alpha útil
    # (si el fondo ya es transparente, omitir)
    bg_is_solid = _detect_solid_background(image)
    if bg_is_solid:
        try:
            from hy3dgen.rembg import BackgroundRemover
            print("      Eliminando fondo (rembg)...")
            rembg = BackgroundRemover()
            image = rembg(image)
            print("      Fondo eliminado.")
        except ImportError:
            print("      ADVERTENCIA: rembg no disponible, usando imagen tal cual.")
    else:
        print("      Imagen con transparencia detectada, omitiendo rembg.")

    # ── 3. Generar geometría ─────────────────────────────────────────────────
    print("[3/4] Generando geometría 3D...")
    print("      (RTX 3090 ~3-8 min)")
    result = pipeline(image=image)
    mesh = result[0]
    print(f"      Geometría generada: {len(mesh.vertices):,} verts | {len(mesh.faces):,} faces")

    # ── 4. Guardar mesh.glb ──────────────────────────────────────────────────
    print("[4/4] Guardando mesh.glb...")
    glb_path = output_dir / "mesh.glb"
    mesh.export(str(glb_path))
    size_kb = glb_path.stat().st_size // 1024
    print(f"      → mesh.glb ({size_kb} KB)")

    torch.cuda.empty_cache()
    print("\n✓ Shape generation completado.")
    return str(glb_path)


def generate_shape_from_text(text_prompt: str, output_dir: str) -> str:
    """
    Genera geometría 3D a partir de un prompt de texto (text-to-3D).
    Devuelve la ruta al mesh.glb generado.

    Usa Hunyuan3D-2 DiT con el mismo pipeline que image-to-3D pero
    pasando text= en lugar de image=.
    Output: mesh.glb sin UVs ni textura (~32K verts, ~65K faces).
    """
    import torch

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Cargar pipeline ──────────────────────────────────────────────────
    print("[1/3] Cargando pipeline Hunyuan3D-2 Shape...")
    pipeline = _load_pipeline()
    print("      Pipeline cargado.")

    # ── 2. Generar geometría desde texto ─────────────────────────────────────
    print(f"[2/3] Generando geometría 3D desde texto: '{text_prompt}'")
    print("      (RTX 3090 ~3-8 min)")
    result = pipeline(text=text_prompt)
    mesh = result[0]
    print(f"      Geometría generada: {len(mesh.vertices):,} verts | {len(mesh.faces):,} faces")

    # ── 3. Guardar mesh.glb ──────────────────────────────────────────────────
    print("[3/3] Guardando mesh.glb...")
    glb_path = output_dir / "mesh.glb"
    mesh.export(str(glb_path))
    size_kb = glb_path.stat().st_size // 1024
    print(f"      → mesh.glb ({size_kb} KB)")

    torch.cuda.empty_cache()
    print("\n✓ Text-to-3D generation completado.")
    return str(glb_path)


def _detect_solid_background(image):
    """Heurística simple: si la imagen no tiene píxeles transparentes, asume fondo sólido."""
    if image.mode != 'RGBA':
        return True
    import numpy as np
    alpha = np.array(image)[:, :, 3]
    transparent_ratio = (alpha < 10).sum() / alpha.size
    return transparent_ratio < 0.05  # menos del 5% transparente = fondo sólido


def main():
    parser = argparse.ArgumentParser(
        description='Hunyuan3D-2 Shape Generation (Linux RTX 3090)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python shape_remote.py --test
  python shape_remote.py --image ref.jpg --output ./output/
  python shape_remote.py --text "american mailbox" --output ./output/
        """
    )
    parser.add_argument('--image',  type=str, help='Imagen de referencia (.jpg/.png)')
    parser.add_argument('--text',   type=str, help='Prompt de texto para text-to-3D')
    parser.add_argument('--output', type=str, default='./output', help='Directorio de salida')
    parser.add_argument('--test',   action='store_true', help='Verificar instalación')
    args = parser.parse_args()

    if args.test:
        run_test()
        return

    if not args.image and not args.text:
        parser.error("Se requiere --image o --text")

    if args.image and args.text:
        parser.error("Usa --image o --text, no ambos")

    try:
        if args.text:
            generate_shape_from_text(args.text, args.output)
        else:
            if not os.path.exists(args.image):
                print(f"ERROR: imagen no encontrada: {args.image}")
                sys.exit(1)
            generate_shape(args.image, args.output)
    except Exception as e:
        print(f"\nERROR durante shape generation: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
