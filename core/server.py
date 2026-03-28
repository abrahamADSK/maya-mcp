#!/usr/bin/env python3
"""
Maya MCP Core Server — Servidor MCP para controlar Autodesk Maya.

Módulo Core: operaciones básicas de escena, objetos, transformaciones y materiales.
Se comunica con Maya via Command Port (TCP) usando maya_bridge.

Uso:
    python server.py                    # stdio transport (MCP estándar)
    python server.py --transport http   # HTTP transport (desarrollo/debug)

Variables de entorno (ver .env.example):
    MAYA_HOST          — host donde corre Maya (default: localhost)
    MAYA_PORT          — puerto Command Port de Maya (default: 7001)
    GPU_API_URL        — URL del servidor GPU API (ej: https://glorfindel:9443)
    GPU_API_KEY        — API key para autenticación con el servidor GPU
"""

import asyncio
import json
import os
import sys
from typing import Optional, List
from enum import Enum
from pathlib import Path

# Asegurar que el directorio de este archivo está en el path
# para que maya_bridge se encuentre sin importar desde dónde se lance
sys.path.insert(0, str(Path(__file__).parent))

from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

from maya_bridge import MayaBridge, MayaBridgeError

# ─────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────

MAYA_HOST = os.environ.get("MAYA_HOST", "localhost")
MAYA_PORT = int(os.environ.get("MAYA_PORT", "7001"))
MAYA_APP  = os.environ.get("MAYA_APP", "Maya")  # macOS app name for `open -a`

mcp = FastMCP("maya_mcp")
bridge = MayaBridge(host=MAYA_HOST, port=MAYA_PORT)


# ─────────────────────────────────────────────
# Modelos de entrada (Pydantic)
# ─────────────────────────────────────────────

class PrimitiveType(str, Enum):
    CUBE = "cube"
    SPHERE = "sphere"
    CYLINDER = "cylinder"
    CONE = "cone"
    PLANE = "plane"
    TORUS = "torus"


class CreatePrimitiveInput(BaseModel):
    """Parámetros para crear una primitiva 3D."""
    model_config = ConfigDict(str_strip_whitespace=True)

    primitive_type: PrimitiveType = Field(..., description="Tipo de primitiva: cube, sphere, cylinder, cone, plane, torus")
    name: Optional[str] = Field(default=None, description="Nombre del objeto (Maya genera uno si se omite)")
    position: Optional[List[float]] = Field(default=None, description="Posición [x, y, z] en world space", min_length=3, max_length=3)
    scale: Optional[List[float]] = Field(default=None, description="Escala [x, y, z]", min_length=3, max_length=3)
    rotation: Optional[List[float]] = Field(default=None, description="Rotación [x, y, z] en grados", min_length=3, max_length=3)


class MaterialInput(BaseModel):
    """Parámetros para crear y asignar un material."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Nombre del objeto al que asignar el material")
    material_name: Optional[str] = Field(default=None, description="Nombre del material (se genera si se omite)")
    color: List[float] = Field(..., description="Color RGB normalizado [r, g, b] (0.0-1.0)", min_length=3, max_length=3)
    material_type: str = Field(default="lambert", description="Tipo de shader: lambert, blinn, phong, aiStandardSurface")


class TransformInput(BaseModel):
    """Parámetros para transformar un objeto."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Nombre del objeto a transformar")
    position: Optional[List[float]] = Field(default=None, description="Nueva posición [x, y, z]", min_length=3, max_length=3)
    rotation: Optional[List[float]] = Field(default=None, description="Nueva rotación [x, y, z] en grados", min_length=3, max_length=3)
    scale: Optional[List[float]] = Field(default=None, description="Nueva escala [x, y, z]", min_length=3, max_length=3)
    relative: bool = Field(default=False, description="Si True, transforma relativo a la posición actual")


