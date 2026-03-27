#!/usr/bin/env python3
"""
texture_remote.py — Texturizado 3D con Hunyuan3D-Paint.
Ejecutar en el servidor GPU remoto dentro del entorno virtual del proyecto.

Uso:
    .venv/bin/python texture_remote.py --mesh mesh.glb --image ref.jpg --output ./output/
    .venv/bin/python texture_remote.py --test

Variables de entorno:
    GPU_MODELS_DIR  — directorio donde están los pesos del modelo (default: ~/ai-studio/vision/hf_models)
"""

import argparse
import os
import sys
import traceback
from pathlib import Path


def run_test():
    """Verifica que el entorno está correctamente instalado."""
    try:
        import torch
        print(f"PyTorch   : {torch.__version__}")
        print(f"CUDA      : {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"GPU       : {torch.cuda.get_device_name(0)}")
            vram = torch.cuda.get_device_properties(0).total_memory / 1e9
            print(f"VRAM      : {vram:.1f} GB")
        else:
            print("ERROR: CUDA no disponible. Verifica la instalación de PyTorch.")
            sys.exit(1)
    except ImportError:
        print("ERROR: PyTorch no instalado.")
        sys.exit(1)

    try:
        import trimesh
        print(f"trimesh   : {trimesh.__version__}")
    except ImportError:
        print("ERROR: trimesh no instalado.")
        sys.exit(1)

    try:
        from hy3dgen.texgen.pipelines import Hunyuan3DPaintPipeline  # noqa: F401
        print("Hunyuan3D : importado OK")
    except ImportError as e:
        print(f"ERROR al importar Hunyuan3D-Paint: {e}")
        print("Asegúrate de haber ejecutado: cd Hunyuan3D-2 && pip install -e .")
        sys.exit(1)

    print("\n✓ Hunyuan3D-Paint listo para texturizar.")


def texture_mesh(mesh_path: str, image_path: str, output_dir: str):
    """
    Texturiza un mesh GLB con una imagen de referencia.
    Genera tres archivos en output_dir:
      - textured.glb       (GLB con textura embebida)
      - mesh_uv.obj        (OBJ con UVs para Maya)
      - texture_baked.png  (textura PNG para USE_BAKED_TEXTURE)
    """
    import torch
    import trimesh
    from PIL import Image
    from hy3dgen.texgen.pipelines import Hunyuan3DPaintPipeline

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Cargar pipeline ──────────────────────────────────────────────────
    print("[1/4] Cargando pipeline Hunyuan3D-Paint...")
    # Intentar primero ruta local (sin depender del cache HF)
    # GPU_MODELS_DIR debe apuntar al directorio raíz que contiene los subdirectorios
    # de cada componente (hunyuan3d-paint-v2-0-turbo, hunyuan3d-delight-v2-0, etc.)
    _local = os.environ.get(
        'GPU_MODELS_DIR',
        os.path.expanduser('~/ai-studio/vision/hf_models')
    )
    if os.path.isdir(_local):
        print(f"      Cargando desde ruta local: {_local}")
        pipeline = Hunyuan3DPaintPipeline.from_pretrained(
            _local,
            subfolder='hunyuan3d-paint-v2-0-turbo'
        )
    else:
        print("      Cargando desde HuggingFace Hub...")
        pipeline = Hunyuan3DPaintPipeline.from_pretrained(
            'tencent/Hunyuan3D-2',
            subfolder='hunyuan3d-paint-v2-0-turbo'
        )
    print("      Pipeline cargado.")

    # ── 2. Cargar mesh ──────────────────────────────────────────────────────
    print(f"[2/4] Cargando mesh: {mesh_path}")
    mesh = trimesh.load(mesh_path, force='mesh')
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)
    print(f"      Vértices: {len(mesh.vertices):,} | Caras: {len(mesh.faces):,}")

    # ── 3. Texturizar ───────────────────────────────────────────────────────
    print(f"[3/4] Texturizando con imagen: {image_path}")
    print("      (RTX 3090 ~2-5 min)")
    image = Image.open(image_path)
    textured_mesh = pipeline(mesh, image)
    print("      Texturizado completado.")

    # ── 4. Guardar resultados ───────────────────────────────────────────────
    print(f"[4/4] Guardando resultados en: {output_dir}")

    glb_path = output_dir / "textured.glb"
    textured_mesh.export(str(glb_path))
    print(f"      → textured.glb")

    obj_path = output_dir / "mesh_uv.obj"
    textured_mesh.export(str(obj_path))
    print(f"      → mesh_uv.obj")

    tex_path = output_dir / "texture_baked.png"
    _saved_tex = False

    try:
        mat = textured_mesh.visual.material
        if hasattr(mat, 'image') and mat.image is not None:
            mat.image.save(str(tex_path))
            _saved_tex = True
    except Exception:
        pass

    if not _saved_tex:
        # Fallback: extraer desde TextureVisuals
        try:
            tv = textured_mesh.visual
            if hasattr(tv, 'to_texture'):
                tv.to_texture().image.save(str(tex_path))
                _saved_tex = True
        except Exception:
            pass

    if not _saved_tex:
        print("      ADVERTENCIA: no se pudo extraer texture_baked.png")
        print("      Usa textured.glb directamente.")
    else:
        print(f"      → texture_baked.png")

    torch.cuda.empty_cache()

    print("\n✓ Texturizado completado.")
    return {
        "glb":     str(glb_path),
        "obj":     str(obj_path),
        "texture": str(tex_path) if _saved_tex else None
    }


def main():
    parser = argparse.ArgumentParser(
        description='Texturizado Hunyuan3D-Paint (Linux RTX 3090)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python texture_remote.py --test
  python texture_remote.py --mesh mesh.glb --image ref.jpg --output ./out/
        """
    )
    parser.add_argument('--mesh',   type=str, help='Ruta al mesh (.glb o .obj)')
    parser.add_argument('--image',  type=str, help='Ruta a la imagen de referencia (.jpg/.png)')
    parser.add_argument('--output', type=str, default='./output', help='Directorio de salida (default: ./output)')
    parser.add_argument('--test',   action='store_true', help='Verificar instalación sin texturizar')
    args = parser.parse_args()

    if args.test:
        run_test()
        return

    if not args.mesh or not args.image:
        parser.error("Se requieren --mesh y --image")

    if not os.path.exists(args.mesh):
        print(f"ERROR: mesh no encontrado: {args.mesh}")
        sys.exit(1)

    if not os.path.exists(args.image):
        print(f"ERROR: imagen no encontrada: {args.image}")
        sys.exit(1)

    try:
        texture_mesh(args.mesh, args.image, args.output)
    except Exception as e:
        print(f"\nERROR durante el texturizado: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
