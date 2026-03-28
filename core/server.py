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
    GPU_SSH_HOST       — usuario@host del servidor GPU remoto
    GPU_REMOTE_BASE    — directorio base en el servidor remoto
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
from fastmcp import FastMCP

from maya_bridge import MayaBridge, MayaBridgeError

# ─────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────

MAYA_HOST = os.environ.get("MAYA_HOST", "localhost")
MAYA_PORT = int(os.environ.get("MAYA_PORT", "7001"))

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

def _handle_error(e: Exception) -> str:
    """Formateo consistente de errores."""
    if isinstance(e, MayaBridgeError):
        return f"Error Maya: {e}"
    return f"Error inesperado: {type(e).__name__}: {e}"


@mcp.tool(
    name="maya_ping",
    annotations={
        "title": "Verificar conexión con Maya",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def maya_ping() -> str:
    """Verifica la conexión con Maya y devuelve info del entorno (versión, escena actual, renderer)."""
    try:
        info = bridge.ping()
        return json.dumps(info, indent=2, ensure_ascii=False)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="maya_create_primitive",
    annotations={
        "title": "Crear primitiva 3D",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
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


@mcp.tool(
    name="maya_assign_material",
    annotations={
        "title": "Crear y asignar material",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
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


@mcp.tool(
    name="maya_transform",
    annotations={
        "title": "Transformar objeto",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
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


@mcp.tool(
    name="maya_list_scene",
    annotations={
        "title": "Consultar objetos de la escena",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
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


@mcp.tool(
    name="maya_delete",
    annotations={
        "title": "Eliminar objeto",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
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


@mcp.tool(
    name="maya_create_light",
    annotations={
        "title": "Crear luz",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
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


@mcp.tool(
    name="maya_create_camera",
    annotations={
        "title": "Crear cámara",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
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


@mcp.tool(
    name="maya_execute_python",
    annotations={
        "title": "Ejecutar Python en Maya",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
async def maya_execute_python(params: ExecutePythonInput) -> str:
    """Ejecuta código Python arbitrario en Maya. El código debe asignar su resultado a la variable 'result'. Útil para operaciones avanzadas no cubiertas por otros tools."""
    try:
        return bridge.execute(params.code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="maya_new_scene",
    annotations={
        "title": "Nueva escena",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
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


@mcp.tool(
    name="maya_save_scene",
    annotations={
        "title": "Guardar escena",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
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
# Texturizado remoto — glorfindel RTX 3090
# ─────────────────────────────────────────────

# Configuración via variables de entorno (o valores por defecto)
_LINUX_SSH_HOST   = os.environ.get("GPU_SSH_HOST",        "user@gpu-host")
_LINUX_REMOTE_DIR = os.environ.get("GPU_REMOTE_BASE",     "/opt/ai-studio")
_LINUX_VISION_DIR = os.environ.get("GPU_VISION_DIR",      "/opt/ai-studio/vision")
_LINUX_WORK_DIR   = os.environ.get("GPU_WORK_DIR",        "/opt/ai-studio/reference/3d_output")
_LINUX_VENV       = os.environ.get("GPU_VENV",            "/opt/ai-studio/vision/.venv")
_MAC_BASE_DIR     = os.environ.get("MAYA_BASE_DIR",
                                   str(Path(__file__).parent.parent))           # raíz del proyecto en Mac


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


async def _run_cmd(cmd: List[str], timeout: int = 600) -> tuple[int, str, str]:
    """Ejecuta un comando async y devuelve (returncode, stdout, stderr)."""
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


@mcp.tool(
    name="shape_generate_remote",
    description=(
        "Genera geometría 3D (mesh.glb) a partir de una imagen de referencia usando "
        "Hunyuan3D-2 DiT en el servidor GPU remoto (Linux RTX 3090). "
        "Sube la imagen al servidor, ejecuta shape_remote.py (~3-8 min), "
        "y descarga mesh.glb al directorio local reference/3d_output/{output_subdir}/. "
        "El mesh resultante no tiene UVs ni textura — usa texture_mesh_remote después "
        "para pintar la textura. "
        "Requiere SSH configurado sin contraseña al servidor GPU."
    ),
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def shape_generate_remote(params: ShapeGenerateInput) -> str:
    """Genera geometría 3D desde una imagen en el servidor GPU remoto."""
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

        remote_work = f"{_LINUX_WORK_DIR}/{params.output_subdir}"
        ssh_host = _LINUX_SSH_HOST

        log_lines = []
        log = lambda msg: log_lines.append(msg)

        # ── Paso 1: Crear directorio remoto ───────────────────────────────
        log(f"[1/4] Creando directorio remoto en {ssh_host}...")
        rc, _, err = await _run_cmd(["ssh", ssh_host, f"mkdir -p {remote_work}"])
        if rc != 0:
            return json.dumps({"error": f"SSH mkdir falló: {err.strip()}",
                               "hint": "Verifica SSH sin contraseña al servidor GPU."})

        # ── Paso 2: Subir imagen ──────────────────────────────────────────
        log(f"[2/4] Subiendo imagen {image_local.name}...")
        rc, _, err = await _run_cmd([
            "scp",
            str(image_local),
            f"{ssh_host}:{remote_work}/input.png"
        ])
        if rc != 0:
            return json.dumps({"error": f"SCP upload falló: {err.strip()}"})

        # ── Paso 3: Ejecutar shape generation ─────────────────────────────
        log(f"[3/4] Ejecutando Hunyuan3D-2 Shape en {ssh_host} (~3-8 min)...")
        remote_script = f"{_LINUX_VISION_DIR}/shape_remote.py"
        remote_cmd = (
            f"source {_LINUX_VENV}/bin/activate && "
            f"python {remote_script} "
            f"--image {remote_work}/input.png "
            f"--output {remote_work}"
        )
        rc, stdout, stderr = await _run_cmd(
            ["ssh", ssh_host, remote_cmd],
            timeout=900  # 15 min máximo
        )

        output_log = (stdout + stderr).strip()
        if rc != 0:
            return json.dumps({
                "error": "Shape generation falló en el servidor GPU",
                "log": output_log[-2000:],
                "hint": "Verifica con: ssh <gpu-host> 'source .venv/bin/activate && python shape_remote.py --test'"
            })

        log(f"[3/4] Shape generation completado.")

        # ── Paso 4: Descargar mesh.glb ────────────────────────────────────
        log(f"[4/4] Descargando mesh.glb...")
        mesh_local = out_dir / "mesh.glb"
        rc, _, err = await _run_cmd([
            "scp",
            f"{ssh_host}:{remote_work}/mesh.glb",
            str(mesh_local)
        ])

        if rc != 0:
            return json.dumps({
                "error": f"No se pudo descargar mesh.glb: {err.strip()}",
                "log": "\n".join(log_lines)
            })

        mesh_size_kb = mesh_local.stat().st_size // 1024 if mesh_local.exists() else 0
        log(f"[4/4] mesh.glb descargado ({mesh_size_kb} KB)")

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


@mcp.tool(
    name="texture_mesh_remote",
    description=(
        "Texturiza un mesh 3D usando Hunyuan3D-Paint en glorfindel (Linux RTX 3090). "
        "Envía mesh.glb + imagen de referencia al Linux, ejecuta el texturizado (~3-5 min), "
        "y recupera mesh_uv.obj + texture_baked.png al directorio local. "
        "Maya detecta automáticamente los archivos resultantes (USE_BAKED_TEXTURE). "
        "Requiere SSH configurado sin contraseña a 'maya-linux' (~/.ssh/config)."
    ),
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
async def texture_mesh_remote(params: TextureRemoteInput) -> str:
    """Texturiza el mesh en glorfindel (RTX 3090) y recupera los resultados."""
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

        remote_work = f"{_LINUX_WORK_DIR}/{params.output_subdir}"
        ssh_host    = _LINUX_SSH_HOST

        log_lines = []
        log = lambda msg: log_lines.append(msg)

        # ── Paso 1: Crear directorio remoto ────────────────────────────────
        log(f"[1/5] Creando directorio remoto en {ssh_host}...")
        rc, _, err = await _run_cmd(["ssh", ssh_host, f"mkdir -p {remote_work}"])
        if rc != 0:
            return json.dumps({"error": f"SSH mkdir falló: {err.strip()}",
                               "hint": "Verifica SSH sin contraseña: ssh maya-linux echo OK"})

        # ── Paso 2: Subir archivos ─────────────────────────────────────────
        log(f"[2/5] Subiendo {params.mesh_filename} y {params.image_filename}...")
        rc, _, err = await _run_cmd([
            "scp",
            str(mesh_local),
            str(image_local),
            f"{ssh_host}:{remote_work}/"
        ])

        if rc != 0:
            return json.dumps({"error": f"SCP upload falló: {err.strip()}"})

        # ── Paso 3: Ejecutar texturizado remoto ────────────────────────────
        log(f"[3/5] Ejecutando Hunyuan3D-Paint en {ssh_host} (~3-5 min)...")
        remote_script = f"{_LINUX_VISION_DIR}/texture_remote.py"
        remote_cmd = (
            f"source {_LINUX_VENV}/bin/activate && "
            f"python {remote_script} "
            f"--mesh {remote_work}/{params.mesh_filename} "
            f"--image {remote_work}/{params.image_filename} "
            f"--output {remote_work}"
        )
        rc, stdout, stderr = await _run_cmd(
            ["ssh", ssh_host, remote_cmd],
            timeout=900  # 15 min máximo
        )

        output_log = (stdout + stderr).strip()
        if rc != 0:
            return json.dumps({
                "error":  "Texturizado remoto falló",
                "log":    output_log[-2000:],  # últimas 2000 chars del log
                "hint":   "Ejecuta el test: ssh maya-linux 'conda run -n hy3d-tex python /home/flame/ai-studio/vision/texture_remote.py --test'"
            })

        log(f"[4/5] Texturizado completado en {ssh_host}.")

        # ── Paso 4: Descargar resultados ───────────────────────────────────
        log(f"[4/5] Descargando resultados...")
        results_to_download = ["textured.glb", "mesh_uv.obj", "texture_baked.png"]
        downloaded = []
        failed     = []

        for fname in results_to_download:
            rc, _, err = await _run_cmd([
                "scp",
                f"{ssh_host}:{remote_work}/{fname}",
                str(out_dir / fname)
            ])
            if rc == 0:
                downloaded.append(fname)
            else:
                failed.append(fname)

        log(f"[5/5] Descargados: {downloaded}")

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