class SceneQueryInput(BaseModel):
    """Parámetros para consultar la escena."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_type: Optional[str] = Field(default=None, description="Filtrar por tipo: mesh, light, camera, transform, etc.")
    name_filter: Optional[str] = Field(default=None, description="Filtrar por nombre (soporta wildcards: *sphere*)")


class ExecutePythonInput(BaseModel):
    """Ejecutar código Python arbitrario en Maya."""
    model_config = ConfigDict(str_strip_whitespace=True)

    code: str = Field(..., description="Código Python a ejecutar en Maya. Asignar resultado a variable 'result'.")


class DeleteObjectInput(BaseModel):
    """Parámetros para eliminar objetos."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Nombre del objeto a eliminar (soporta wildcards)")


class LightInput(BaseModel):
    """Parámetros para crear una luz."""
    model_config = ConfigDict(str_strip_whitespace=True)

    light_type: str = Field(default="directional", description="Tipo: directional, point, spot, area, ambient")
    name: Optional[str] = Field(default=None, description="Nombre de la luz")
    intensity: float = Field(default=1.0, description="Intensidad de la luz", ge=0.0)
    color: Optional[List[float]] = Field(default=None, description="Color RGB [r, g, b] (0.0-1.0)", min_length=3, max_length=3)
    position: Optional[List[float]] = Field(default=None, description="Posición [x, y, z]", min_length=3, max_length=3)


class CameraInput(BaseModel):
    """Parámetros para crear una cámara."""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, description="Nombre de la cámara")
    position: Optional[List[float]] = Field(default=None, description="Posición [x, y, z]", min_length=3, max_length=3)
    look_at: Optional[List[float]] = Field(default=None, description="Punto al que mira [x, y, z]", min_length=3, max_length=3)
    focal_length: float = Field(default=35.0, description="Distancia focal en mm", ge=1.0, le=500.0)


# ─────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────

async def _run_cmd(cmd: List[str], timeout: int = 60) -> tuple:
    """Ejecuta un comando local async y devuelve (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"Timeout después de {timeout}s"
    return proc.returncode, stdout.decode(), stderr.decode()


def _handle_error(e: Exception) -> str:
    """Formateo consistente de errores."""
    if isinstance(e, MayaBridgeError):
        return f"Error Maya: {e}"
    return f"Error inesperado: {type(e).__name__}: {e}"


@mcp.tool(name="maya_ping")
async def maya_ping() -> str:
    """Verifica la conexión con Maya y devuelve info del entorno (versión, escena actual, renderer)."""
    try:
        info = bridge.ping()
        return json.dumps(info, indent=2, ensure_ascii=False)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_launch")
async def maya_launch() -> str:
    """Abre Maya y espera a que el Command Port responda."""
    import socket
    import time

    # 1. Comprobar si ya está conectado
    try:
        info = bridge.ping()
        return json.dumps({
            "status": "already_running",
            "version": info.get("version", "unknown"),
            "message": "Maya ya está abierto y el Command Port responde."
        }, ensure_ascii=False)
    except Exception:
        pass  # No está corriendo o no responde — lo abrimos

    # 2. Lanzar Maya
    rc, _, err = await _run_cmd(["open", "-a", MAYA_APP], timeout=10)
    if rc != 0:
        return json.dumps({
            "error": f"No se pudo abrir Maya ({MAYA_APP}): {err.strip()}",
            "hint": "Verifica que Maya está instalado. Configura MAYA_APP en .env si el nombre es distinto."
        }, ensure_ascii=False)

    # 3. Esperar a que el Command Port esté listo (max 90s)
    max_wait = 90
    poll_interval = 3
    waited = 0

    while waited < max_wait:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((MAYA_HOST, MAYA_PORT))
            sock.close()
            # Puerto abierto — intentar ping real
            try:
                info = bridge.ping()
                return json.dumps({
                    "status": "launched",
                    "waited_seconds": waited,
                    "version": info.get("version", "unknown"),
                    "message": f"Maya abierto y Command Port listo ({waited}s)."
                }, ensure_ascii=False)
            except Exception:
                continue  # Puerto abierto pero Maya aún cargando
        except (ConnectionRefusedError, socket.timeout, OSError):
            continue  # Puerto aún no disponible

    return json.dumps({
        "error": f"Maya se abrió pero el Command Port no respondió en {max_wait}s.",
        "hint": "Verifica que tienes el Command Port en userSetup.py: cmds.commandPort(name=':7001', sourceType='mel')"
    }, ensure_ascii=False)


@mcp.tool(name="maya_create_primitive")
async def maya_create_primitive(params: CreatePrimitiveInput) -> str:
    """Crea una primitiva 3D en Maya (cubo, esfera, cilindro, cono, plano, torus) con posición, escala y rotación opcionales."""
    try:
        create_funcs = {
            "cube": "cmds.polyCube",
            "sphere": "cmds.polySphere",
            "cylinder": "cmds.polyCylinder",
            "cone": "cmds.polyCone",
            "plane": "cmds.polyPlane",
            "torus": "cmds.polyTorus",
        }
        func = create_funcs[params.primitive_type.value]
        name_arg = f", name='{params.name}'" if params.name else ""

        code = f"""
