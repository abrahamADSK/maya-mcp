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
    GPU_API_URL        — URL del servidor GPU API (ej: http://your-gpu-host:8000)
    GPU_API_KEY        — API key para autenticación (vacío si acceso abierto en LAN)
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
# Nuevos modelos de entrada (P2-P5, A-E)
# ─────────────────────────────────────────────

class MeshOperationType(str, Enum):
    EXTRUDE = "extrude"
    BEVEL = "bevel"
    BOOLEAN_UNION = "boolean_union"
    BOOLEAN_DIFFERENCE = "boolean_difference"
    BOOLEAN_INTERSECTION = "boolean_intersection"
    COMBINE = "combine"
    SEPARATE = "separate"
    SMOOTH = "smooth"


class MeshOperationInput(BaseModel):
    """Parámetros para operaciones de mesh."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Nombre del objeto mesh")
    operation: MeshOperationType = Field(..., description="Tipo de operación")
    second_object: Optional[str] = Field(default=None, description="Segundo objeto (requerido para boolean y combine)")
    faces: Optional[str] = Field(default=None, description="Componentes de cara (ej: 'pCube1.f[0:3]') para extrude/bevel")
    offset: float = Field(default=0.2, description="Offset/distancia para extrude o bevel", ge=0.0)
    divisions: int = Field(default=1, description="Divisiones para smooth o segments para bevel", ge=1, le=10)


class KeyframeInput(BaseModel):
    """Parámetros para crear keyframes de animación."""
    model_config = ConfigDict(str_strip_whitespace=True)

    object_name: str = Field(..., description="Nombre del objeto a animar")
    attribute: str = Field(default="translateX", description="Atributo a animar (translateX/Y/Z, rotateX/Y/Z, scaleX/Y/Z, visibility)")
    value: float = Field(..., description="Valor del keyframe")
    frame: float = Field(..., description="Frame en el que insertar el keyframe")
    in_tangent: str = Field(default="auto", description="Tangente de entrada: auto, linear, flat, spline, step")
    out_tangent: str = Field(default="auto", description="Tangente de salida: auto, linear, flat, spline, step")


class ImportFileInput(BaseModel):
    """Parámetros para importar archivos 3D."""
    model_config = ConfigDict(str_strip_whitespace=True)

    file_path: str = Field(..., description="Ruta absoluta al archivo a importar (.obj, .fbx, .glb, .abc, .ma, .mb)")
    namespace: Optional[str] = Field(default=None, description="Namespace para evitar colisiones de nombres")
    group_under: Optional[str] = Field(default=None, description="Nombre del grupo padre (se crea si no existe)")
    scale_factor: Optional[float] = Field(default=None, description="Factor de escala al importar (ej: 0.01 para cm→m)")


class ViewportCaptureInput(BaseModel):
    """Parámetros para capturar el viewport de Maya."""
    model_config = ConfigDict(str_strip_whitespace=True)

    output_path: str = Field(default="/tmp/maya_viewport.png", description="Ruta de salida para la imagen (.png/.jpg)")
    width: int = Field(default=1920, description="Ancho de la captura en píxeles", ge=100, le=8192)
    height: int = Field(default=1080, description="Alto de la captura en píxeles", ge=100, le=8192)
    camera: Optional[str] = Field(default=None, description="Cámara a usar (default: panel activo)")
    frame: Optional[float] = Field(default=None, description="Frame a capturar (default: frame actual)")


class ShelfButtonInput(BaseModel):
    """Parámetros para crear un botón en la shelf de Maya."""
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(..., description="Etiqueta del botón (texto corto)")
    command: str = Field(..., description="Código Python que ejecuta el botón al hacer click")
    tooltip: str = Field(default="", description="Texto de ayuda al pasar el ratón")
    shelf_name: str = Field(default="Custom", description="Nombre de la shelf donde crear el botón")
    icon_label: str = Field(default="MCP", description="Texto superpuesto en el icono (max 4 chars)")


# ─────────────────────────────────────────────
# Nuevos Tools (P2-P6, A-E)
# ─────────────────────────────────────────────


@mcp.tool(name="maya_mesh_operation")
async def maya_mesh_operation(params: MeshOperationInput) -> str:
    """Ejecuta operaciones de mesh: extrude, bevel, boolean (union/difference/intersection), combine, separate, smooth."""
    try:
        op = params.operation.value

        if op == "extrude":
            faces = params.faces or f"{params.object_name}.f[:]"
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_extrude')
try:
    result_faces = cmds.polyExtrudeFacet('{faces}', localTranslateZ={params.offset}, divisions={params.divisions})
    result = {{'operation': 'extrude', 'faces': '{faces}', 'offset': {params.offset}, 'result': str(result_faces)}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "bevel":
            faces = params.faces or f"{params.object_name}.e[:]"
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_bevel')
try:
    result_edges = cmds.polyBevel3('{faces}', offset={params.offset}, segments={params.divisions})
    result = {{'operation': 'bevel', 'target': '{faces}', 'offset': {params.offset}, 'result': str(result_edges)}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op.startswith("boolean_"):
            if not params.second_object:
                return json.dumps({"error": "Boolean requiere 'second_object'"})
            bool_op = {"boolean_union": 1, "boolean_difference": 2, "boolean_intersection": 3}[op]
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_boolean')
try:
    result_node = cmds.polyCBoolOp('{params.object_name}', '{params.second_object}', op={bool_op}, ch=False)
    result = {{'operation': '{op}', 'objects': ['{params.object_name}', '{params.second_object}'], 'result': str(result_node[0])}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "combine":
            if not params.second_object:
                return json.dumps({"error": "Combine requiere 'second_object'"})
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_combine')
try:
    combined = cmds.polyUnite('{params.object_name}', '{params.second_object}', ch=False)
    result = {{'operation': 'combine', 'result': str(combined[0])}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "separate":
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_separate')
try:
    separated = cmds.polySeparate('{params.object_name}', ch=False)
    result = {{'operation': 'separate', 'parts': [str(s) for s in separated]}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        elif op == "smooth":
            code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_smooth')
try:
    cmds.polySmooth('{params.object_name}', divisions={params.divisions})
    verts = cmds.polyEvaluate('{params.object_name}', vertex=True)
    faces = cmds.polyEvaluate('{params.object_name}', face=True)
    result = {{'operation': 'smooth', 'object': '{params.object_name}', 'divisions': {params.divisions}, 'vertices': verts, 'faces': faces}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        else:
            return json.dumps({"error": f"Operación desconocida: {op}"})

        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_set_keyframe")
async def maya_set_keyframe(params: KeyframeInput) -> str:
    """Crea un keyframe de animación en un objeto. Permite animar translate, rotate, scale y visibility por frame."""
    try:
        code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_keyframe')
try:
    cmds.setKeyframe('{params.object_name}', attribute='{params.attribute}',
                     value={params.value}, time={params.frame},
                     inTangentType='{params.in_tangent}', outTangentType='{params.out_tangent}')
    result = {{'object': '{params.object_name}', 'attribute': '{params.attribute}',
              'value': {params.value}, 'frame': {params.frame}}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_import_file")
async def maya_import_file(params: ImportFileInput) -> str:
    """Importa archivos 3D en Maya: OBJ, FBX, GLB/GLTF, Alembic ABC, Maya MA/MB. Con opciones de namespace, grupo padre y escala."""
    try:
        ext = params.file_path.rsplit(".", 1)[-1].lower() if "." in params.file_path else ""
        ns_opt = f", namespace='{params.namespace}'" if params.namespace else ""
        group_code = ""
        if params.group_under:
            group_code = f"""
if not cmds.objExists('{params.group_under}'):
    cmds.group(empty=True, name='{params.group_under}')
"""
        scale_code = ""
        if params.scale_factor:
            scale_code = f"""
for _mcp_obj in _mcp_imported:
    if cmds.objectType(_mcp_obj) == 'transform':
        cmds.scale({params.scale_factor}, {params.scale_factor}, {params.scale_factor}, _mcp_obj)
"""
        # Build file type string
        file_types = {
            "obj": "OBJ", "fbx": "FBX", "abc": "Alembic",
            "glb": "glTF", "gltf": "glTF", "ma": "mayaAscii", "mb": "mayaBinary",
        }
        ftype = file_types.get(ext, "")
        type_opt = f", type='{ftype}'" if ftype else ""

        code = f"""
import maya.cmds as cmds
cmds.undoInfo(openChunk=True, chunkName='mcp_import')
try:
    _mcp_before = set(cmds.ls(transforms=True))
    {group_code}
    cmds.file('{params.file_path}', i=True, ignoreVersion=True,
              mergeNamespacesOnClash=False, returnNewNodes=True{ns_opt}{type_opt})
    _mcp_after = set(cmds.ls(transforms=True))
    _mcp_imported = list(_mcp_after - _mcp_before)
    {scale_code}
    result = {{'imported': len(_mcp_imported), 'objects': _mcp_imported[:20],
              'file': '{params.file_path}'}}
finally:
    cmds.undoInfo(closeChunk=True)
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_viewport_capture")
async def maya_viewport_capture(params: ViewportCaptureInput) -> str:
    """Captura el viewport de Maya como imagen PNG/JPG. No hace render Arnold — es un grab instantáneo del viewport (<1s). Útil para verificar visualmente el estado de la escena."""
    try:
        camera_opt = ""
        if params.camera:
            camera_opt = f"""
# Set camera for the active panel
_mcp_panel = cmds.getPanel(withFocus=True)
if cmds.getPanel(typeOf=_mcp_panel) == 'modelPanel':
    cmds.modelPanel(_mcp_panel, edit=True, camera='{params.camera}')
"""
        frame_opt = f", frame=[{params.frame}]" if params.frame is not None else ""
        fmt = "png" if params.output_path.endswith(".png") else "jpg"

        code = f"""
import maya.cmds as cmds
import os
cmds.undoInfo(stateWithoutFlush=False)
try:
    {camera_opt}
    _mcp_img = cmds.playblast(
        completeFilename='{params.output_path}',
        format='image', compression='{fmt}',
        width={params.width}, height={params.height},
        showOrnaments=False, viewer=False,
        offScreen=True, percent=100{frame_opt}
    )
    _mcp_size = os.path.getsize('{params.output_path}') // 1024
    result = {{'captured': '{params.output_path}', 'size_kb': _mcp_size,
              'resolution': '{params.width}x{params.height}'}}
finally:
    cmds.undoInfo(stateWithoutFlush=True)
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_scene_snapshot")
async def maya_scene_snapshot() -> str:
    """Devuelve un snapshot completo del estado de la escena: archivo, modificada, frame, objetos por tipo, renderer, plugins, resolución de render. Útil para tomar decisiones informadas antes de operaciones."""
    try:
        code = """
import maya.cmds as cmds
_mcp_meshes = cmds.ls(type='mesh') or []
_mcp_lights = cmds.ls(lights=True) or []
_mcp_cameras = cmds.ls(cameras=True) or []
_mcp_curves = cmds.ls(type='nurbsCurve') or []
_mcp_transforms = cmds.ls(transforms=True) or []
_mcp_plugins = cmds.pluginInfo(query=True, listPlugins=True) or []

result = {
    'file': cmds.file(q=True, sceneName=True) or 'untitled',
    'modified': cmds.file(q=True, modified=True),
    'current_frame': cmds.currentTime(q=True),
    'frame_range': [cmds.playbackOptions(q=True, min=True), cmds.playbackOptions(q=True, max=True)],
    'renderer': cmds.getAttr('defaultRenderGlobals.currentRenderer'),
    'render_resolution': [
        cmds.getAttr('defaultResolution.width'),
        cmds.getAttr('defaultResolution.height')
    ],
    'counts': {
        'transforms': len(_mcp_transforms),
        'meshes': len(_mcp_meshes),
        'lights': len(_mcp_lights),
        'cameras': len(_mcp_cameras),
        'curves': len(_mcp_curves),
    },
    'loaded_plugins': _mcp_plugins[:20],
    'up_axis': cmds.upAxis(q=True, axis=True),
    'units': cmds.currentUnit(q=True, linear=True),
}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="maya_shelf_button")
async def maya_shelf_button(params: ShelfButtonInput) -> str:
    """Crea un botón personalizado en la shelf de Maya con código Python asociado. Permite que Claude deje herramientas reutilizables en la interfaz."""
    try:
        # Escape the command for embedding in Python string
        safe_command = params.command.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        code = f"""
import maya.cmds as cmds
import maya.mel as mel

# Crear o encontrar la shelf
_mcp_shelf = '{params.shelf_name}'
if not cmds.shelfLayout(_mcp_shelf, exists=True):
    mel.eval('addNewShelfTab "{params.shelf_name}"')

_mcp_btn = cmds.shelfButton(
    parent=_mcp_shelf,
    label='{params.label}',
    annotation='{params.tooltip}',
    imageOverlayLabel='{params.icon_label[:4]}',
    image='pythonFamily.png',
    command='{safe_command}',
    sourceType='python'
)
result = {{'button': _mcp_btn, 'shelf': _mcp_shelf, 'label': '{params.label}'}}
"""
        return await asyncio.to_thread(bridge.execute, code)
    except Exception as e:
        return _handle_error(e)


