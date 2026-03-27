"""
pipeline_runner.py — Pipeline completo: imagen → GPU remota (shape + paint) → Maya

Modos:
  full       imagen → shape (mesh.glb) → paint (mesh_uv.obj + texture) → Maya  [default]
  paint-only mesh.glb existente → paint → Maya  (cuando la geometría ya está OK)

Uso desde Maya Script Editor (Python tab):
  exec(open('/path/to/project/vision/pipeline_runner.py').read())

Uso desde terminal:
  python vision/pipeline_runner.py
  python vision/pipeline_runner.py --mode paint-only
  python vision/pipeline_runner.py --image /path/to/reference.jpg

Configuración via variables de entorno (ver .env.example):
  GPU_SSH_HOST     — usuario@host del servidor GPU remoto
  GPU_SSH_KEY      — ruta a la clave SSH privada
  GPU_REMOTE_BASE  — directorio base del proyecto en el servidor remoto
"""

import argparse
import os
import subprocess
import sys
import time

# ============================================================
# CONFIG — leer de variables de entorno con defaults seguros
# ============================================================

# Directorio raíz del proyecto (relativo a este script)
BASE_DIR = os.environ.get(
    'PROJECT_DIR',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

OUTPUT_SUBDIR = os.environ.get('OUTPUT_SUBDIR', '0')
LOCAL_DIR     = os.path.join(BASE_DIR, 'reference', '3d_output', OUTPUT_SUBDIR)

# Imagen de referencia por defecto (sobreescribir con --image o DEFAULT_IMAGE)
DEFAULT_IMAGE = os.environ.get(
    'REFERENCE_IMAGE',
    os.path.join(BASE_DIR, 'reference', 'reference.jpg')
)

# SSH — servidor GPU remoto
SSH_HOST = os.environ.get('GPU_SSH_HOST', 'user@gpu-host')
SSH_KEY  = os.path.expanduser(os.environ.get('GPU_SSH_KEY', '~/.ssh/id_rsa'))
SSH_OPTS = ['-i', SSH_KEY, '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']

# Rutas en el servidor remoto
REMOTE_BASE  = os.environ.get('GPU_REMOTE_BASE', '/opt/ai-studio')
REMOTE_VENV  = os.environ.get('GPU_VENV', f'{REMOTE_BASE}/vision/.venv')
REMOTE_DIR   = f'{REMOTE_BASE}/reference/3d_output/{OUTPUT_SUBDIR}'

# Scripts remotos (se suben siempre para que los cambios locales se apliquen)
REMOTE_SHAPE_SCRIPT   = f'{REMOTE_BASE}/vision/shape_remote.py'
REMOTE_TEXTURE_SCRIPT = f'{REMOTE_BASE}/vision/texture_remote.py'

# Timeouts
SHAPE_TIMEOUT   = int(os.environ.get('SHAPE_TIMEOUT',   '900'))   # 15 min
TEXTURE_TIMEOUT = int(os.environ.get('TEXTURE_TIMEOUT', '600'))   # 10 min


# ============================================================
# HELPERS
# ============================================================

def _run(cmd, timeout=60, label=''):
    t0 = time.time()
    if label:
        print(f'\n[Pipeline] {label}')
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.time() - t0
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        err = result.stderr.strip()
        if err:
            print(f'  STDERR: {err[-800:]}')
        raise RuntimeError(f"Comando falló (rc={result.returncode}): {' '.join(str(c) for c in cmd[:4])}")
    print(f'  OK ({elapsed:.1f}s)')
    return result


def ssh(remote_cmd, timeout=60, label=''):
    return _run(['ssh'] + SSH_OPTS + [SSH_HOST, remote_cmd], timeout=timeout, label=label)


def scp_up(local, remote, timeout=60, label=''):
    return _run(['scp'] + SSH_OPTS + [local, f'{SSH_HOST}:{remote}'], timeout=timeout, label=label)


def scp_down(remote, local, timeout=60, label=''):
    return _run(['scp'] + SSH_OPTS + [f'{SSH_HOST}:{remote}', local], timeout=timeout, label=label)


def file_info(path):
    if os.path.exists(path):
        return f'{os.path.getsize(path) // 1024} KB'
    return '(no existe)'


# ============================================================
# PIPELINE STEPS
# ============================================================

def step_check_connection():
    """Verifica que el servidor GPU es accesible."""
    print('\n[Pipeline] Verificando conexión SSH al servidor GPU...')
    result = subprocess.run(
        ['ssh'] + SSH_OPTS + [SSH_HOST,
            'echo OK && nvidia-smi --query-gpu=name,memory.free --format=csv,noheader'],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'No se puede conectar a {SSH_HOST}.\n'
            'Verifica GPU_SSH_HOST y GPU_SSH_KEY en tu .env'
        )
    print(f'  {result.stdout.strip()}')


def step_prepare_remote():
    """Crea directorio remoto y sube los scripts actualizados."""
    ssh(f'mkdir -p {REMOTE_DIR}', label='Creando directorio remoto')

    local_shape   = os.path.join(BASE_DIR, 'vision', 'shape_remote.py')
    local_texture = os.path.join(BASE_DIR, 'vision', 'texture_remote.py')
    scp_up(local_shape,   REMOTE_SHAPE_SCRIPT,   label='Subiendo shape_remote.py')
    scp_up(local_texture, REMOTE_TEXTURE_SCRIPT, label='Subiendo texture_remote.py')


def step_upload_image(image_local):
    """Sube la imagen de referencia al servidor remoto."""
    scp_up(image_local, f'{REMOTE_DIR}/input.png',
           timeout=30, label=f'Subiendo imagen ({file_info(image_local)})')


def step_shape_generation():
    """Genera mesh.glb en el servidor remoto con Hunyuan3D-2 Shape."""
    print('\n[Pipeline] Generando geometría 3D con Hunyuan3D-2 Shape...')
    print('[Pipeline] Puede tardar 1-15 min según GPU y si los pesos ya están cacheados.')
    print(f'[Pipeline] Timeout: {SHAPE_TIMEOUT}s')

    remote_cmd = (
        f'{REMOTE_VENV}/bin/python {REMOTE_SHAPE_SCRIPT} '
        f'--image {REMOTE_DIR}/input.png '
        f'--output {REMOTE_DIR}'
    )
    ssh(remote_cmd, timeout=SHAPE_TIMEOUT, label='Ejecutando shape_remote.py')


def step_download_mesh():
    """Descarga mesh.glb desde el servidor remoto."""
    os.makedirs(LOCAL_DIR, exist_ok=True)
    mesh_local = os.path.join(LOCAL_DIR, 'mesh.glb')
    scp_down(f'{REMOTE_DIR}/mesh.glb', mesh_local,
             timeout=60, label='Bajando mesh.glb')
    if os.path.exists(mesh_local):
        print(f'  mesh.glb: {file_info(mesh_local)}')
    return mesh_local


def step_upload_mesh(mesh_local):
    """Sube mesh.glb al servidor remoto (modo paint-only)."""
    scp_up(mesh_local, f'{REMOTE_DIR}/mesh.glb',
           timeout=60, label=f'Subiendo mesh.glb ({file_info(mesh_local)})')


def step_texture_generation():
    """Texturiza el mesh con Hunyuan3D-Paint en el servidor remoto."""
    print('\n[Pipeline] Texturizando con Hunyuan3D-Paint...')
    print('[Pipeline] Puede tardar 2-5 min.')
    print(f'[Pipeline] Timeout: {TEXTURE_TIMEOUT}s')

    remote_cmd = (
        f'{REMOTE_VENV}/bin/python {REMOTE_TEXTURE_SCRIPT} '
        f'--mesh {REMOTE_DIR}/mesh.glb '
        f'--image {REMOTE_DIR}/input.png '
        f'--output {REMOTE_DIR}'
    )
    ssh(remote_cmd, timeout=TEXTURE_TIMEOUT, label='Ejecutando texture_remote.py')


def step_verify_remote_outputs():
    """Verifica que los outputs del texturizado existen en el servidor remoto."""
    r = subprocess.run(
        ['ssh'] + SSH_OPTS + [SSH_HOST,
            f'ls -lh {REMOTE_DIR}/mesh_uv.obj {REMOTE_DIR}/texture_baked.png 2>&1'],
        capture_output=True, text=True, timeout=10
    )
    print(f'[Pipeline] Outputs remotos:\n{r.stdout.strip()}')
    if 'No such file' in r.stdout or r.returncode != 0:
        raise RuntimeError('mesh_uv.obj o texture_baked.png no encontrados en el servidor remoto.')


def step_download_outputs():
    """Descarga mesh_uv.obj, texture_baked.png y textured.glb."""
    os.makedirs(LOCAL_DIR, exist_ok=True)
    uv_obj  = os.path.join(LOCAL_DIR, 'mesh_uv.obj')
    tex_png = os.path.join(LOCAL_DIR, 'texture_baked.png')
    glb_out = os.path.join(LOCAL_DIR, 'textured.glb')

    scp_down(f'{REMOTE_DIR}/mesh_uv.obj',       uv_obj,  label='Bajando mesh_uv.obj')
    scp_down(f'{REMOTE_DIR}/texture_baked.png', tex_png, label='Bajando texture_baked.png')

    r = subprocess.run(
        ['scp'] + SSH_OPTS + [f'{SSH_HOST}:{REMOTE_DIR}/textured.glb', glb_out],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode == 0:
        print(f'  textured.glb: {file_info(glb_out)}')

    print('\n[Pipeline] Ficheros locales:')
    for fname in ['mesh.glb', 'input.png', 'mesh_uv.obj', 'texture_baked.png', 'textured.glb']:
        p = os.path.join(LOCAL_DIR, fname)
        marker = '✓' if os.path.exists(p) else '✗'
        print(f'  {marker} {fname:25s} {file_info(p)}')


def step_maya_import():
    """Importa en Maya: maya_import_hires.py + maya_fix_position_smooth.py"""
    import_script = os.path.join(BASE_DIR, 'vision', 'maya_import_hires.py')
    fix_script    = os.path.join(BASE_DIR, 'vision', 'maya_fix_position_smooth.py')

    if not os.path.exists(import_script):
        print(f'  ERROR: no encontrado {import_script}')
        return

    print('\n[Pipeline] Importando en Maya (maya_import_hires.py)...')
    exec(open(import_script).read(), globals())

    if os.path.exists(fix_script):
        print('\n[Pipeline] Aplicando correcciones (maya_fix_position_smooth.py)...')
        exec(open(fix_script).read(), globals())
    else:
        print(f'  ADVERTENCIA: no encontrado {fix_script}')


# ============================================================
# MAIN
# ============================================================

def run_pipeline(mode='full', image_path=None, skip_maya=False):
    """
    Ejecuta el pipeline completo o parcial.

    mode:
      'full'       — imagen → shape → paint → Maya
      'paint-only' — mesh.glb existente → paint → Maya

    image_path: ruta a la imagen de referencia (.jpg/.png). Si None, usa DEFAULT_IMAGE.
    skip_maya:  si True, omite la importación en Maya (útil desde terminal).
    """
    image_local = image_path or DEFAULT_IMAGE
    mesh_local  = os.path.join(LOCAL_DIR, 'mesh.glb')

    os.makedirs(LOCAL_DIR, exist_ok=True)

    print('=' * 60)
    print(f'[Pipeline] Iniciando  modo={mode}')
    print(f'[Pipeline] Imagen:    {image_local}  ({file_info(image_local)})')
    print(f'[Pipeline] Local:     {LOCAL_DIR}')
    print(f'[Pipeline] Remote:    {SSH_HOST}:{REMOTE_DIR}')
    print('=' * 60)

    if not os.path.exists(image_local):
        raise FileNotFoundError(
            f'Imagen de referencia no encontrada: {image_local}\n'
            'Configura REFERENCE_IMAGE en .env o pasa --image /ruta/a/imagen.jpg'
        )

    if mode == 'paint-only' and not os.path.exists(mesh_local):
        raise FileNotFoundError(
            f'Modo paint-only requiere mesh.glb en: {mesh_local}\n'
            "Usa mode='full' para generarlo desde la imagen."
        )

    t_start = time.time()

    step_check_connection()
    step_prepare_remote()
    step_upload_image(image_local)

    if mode == 'full':
        step_shape_generation()
        step_download_mesh()
    else:
        step_upload_mesh(mesh_local)

    step_texture_generation()
    step_verify_remote_outputs()
    step_download_outputs()

    if not skip_maya:
        step_maya_import()

    elapsed = time.time() - t_start
    print(f"\n{'=' * 60}")
    print(f'[Pipeline] ¡Completado en {elapsed / 60:.1f} min!')
    print(f"{'=' * 60}")


# ── Punto de entrada ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Pipeline imagen → GPU remota (shape + paint) → Maya',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python vision/pipeline_runner.py
  python vision/pipeline_runner.py --mode paint-only
  python vision/pipeline_runner.py --image /ruta/a/nueva_ref.jpg
  python vision/pipeline_runner.py --skip-maya
        """
    )
    parser.add_argument(
        '--mode', choices=['full', 'paint-only'], default='full',
        help="'full' = shape+paint desde imagen | 'paint-only' = solo texturizar mesh existente"
    )
    parser.add_argument('--image', type=str, default=None,
                        help='Imagen de referencia (.jpg/.png)')
    parser.add_argument('--skip-maya', action='store_true',
                        help='No importar en Maya al terminar')
    args = parser.parse_args()
    run_pipeline(mode=args.mode, image_path=args.image, skip_maya=args.skip_maya)

else:
    # Ejecutado con exec() desde Maya Script Editor
    run_pipeline(mode='paint-only')