import maya.cmds as cmds
obj = {func}({name_arg})[0]
"""
        if params.position:
            code += f"cmds.xform(obj, translation={params.position}, worldSpace=True)\n"
        if params.scale:
            code += f"cmds.xform(obj, scale={params.scale})\n"
        if params.rotation:
            code += f"cmds.xform(obj, rotation={params.rotation})\n"

        code += "result = {'name': obj, 'type': '" + params.primitive_type.value + "'}"

        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_assign_material")
async def maya_assign_material(params: MaterialInput) -> str:
    """Crea un material (lambert, blinn, phong, aiStandardSurface) con color RGB y lo asigna a un objeto."""
    try:
        mat_name = params.material_name or f"{params.object_name}_mat"
        r, g, b = params.color

        code = f"""
import maya.cmds as cmds
mat = cmds.shadingNode('{params.material_type}', asShader=True, name='{mat_name}')
sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='{mat_name}_SG')
cmds.connectAttr(mat + '.outColor', sg + '.surfaceShader')
cmds.setAttr(mat + '.color', {r}, {g}, {b}, type='double3')
cmds.select('{params.object_name}')
cmds.sets(forceElement=sg)
result = {{'material': mat, 'shading_group': sg, 'assigned_to': '{params.object_name}'}}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_transform")
async def maya_transform(params: TransformInput) -> str:
    """Mueve, rota o escala un objeto en la escena de Maya."""
    try:
        ws = "False" if params.relative else "True"
        rel = "True" if params.relative else "False"

        code = f"import maya.cmds as cmds\n"
        if params.position:
            code += f"cmds.xform('{params.object_name}', translation={params.position}, worldSpace={ws}, relative={rel})\n"
        if params.rotation:
            code += f"cmds.xform('{params.object_name}', rotation={params.rotation}, worldSpace={ws}, relative={rel})\n"
        if params.scale:
            code += f"cmds.xform('{params.object_name}', scale={params.scale}, relative={rel})\n"

        code += f"""
pos = cmds.xform('{params.object_name}', q=True, translation=True, worldSpace=True)
rot = cmds.xform('{params.object_name}', q=True, rotation=True, worldSpace=True)
scl = cmds.xform('{params.object_name}', q=True, scale=True)
result = {{'object': '{params.object_name}', 'position': pos, 'rotation': rot, 'scale': scl}}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_list_scene")
async def maya_list_scene(params: SceneQueryInput) -> str:
    """Lista objetos de la escena de Maya, con filtros opcionales por tipo o nombre."""
    try:
        filters = []
        if params.object_type:
            filters.append(f"type='{params.object_type}'")
        if params.name_filter:
            filters.append(f"'{params.name_filter}'")

        filter_str = ", ".join(filters)

        code = f"""