# ─────────────────────────────────────────────
# GPU remoto — Vision3D API REST (Hunyuan3D-2)
# ─────────────────────────────────────────────

from mcp.server.fastmcp import Context

# Configuración via variables de entorno
_GPU_API_URL  = os.environ.get("GPU_API_URL",  "http://localhost:8000").rstrip("/")
_GPU_API_KEY  = os.environ.get("GPU_API_KEY",  "")
_GPU_VERIFY   = os.environ.get("GPU_VERIFY_TLS", "false").lower() in ("true", "1", "yes")
_MAC_BASE_DIR = os.environ.get("MAYA_BASE_DIR",
                                str(Path(__file__).parent.parent))           # raíz del proyecto en Mac

# Lazy httpx client
_http_client = None

# Track log cursors per job (for incremental log delivery)
_job_log_cursors: dict[str, int] = {}


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


async def _download_file(job_id: str, filename: str, dest: Path) -> bool:
    """Download a single file from a completed job."""
    client = _get_http_client()
    resp = await client.get(f"/api/jobs/{job_id}/files/{filename}")
    if resp.status_code == 200:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    return False


def _build_quality_form_data(params) -> dict:
    """Build form_data dict with quality params from a ShapeGenerateInput or ShapeTextInput."""
    form_data = {}
    if hasattr(params, "target_faces") and params.target_faces > 0:
        form_data["target_faces"] = str(params.target_faces)
    elif hasattr(params, "target_faces"):
        form_data["target_faces"] = str(params.target_faces)
    if params.preset:
        form_data["preset"] = params.preset
    if params.model:
        form_data["model"] = params.model
    if params.octree_resolution > 0:
        form_data["octree_resolution"] = str(params.octree_resolution)
    if params.num_inference_steps > 0:
        form_data["num_inference_steps"] = str(params.num_inference_steps)
    return form_data