import maya.cmds as cmds
import json
objects = cmds.ls({filter_str}) or []
result = {{'count': len(objects), 'objects': objects}}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_delete")
async def maya_delete(params: DeleteObjectInput) -> str:
    """Elimina un objeto de la escena de Maya por nombre (soporta wildcards como *sphere*)."""
    try:
        code = f"""
import maya.cmds as cmds
targets = cmds.ls('{params.object_name}')
if targets:
    cmds.delete(targets)
    result = {{'deleted': targets}}
else:
    result = {{'error': 'No se encontró: {params.object_name}'}}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_create_light")
async def maya_create_light(params: LightInput) -> str:
    """Crea una luz en Maya (directional, point, spot, area, ambient) con intensidad y color configurables."""
    try:
        light_funcs = {
            "directional": "cmds.directionalLight",
            "point": "cmds.pointLight",
            "spot": "cmds.spotLight",
            "area": "cmds.shadingNode('areaLight', asLight=True",
            "ambient": "cmds.ambientLight",
        }

        name_arg = f", name='{params.name}'" if params.name else ""

        if params.light_type == "area":
            code = f"""
import maya.cmds as cmds
light = cmds.shadingNode('areaLight', asLight=True{name_arg})
"""
        else:
            func = light_funcs.get(params.light_type, "cmds.directionalLight")
            code = f"""
import maya.cmds as cmds
light = {func}({name_arg})
"""

        code += f"cmds.setAttr(light + '.intensity', {params.intensity})\n"

        if params.color:
            r, g, b = params.color
            code += f"cmds.setAttr(light + '.color', {r}, {g}, {b}, type='double3')\n"

        if params.position:
            code += f"""
parent = cmds.listRelatives(light, parent=True)[0]
cmds.xform(parent, translation={params.position}, worldSpace=True)
"""

        code += "result = {'light': light, 'type': '" + params.light_type + "'}\n"

        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_create_camera")
async def maya_create_camera(params: CameraInput) -> str:
    """Crea una cámara en Maya con posición, punto de mira y focal length configurables."""
    try:
        name_arg = f", name='{params.name}'" if params.name else ""
        code = f"""
import maya.cmds as cmds
cam = cmds.camera({name_arg})[0]
cmds.setAttr(cam + '.focalLength', {params.focal_length})
"""
        if params.position:
            code += f"cmds.xform(cam, translation={params.position}, worldSpace=True)\n"

        if params.look_at:
            code += f"""
aim = cmds.spaceLocator(name=cam + '_aim')[0]
cmds.xform(aim, translation={params.look_at}, worldSpace=True)
cmds.aimConstraint(aim, cam, aimVector=[0, 0, -1], upVector=[0, 1, 0])
cmds.delete(aim)
"""

        code += "result = {'camera': cam}\n"
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_execute_python")
async def maya_execute_python(params: ExecutePythonInput) -> str:
    """Ejecuta código Python arbitrario en Maya. El código debe asignar su resultado a la variable 'result'. Útil para operaciones avanzadas no cubiertas por otros tools."""
    try:
        return bridge.execute(params.code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_new_scene")
async def maya_new_scene() -> str:
    """Crea una nueva escena vacía en Maya (descarta la escena actual sin guardar)."""
    try:
        code = """
import maya.cmds as cmds
cmds.file(new=True, force=True)
result = {'status': 'new_scene_created'}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_save_scene")
async def maya_save_scene() -> str:
    """Guarda la escena actual de Maya."""
    try:
        code = """
import maya.cmds as cmds
scene = cmds.file(q=True, sceneName=True)
if scene:
    cmds.file(save=True)
    result = {'saved': scene}
else:
    result = {'error': 'Escena sin nombre. Usa maya_execute_python para hacer file(rename=...)'}
"""
        return bridge.execute(code)
    except Exception as e:
        return _handle_error(e)


# ─────────────────────────────────────────────
# GPU remoto — API REST (Hunyuan3D-2)
# ─────────────────────────────────────────────

# Configuración via variables de entorno
_GPU_API_URL  = os.environ.get("GPU_API_URL",  "https://glorfindel:9443").rstrip("/")
_GPU_API_KEY  = os.environ.get("GPU_API_KEY",  "")
_GPU_VERIFY   = os.environ.get("GPU_VERIFY_TLS", "false").lower() in ("true", "1", "yes")
_MAC_BASE_DIR = os.environ.get("MAYA_BASE_DIR",
                                str(Path(__file__).parent.parent))           # raíz del proyecto en Mac

# Lazy httpx client
_http_client = None

def _get_http_client():
    """Return a reusable httpx client for GPU API calls."""
    global _http_client
    if _http_client is None:
        import httpx
        headers = {}
        if _GPU_API_KEY:
            headers["x-api-key"] = _GPU_API_KEY
        _http_client = httpx.AsyncClient(
            base_url=_GPU_API_URL,
            headers=headers,
            verify=_GPU_VERIFY,
            timeout=httpx.Timeout(connect=10, read=900, write=60, pool=10),
        )
    return _http_client


async def _poll_job(job_id: str, log_fn=None, poll_interval: float = 5.0) -> dict:
    """Poll a GPU server job until it completes or fails."""
    client = _get_http_client()
    last_log_len = 0

    while True:
        resp = await client.get(f"/api/jobs/{job_id}")
        resp.raise_for_status()
        job = resp.json()

        # Stream new log lines
        if log_fn and "log" in job:
            for line in job["log"][last_log_len:]:
                log_fn(line)
            last_log_len = len(job["log"])

        if job["status"] == "completed":
            return job
        elif job["status"] == "failed":
            raise RuntimeError(job.get("error", "Job failed without details"))

        await asyncio.sleep(poll_interval)


async def _download_file(job_id: str, filename: str, dest: Path) -> bool:
    """Download a single file from a completed job."""
    client = _get_http_client()
    resp = await client.get(f"/api/jobs/{job_id}/files/{filename}")
    if resp.status_code == 200:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    return False


class ShapeGenerateInput(BaseModel):
    """Parámetros para la generación de geometría 3D en el servidor GPU remoto."""
    model_config = ConfigDict(str_strip_whitespace=True)

    image_path: str = Field(
        ...,
        description="Ruta local absoluta a la imagen de referencia (.jpg/.png). "
                    "Puede ser una imagen descargada de ShotGrid u otra fuente."
    )
    output_subdir: str = Field(
        default="0",
        description="Subdirectorio de salida dentro de reference/3d_output/ (ej: '0', 'asset_1478')"
    )


class ShapeTextInput(BaseModel):
    """Parámetros para la generación de geometría 3D desde texto (text-to-3D)."""
    model_config = ConfigDict(str_strip_whitespace=True)

    text_prompt: str = Field(
        ...,
        description="Descripción en inglés del objeto 3D a generar. "
                    "Ejemplos: 'american mailbox', 'wooden chair', 'medieval sword'."
    )
    output_subdir: str = Field(
        default="0",
        description="Subdirectorio de salida dentro de reference/3d_output/ (ej: '0', 'mailbox_0')"
    )


class TextureRemoteInput(BaseModel):
    """Parámetros para el texturizado remoto en glorfindel (RTX 3090)."""
    model_config = ConfigDict(str_strip_whitespace=True)

    output_subdir: str = Field(
        ...,
        description="Subdirectorio de salida dentro de reference/3d_output/ (ej: '0', 'frog_0')"
    )
    mesh_filename: str = Field(
        default="mesh.glb",
        description="Nombre del archivo mesh dentro de output_subdir (default: mesh.glb)"
    )
    image_filename: str = Field(
        default="input.png",
        description="Nombre de la imagen de referencia dentro de output_subdir (default: input.png)"
    )