# ── Input models ──────────────────────────────────────────────────────────


class ShapeGenerateInput(BaseModel):
    """Parámetros para iniciar generación 3D desde imagen en Vision3D.

    Presets de calidad:
      - low:    turbo, octree 256, 10 steps, 10k faces   (~1 min, preview rápido)
      - medium: turbo, octree 384, 20 steps, 50k faces   (~2 min, uso general)
      - high:   full,  octree 384, 30 steps, 150k faces  (~8 min, detallado)
      - ultra:  full,  octree 512, 50 steps, sin límite   (~12 min, máximo detalle)
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    image_path: str = Field(
        ...,
        description="Ruta local absoluta a la imagen de referencia (.jpg/.png)."
    )
    output_subdir: str = Field(
        default="0",
        description="Subdirectorio de salida dentro de reference/3d_output/ (ej: '0', 'asset_1478')"
    )
    preset: str = Field(
        default="",
        description="Preset de calidad: 'low', 'medium', 'high', 'ultra'. "
                    "Los parámetros individuales sobreescriben al preset."
    )
    model: str = Field(
        default="",
        description="Modelo de shape: 'turbo' (~1 min) o 'full' (~5 min, más detalle). "
                    "Vacío = usa el del preset o 'turbo' por defecto."
    )
    octree_resolution: int = Field(
        default=0,
        description="Resolución octree (256/384/512). 0 = usa el del preset."
    )
    num_inference_steps: int = Field(
        default=0,
        description="Pasos de inferencia. turbo: 5-10, full: 30-50. 0 = usa el del preset."
    )
    target_faces: int = Field(
        default=50000,
        description="Caras objetivo tras decimación. 0 = sin decimación."
    )


class ShapeTextInput(BaseModel):
    """Parámetros para iniciar generación 3D desde texto en Vision3D."""
    model_config = ConfigDict(str_strip_whitespace=True)

    text_prompt: str = Field(
        ...,
        description="Descripción en inglés del objeto 3D a generar."
    )
    output_subdir: str = Field(
        default="0",
        description="Subdirectorio de salida (ej: '0', 'mailbox_0')"
    )
    preset: str = Field(default="", description="Preset: 'low', 'medium', 'high', 'ultra'.")
    model: str = Field(default="", description="'turbo' o 'full'. Vacío = preset.")
    octree_resolution: int = Field(default=0, description="256/384/512. 0 = preset.")
    num_inference_steps: int = Field(default=0, description="Pasos. 0 = preset.")
    target_faces: int = Field(default=0, description="Caras objetivo. 0 = sin decimación.")


class TextureRemoteInput(BaseModel):
    """Parámetros para iniciar texturizado en Vision3D."""
    model_config = ConfigDict(str_strip_whitespace=True)

    output_subdir: str = Field(
        ...,
        description="Subdirectorio dentro de reference/3d_output/"
    )
    mesh_filename: str = Field(
        default="mesh.glb",
        description="Nombre del mesh dentro de output_subdir"
    )
    image_filename: str = Field(
        default="input.png",
        description="Nombre de la imagen de referencia dentro de output_subdir"
    )


class Vision3DPollInput(BaseModel):
    """Parámetros para sondear el estado de un job en Vision3D."""
    model_config = ConfigDict(str_strip_whitespace=True)

    job_id: str = Field(..., description="ID del job devuelto por shape_generate_remote/text/texture.")


class Vision3DDownloadInput(BaseModel):
    """Parámetros para descargar los resultados de un job completado."""
    model_config = ConfigDict(str_strip_whitespace=True)

    job_id: str = Field(..., description="ID del job completado.")
    output_subdir: str = Field(..., description="Subdirectorio local de salida (el mismo usado al crear el job).")
    files: List[str] = Field(
        default_factory=lambda: ["textured.glb", "mesh_uv.obj", "texture_baked.png", "mesh.glb"],
        description="Lista de archivos a descargar. Por defecto descarga todos los del pipeline completo."
    )


# ── Tools: comprobar disponibilidad de Vision3D ─────────────────────────


@mcp.tool(name="vision3d_health")
async def vision3d_health(ctx: Context) -> str:
    """Comprueba si el servidor Vision3D está disponible y responde.

    Retorna información de GPU, modelos disponibles, y si text-to-3D está activo.
    Llama a este tool ANTES de ofrecer opciones de generación IA al usuario,
    para saber si Vision3D está encendido y accesible.
    """
    try:
        client = _get_http_client()
        await ctx.info("Comprobando disponibilidad de Vision3D...")
        resp = await client.get("/api/health", timeout=5.0)

        if resp.status_code != 200:
            return json.dumps({
                "available": False,
                "error": f"Vision3D respondió con HTTP {resp.status_code}",
                "url": _GPU_API_URL,
            })

        health = resp.json()
        return json.dumps({
            "available": True,
            "url": _GPU_API_URL,
            "gpu": health.get("gpu", "unknown"),
            "vram_gb": health.get("vram_gb"),
            "models": health.get("models", []),
            "text_to_3d": health.get("text_to_3d", "unknown"),
        }, indent=2)

    except Exception as e:
        return json.dumps({
            "available": False,
            "error": f"No se pudo conectar a Vision3D ({_GPU_API_URL}): {e}",
            "hint": "Verifica que el servidor Vision3D está encendido y accesible desde esta red.",
        })


# ── Tools: iniciar jobs (non-blocking) ───────────────────────────────────


@mcp.tool(name="shape_generate_remote")
async def shape_generate_remote(params: ShapeGenerateInput, ctx: Context) -> str:
    """Inicia generación 3D texturizada desde imagen en Vision3D (non-blocking).

    Sube la imagen y arranca el pipeline completo (shape + decimación + texturizado).
    Retorna un job_id inmediatamente. Usa vision3d_poll para seguir el progreso
    y vision3d_download para descargar los resultados cuando termine.
    """
    try:
        image_local = Path(params.image_path)
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir

        if not image_local.exists():
            return json.dumps({
                "error": f"Imagen no encontrada: {image_local}",
                "hint": "Descarga primero la imagen con sg_download de fpt-mcp."
            })

        out_dir.mkdir(parents=True, exist_ok=True)

        # Copiar imagen al directorio de salida como input.png
        import shutil
        input_copy = out_dir / "input.png"
        shutil.copy2(str(image_local), str(input_copy))

        client = _get_http_client()
        quality_desc = params.preset or f"model={params.model or 'turbo'}"

        await ctx.info(f"Subiendo imagen a Vision3D ({quality_desc})...")

        form_data = {"output_subdir": params.output_subdir}
        form_data.update(_build_quality_form_data(params))

        with open(str(image_local), "rb") as f:
            resp = await client.post(
                "/api/generate-full",
                files={"image": (image_local.name, f, "image/png")},
                data=form_data,
            )

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verifica que Vision3D está corriendo: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Job iniciado: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": params.output_subdir,
            "output_dir": str(out_dir),
            "quality": quality_desc,
            "image_copy": str(input_copy),
            "next_step": f"Llama a vision3d_poll(job_id='{job_id}') para ver el progreso. "
                         f"Cuando status sea 'completed', llama a vision3d_download(job_id='{job_id}', "
                         f"output_subdir='{params.output_subdir}').",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(name="shape_generate_text")
async def shape_generate_text(params: ShapeTextInput, ctx: Context) -> str:
    """Inicia generación 3D desde texto en Vision3D (non-blocking).

    Envía el prompt y arranca el pipeline text-to-3D.
    Retorna job_id. Usa vision3d_poll para seguir el progreso.
    """
    try:
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)

        client = _get_http_client()
        quality_desc = params.preset or f"model={params.model or 'turbo'}"

        await ctx.info(f"Enviando prompt a Vision3D: '{params.text_prompt}' ({quality_desc})...")

        form_data = {
            "text_prompt": params.text_prompt,
            "output_subdir": params.output_subdir,
        }
        form_data.update(_build_quality_form_data(params))

        resp = await client.post("/api/generate-text", data=form_data)

        if resp.status_code != 200:
            return json.dumps({
                "error": f"GPU API error ({resp.status_code}): {resp.text}",
                "hint": f"Verifica que Vision3D está corriendo: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Job iniciado: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": params.output_subdir,
            "output_dir": str(out_dir),
            "quality": quality_desc,
            "next_step": f"Llama a vision3d_poll(job_id='{job_id}') para seguir el progreso.",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(name="texture_mesh_remote")
async def texture_mesh_remote(params: TextureRemoteInput, ctx: Context) -> str:
    """Inicia texturizado de mesh en Vision3D (non-blocking).

    Sube mesh + imagen y arranca el pipeline de texturizado.
    Retorna job_id. Usa vision3d_poll para seguir el progreso.
    """
    try:
        out_dir     = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir
        mesh_local  = out_dir / params.mesh_filename
        image_local = out_dir / params.image_filename

        if not mesh_local.exists():
            return json.dumps({
                "error": f"Mesh no encontrado: {mesh_local}",
                "hint":  "Genera primero el mesh con shape_generate_remote."
            })
        if not image_local.exists():
            return json.dumps({
                "error":  f"Imagen no encontrada: {image_local}",
                "hint":   f"Copia la imagen como '{params.image_filename}' en {out_dir}"
            })

        client = _get_http_client()

        await ctx.info(f"Subiendo {params.mesh_filename} + {params.image_filename} a Vision3D...")

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
                "hint": f"Verifica Vision3D: curl -k {_GPU_API_URL}/api/health"
            })

        job = resp.json()
        job_id = job["job_id"]
        _job_log_cursors[job_id] = 0

        await ctx.info(f"Job de texturizado iniciado: {job_id}")

        return json.dumps({
            "status": "started",
            "job_id": job_id,
            "output_subdir": params.output_subdir,
            "next_step": f"Llama a vision3d_poll(job_id='{job_id}') para seguir el progreso.",
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tools: sondear progreso y descargar ──────────────────────────────────


@mcp.tool(name="vision3d_poll")
async def vision3d_poll(params: Vision3DPollInput, ctx: Context) -> str:
    """Sondea el estado de un job en Vision3D. Devuelve las líneas de log nuevas
    desde la última llamada (progreso incremental).

    Llama a este tool repetidamente mientras status sea 'running'.
    Cuando status sea 'completed', llama a vision3d_download.
    Cuando status sea 'failed', muestra el error al usuario.
    """
    try:
        client = _get_http_client()
        resp = await client.get(f"/api/jobs/{params.job_id}")

        if resp.status_code == 404:
            return json.dumps({"error": f"Job '{params.job_id}' no encontrado en Vision3D."})

        resp.raise_for_status()
        job = resp.json()

        # Deliver only new log lines since last poll
        cursor = _job_log_cursors.get(params.job_id, 0)
        all_log = job.get("log", [])
        new_lines = all_log[cursor:]
        _job_log_cursors[params.job_id] = len(all_log)

        # ctx.info for future MCP progress support
        for line in new_lines:
            await ctx.info(line)

        elapsed = job.get("elapsed_s", 0)
        status = job["status"]

        result = {
            "status": status,
            "elapsed_s": elapsed,
            "new_log_lines": new_lines,
            "total_log_lines": len(all_log),
        }

        if status == "completed":
            result["files"] = [f["name"] for f in job.get("files", [])]
            result["next_step"] = (
                f"Job completado en {elapsed}s. Llama a vision3d_download("
                f"job_id='{params.job_id}', output_subdir='...') para descargar los archivos."
            )
            # Cleanup cursor
            _job_log_cursors.pop(params.job_id, None)
        elif status == "failed":
            result["error"] = job.get("error", "Error desconocido")
            _job_log_cursors.pop(params.job_id, None)
        else:
            result["next_step"] = (
                f"Job en progreso ({elapsed}s). Vuelve a llamar a "
                f"vision3d_poll(job_id='{params.job_id}') para actualizar."
            )

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(name="vision3d_download")
async def vision3d_download(params: Vision3DDownloadInput, ctx: Context) -> str:
    """Descarga los archivos de un job completado de Vision3D al directorio local.

    Llama a este tool después de que vision3d_poll reporte status='completed'.
    Descarga los archivos especificados al subdirectorio local de salida.
    """
    try:
        out_dir = Path(_MAC_BASE_DIR) / "reference" / "3d_output" / params.output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)

        await ctx.info(f"Descargando {len(params.files)} archivos de Vision3D...")

        downloaded = []
        failed = []

        for fname in params.files:
            ok = await _download_file(params.job_id, fname, out_dir / fname)
            if ok:
                size_kb = (out_dir / fname).stat().st_size // 1024
                downloaded.append({"name": fname, "size_kb": size_kb})
                await ctx.info(f"  {fname} ({size_kb} KB)")
            else:
                failed.append(fname)

        baked_ready = (out_dir / "mesh_uv.obj").exists() and \
                      (out_dir / "texture_baked.png").exists()
        textured_ready = (out_dir / "textured.glb").exists()

        return json.dumps({
            "status": "ok",
            "output_dir": str(out_dir),
            "downloaded": downloaded,
            "failed": failed,
            "textured": textured_ready,
            "baked_texture": baked_ready,
            "next_step": (
                "Archivos descargados. Importa textured.glb en Maya con maya_execute_python, "
                "o usa mesh_uv.obj + texture_baked.png para control total de UVs."
                if textured_ready else
                "Descarga parcial. Revisa 'failed' para ver qué archivos fallaron."
            ),
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