@mcp.tool(name="shape_generate_remote")
async def shape_generate_remote(params: ShapeGenerateInput) -> str:
    """Genera geometría 3D desde una imagen en el servidor GPU remoto (via HTTPS API)."""
    try:
        image_local = Path(params.image_path)
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir

        # ── Verificar imagen local ────────────────────────────────────────
        if not image_local.exists():
            return json.dumps({
                "error": f"Imagen no encontrada: {image_local}",
                "hint": "Descarga primero la imagen con sg_download de fpt-mcp."
            })

        # Crear directorio de salida local
        out_dir.mkdir(parents=True, exist_ok=True)

        # Copiar imagen al directorio de salida como input.png (para texture_mesh_remote)
        import shutil
        input_copy = out_dir / "input.png"
        shutil.copy2(str(image_local), str(input_copy))

        log_lines = []
        log = lambda msg: log_lines.append(msg)
        client = _get_http_client()

        # ── Paso 1: Subir imagen al GPU server ───────────────────────────
        log(f"[1/3] Subiendo imagen a GPU server ({_GPU_API_URL})...")
        with open(str(image_local), "rb") as f:
            resp = await client.post(
                "/api/generate-shape",
                files={"image": (image_local.name, f, "image/png")},
                data={"output_subdir": params.output_subdir},
            )

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verifica que el servidor GPU está corriendo: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        log(f"[1/3] Job creado: {job_id}")

        # ── Paso 2: Esperar resultado ─────────────────────────────────────
        log(f"[2/3] Generando geometría 3D (~3-8 min)...")
        result = await _poll_job(job_id, log_fn=log)
        log(f"[2/3] Shape generation completado.")

        # ── Paso 3: Descargar mesh.glb ────────────────────────────────────
        log(f"[3/3] Descargando mesh.glb...")
        mesh_local = out_dir / "mesh.glb"
        ok = await _download_file(job_id, "mesh.glb", mesh_local)

        if not ok:
            return json.dumps({
                "error": "No se pudo descargar mesh.glb del servidor GPU",
                "log": "\n".join(log_lines)
            })

        mesh_size_kb = mesh_local.stat().st_size // 1024 if mesh_local.exists() else 0
        log(f"[3/3] mesh.glb descargado ({mesh_size_kb} KB)")

        return json.dumps({
            "status": "ok",
            "mesh_path": str(mesh_local),
            "mesh_size_kb": mesh_size_kb,
            "output_dir": str(out_dir),
            "image_copy": str(input_copy),
            "next_step": (
                f"Mesh generado. Para texturizar, llama a texture_mesh_remote con "
                f"output_subdir='{params.output_subdir}'. La imagen ya está copiada "
                f"como input.png en el directorio de salida."
            ),
            "log_summary": "\n".join(log_lines)
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(name="shape_generate_text")
async def shape_generate_text(params: ShapeTextInput) -> str:
    """Genera geometría 3D desde una descripción de texto (text-to-3D) en el servidor GPU remoto (via HTTPS API)."""
    try:
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)

        log_lines = []
        log = lambda msg: log_lines.append(msg)
        client = _get_http_client()

        # ── Paso 1: Enviar prompt al GPU server ──────────────────────────
        log(f"[1/3] Enviando prompt a GPU server: '{params.text_prompt}'...")
        resp = await client.post(
            "/api/generate-text",
            data={
                "text_prompt": params.text_prompt,
                "output_subdir": params.output_subdir,
            },
        )

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verifica que el servidor GPU está corriendo: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        log(f"[1/3] Job creado: {job_id}")

        # ── Paso 2: Esperar resultado ─────────────────────────────────────
        log(f"[2/3] Generando geometría 3D desde texto (~3-8 min)...")
        result = await _poll_job(job_id, log_fn=log)
        log("[2/3] Text-to-3D completado.")

        # ── Paso 3: Descargar mesh.glb ────────────────────────────────────
        log("[3/3] Descargando mesh.glb...")
        mesh_local = out_dir / "mesh.glb"
        ok = await _download_file(job_id, "mesh.glb", mesh_local)

        if not ok:
            return json.dumps({
                "error": "No se pudo descargar mesh.glb del servidor GPU",
                "log": "\n".join(log_lines)
            })

        mesh_size_kb = mesh_local.stat().st_size // 1024 if mesh_local.exists() else 0
        log(f"[3/3] mesh.glb descargado ({mesh_size_kb} KB)")

        return json.dumps({
            "status": "ok",
            "mesh_path": str(mesh_local),
            "mesh_size_kb": mesh_size_kb,
            "output_dir": str(out_dir),
            "text_prompt": params.text_prompt,
            "next_step": (
                f"Mesh generado desde texto. Para texturizar, llama a texture_mesh_remote con "
                f"output_subdir='{params.output_subdir}'. Nota: text-to-3D no genera imagen de "
                f"referencia, por lo que el texturizado usará solo la geometría."
            ),
            "log_summary": "\n".join(log_lines)
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(name="texture_mesh_remote")
async def texture_mesh_remote(params: TextureRemoteInput) -> str:
    """Texturiza el mesh en el servidor GPU remoto (via HTTPS API) y recupera los resultados."""
    try:
        out_dir     = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir
        mesh_local  = out_dir / params.mesh_filename
        image_local = out_dir / params.image_filename

        # ── Verificar archivos locales ─────────────────────────────────────
        if not mesh_local.exists():
            return json.dumps({
                "error": f"Mesh no encontrado: {mesh_local}",
                "hint":  "Genera primero el mesh con la herramienta de imagen-a-3D."
            })
        if not image_local.exists():
            return json.dumps({
                "error":  f"Imagen de referencia no encontrada: {image_local}",
                "hint":   f"Copia la imagen de referencia como '{params.image_filename}' en {out_dir}"
            })

        log_lines = []
        log = lambda msg: log_lines.append(msg)
        client = _get_http_client()

        # ── Paso 1: Subir mesh + imagen al GPU server ─────────────────────
        log(f"[1/3] Subiendo {params.mesh_filename} y {params.image_filename} a GPU server...")
        with open(str(mesh_local), "rb") as mf, open(str(image_local), "rb") as imf:
            resp = await client.post(
                "/api/texture-mesh",
                files={
                    "mesh": (params.mesh_filename, mf, "application/octet-stream"),
                    "image": (params.image_filename, imf, "image/png"),
                },
                data={"output_subdir": params.output_subdir},
            )

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verifica que el servidor GPU está corriendo: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        log(f"[1/3] Job creado: {job_id}")

        # ── Paso 2: Esperar resultado ─────────────────────────────────────
        log(f"[2/3] Texturizando mesh (~3-5 min)...")
        result = await _poll_job(job_id, log_fn=log)
        log("[2/3] Texturizado completado.")

        # ── Paso 3: Descargar resultados ──────────────────────────────────
        log("[3/3] Descargando resultados...")
        results_to_download = ["textured.glb", "mesh_uv.obj", "texture_baked.png"]
        downloaded = []
        failed     = []

        for fname in results_to_download:
            ok = await _download_file(job_id, fname, out_dir / fname)
            if ok:
                downloaded.append(fname)
            else:
                failed.append(fname)

        log(f"[3/3] Descargados: {downloaded}")

        # ── Resultado ──────────────────────────────────────────────────────
        baked_ready = (out_dir / "mesh_uv.obj").exists() and \
                      (out_dir / "texture_baked.png").exists()

        return json.dumps({
            "status":         "ok",
            "output_dir":     str(out_dir),
            "downloaded":     downloaded,
            "failed":         failed,
            "USE_BAKED_TEXTURE": baked_ready,
            "next_step":      (
                "Ejecuta import_and_setup.py en Maya — detectará "
                "mesh_uv.obj + texture_baked.png automáticamente (USE_BAKED_TEXTURE=True)."
                if baked_ready else
                "Algunos archivos no se descargaron. Verifica el log."
            ),
            "log_summary":    "\n".join(log_lines)
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
