# maya-mcp — Contexto Crítico para Claude

> **Última actualización**: 2026-03-30
> Este documento persiste entre sesiones de Claude Code. Consulta aquí para entender la arquitectura, configuración y flujos de trabajo de maya-mcp.

---

## 1. Arquitectura

**maya-mcp** es un servidor **MCP (Model Context Protocol)** basado en **FastMCP** que expone herramientas para:

1. **Controlar Autodesk Maya** (local, macOS)
   - Se comunica con Maya via **TCP Command Port** (puerto 7001 por defecto)
   - Usa `maya_bridge.py` (socket bridge) para ejecutar comandos MEL/Python

2. **Orquestar Vision3D** (servidor GPU remoto)
   - Se comunica via **REST API HTTP** con Vision3D (LAN directa, sin Caddy/HTTPS)
   - Soporta generación 3D desde imagen (shape generation + texturizado)
   - Soporta generación desde texto (text-to-3D)
   - Soporta texturizado de meshes existentes

```
┌──────────────────┐
│   Claude Code    │
└────────┬─────────┘
         │ (MCP Protocol)
┌────────▼──────────────────────────────┐
│   maya-mcp FastMCP Server             │
│  (core/server.py: 13 tools)           │
├────────────────────────────────────────┤
│  Maya Bridge (TCP)     Vision3D REST   │
└────┬───────────────────────┬──────────┘
     │ :7001 Command Port    │ HTTP :8000
     │                       │
┌────▼──────────────┐   ┌───▼──────────────────┐
│ Autodesk Maya     │   │ Vision3D GPU Server  │
│ (local Mac)       │   │ (glorfindel)         │
└───────────────────┘   │ Hunyuan3D-2          │
                        └──────────────────────┘
```

---

## 2. Entorno de Ejecución

### Ubicación
- **Repositorio**: `~/Claude_projects/maya-mcp-project/` (Mac local)
- **Servidor MCP**: corre con `python core/server.py` (stdio transport estándar MCP)
- **Configuración MCP**: `~/.claude.json` (vía `claude mcp add -s user`)
- **Permisos de tools**: `~/.claude/settings.json`

### Variables de Entorno (`.env`)
```bash
# Maya Local
MAYA_HOST=localhost          # Host donde corre Maya (default: localhost)
MAYA_PORT=7001              # Puerto Command Port (default: 7001)

# Vision3D (GPU remoto — HTTP directo, sin Caddy)
GPU_API_URL=http://glorfindel:8000       # HTTP endpoint del GPU server
GPU_API_KEY=                              # Dejar vacío si acceso abierto en LAN
```

**Nota**: La variable `GPU_API_URL` también se configura en `~/.claude.json` vía `claude mcp add`. No se necesita Caddy ni HTTPS para acceso LAN.

### Requisitos
- **macOS Ventura+** con Apple Silicon (soporte Intel)
- **Autodesk Maya 2023+** (testeado en 2026)
- **Arnold** (`mtoa` plugin, incluido con Maya)
- **Command Port habilitado** en `userSetup.py` de Maya:
  ```python
  cmds.commandPort(name=':7001', sourceType='mel')
  ```
- **Python 3.10+** para ejecutar `core/server.py`

---

## 3. Tools Disponibles

### Maya Tools (11 herramientas)

| Tool | Descripción |
|------|-------------|
| `maya_launch` | Abre Maya y espera a que el Command Port responda (max 90s) |
| `maya_ping` | Verifica conexión con Maya, devuelve versión, escena actual, renderer |
| `maya_create_primitive` | Crea primitivas 3D (cube, sphere, cylinder, cone, plane, torus) con pos/rot/scale |
| `maya_assign_material` | Crea y asigna material (lambert, blinn, phong, aiStandardSurface) con color RGB |
| `maya_transform` | Mueve, rota, escala objetos en world space o relative |
| `maya_list_scene` | Lista objetos de escena con filtros por tipo o nombre (wildcards) |
| `maya_delete` | Elimina objetos por nombre (soporta wildcards como `*sphere*`) |
| `maya_execute_python` | Ejecuta código Python arbitrario en Maya (result variable) |
| `maya_new_scene` | Crea nueva escena vacía (descarta sin guardar) |
| `maya_save_scene` | Guarda escena actual (requiere nombre previo) |
| `maya_create_light` | Crea luces (directional, point, spot, area, ambient) con intensidad/color/posición |
| `maya_create_camera` | Crea cámara con posición, look_at point, focal length configurables |

### Vision3D Tools (6 herramientas)

| Tool | Descripción |
|------|-------------|
| `vision3d_health` | Verifica si el servidor Vision3D está disponible, GPU, modelos, text-to-3D |
| `shape_generate_remote` | Inicia generación 3D texturizada desde imagen (non-blocking, retorna job_id) |
| `shape_generate_text` | Inicia generación 3D desde prompt de texto (non-blocking, retorna job_id) |
| `texture_mesh_remote` | Inicia texturizado de mesh existente (non-blocking, retorna job_id) |
| `vision3d_poll` | Sondea estado del job, devuelve líneas de log nuevas (progreso incremental) |
| `vision3d_download` | Descarga archivos de job completado al directorio local |

---

## 4. Flujo Granular Vision3D (Non-Blocking)

Vision3D opera con un **patrón non-blocking de 3 pasos**:

### Paso 1: Iniciar Job (Retorna job_id inmediatamente)
```python
# Ejemplo con shape_generate_remote
response = shape_generate_remote(
    image_path="/path/to/reference.png",
    output_subdir="asset_001",
    preset="medium"  # low/medium/high/ultra
)
# Respuesta: {"status": "started", "job_id": "abc123", ...}
```

### Paso 2: Sondear Progreso (Llamar repetidamente)
```python
# Mientras status sea 'running', llamar vision3d_poll
response = vision3d_poll(job_id="abc123")
# Respuesta:
# {
#   "status": "running",
#   "elapsed_s": 45,
#   "new_log_lines": ["[Shape] Generating...", "[Shape] Done."],
#   "total_log_lines": 12,
#   "next_step": "Vuelve a llamar a vision3d_poll..."
# }
```

**Clave**: `_job_log_cursors` es un dict que trackea la posición de lectura de logs por `job_id`, permitiendo entrega **incremental** de nuevas líneas en cada poll.

### Paso 3: Descargar Resultados (Cuando status='completed')
```python
response = vision3d_download(
    job_id="abc123",
    output_subdir="asset_001",
    files=["textured.glb", "mesh_uv.obj", "texture_baked.png", "mesh.glb"]
)
# Respuesta:
# {
#   "status": "ok",
#   "output_dir": "/path/to/3d_output/asset_001",
#   "downloaded": [{"name": "textured.glb", "size_kb": 2048}, ...],
#   "failed": [],
#   "textured": true,
#   "baked_texture": true
# }
```

### Pre-requisito: Verificar disponibilidad Vision3D
**ANTES de ofrecer opciones de IA al usuario**, siempre llama:
```python
health = vision3d_health()
# Respuesta: {"available": true, "gpu": "NVIDIA RTX 4090", "vram_gb": 24, "text_to_3d": "enabled", ...}
```

---

## 5. Modelos Pydantic de Entrada

### ShapeGenerateInput
```python
class ShapeGenerateInput(BaseModel):
    image_path: str              # Ruta local absoluta a imagen (.jpg/.png)
    output_subdir: str = "0"     # Subdirectorio en reference/3d_output/
    preset: str = ""             # "low" | "medium" | "high" | "ultra"
    model: str = ""              # "turbo" (~1 min) | "full" (~5 min)
    octree_resolution: int = 0   # 256/384/512 (0 = usa preset)
    num_inference_steps: int = 0 # turbo: 5-10, full: 30-50
    target_faces: int = 50000    # Caras tras decimación (0 = sin decimación)
```

**Presets de calidad**:
- `low`: turbo, octree 256, 10 steps, 10k faces (~1 min, preview rápido)
- `medium`: turbo, octree 384, 20 steps, 50k faces (~2 min, general)
- `high`: full, octree 384, 30 steps, 150k faces (~8 min, detallado)
- `ultra`: full, octree 512, 50 steps, sin límite (~12 min, máximo detalle)

### ShapeTextInput
```python
class ShapeTextInput(BaseModel):
    text_prompt: str             # Descripción en inglés
    output_subdir: str = "0"
    preset: str = ""
    model: str = ""
    octree_resolution: int = 0
    num_inference_steps: int = 0
    target_faces: int = 0
```

### TextureRemoteInput
```python
class TextureRemoteInput(BaseModel):
    output_subdir: str           # Subdirectorio de salida
    mesh_filename: str = "mesh.glb"
    image_filename: str = "input.png"
```

### Vision3DPollInput
```python
class Vision3DPollInput(BaseModel):
    job_id: str  # ID retornado por shape_generate_remote/text/texture
```

### Vision3DDownloadInput
```python
class Vision3DDownloadInput(BaseModel):
    job_id: str                  # ID del job completado
    output_subdir: str           # Subdirectorio local
    files: List[str] = [...]     # Archivos a descargar
```

---

## 6. Bugs Conocidos & Historial

### Maya Command Port: Conexión rechazada
**Problema**: `maya-mcp` a veces no puede ejecutar comandos en Maya (error: "Connection refused")

**Causa**: Command Port no está habilitado en `userSetup.py` de Maya

**Solución**:
1. En Maya Script Editor, ejecutar:
   ```mel
   commandPort -name ":7001" -sourceType mel;
   ```
2. O añadir permanentemente a `~/Library/Preferences/Autodesk/maya/<version>/scripts/userSetup.py`:
   ```python
   import maya.cmds as cmds
   try:
       cmds.commandPort(name=':7001', sourceType='mel')
   except:
       pass
   ```
3. Reiniciar Maya

### Reinicio del servidor MCP
**PENDIENTE**: Verificar si hay que reiniciar `maya-mcp` después de cambios en `server.py`.
- Depende de cómo se lance (stdio vs. persistent process)
- Si usa Claude Code CLI con `mcp run`, probablemente requiere reinicio manual

---

## 7. Relación con Otros Proyectos

### vision3d (GitHub: abrahamADSK/vision3d)
- **Ubicación**: Servidor GPU remoto (`glorfindel`), directorio `/home/flame/ai-studio/vision3d/`
- **Función**: Genera formas 3D y texturas vía Hunyuan3D-2
- **Interfaz**: REST API HTTP (puerto 8000)
- **Consumido por**: `maya-mcp` vía `shape_generate_remote`, `shape_generate_text`, `texture_mesh_remote`
- **Text-to-3D**: Pipeline completo de 3 fases (HunyuanDiT → rembg → shape → paint)
- **Web UI**: `http://glorfindel:8000/` con tabs Image→3D, Text→3D + visor 3D orbit

### fpt-mcp
- **Ubicación**: `~/Claude_projects/fpt-mcp/`
- **Función**: Consola Qt que orquesta `maya-mcp` + otras herramientas vía Claude Code CLI
- **System prompt**: Define el workflow completo (qué herramientas llamar, cuándo, en qué orden)
- **Relación**: `fpt-mcp` es el "director", `maya-mcp` es una "pieza de la orquesta"

### Los tres repos (maya-mcp, vision3d, fpt-mcp)
- Ubicados en `~/Claude_projects/` en el Mac local
- Se comunican vía HTTP REST API (no SSH directo, no HTTPS/Caddy)
- **Importante**: NUNCA mezclar comandos de Mac y glorfindel en el mismo bloque de código

---

## 8. Notas para Desarrollo

### Cambios en server.py
- Después de editar `core/server.py`, **hay que reiniciar el servidor MCP**
- Si usa Claude Code CLI, requiere un nuevo comando `mcp run`

### Añadir nuevos tools
1. Definir modelo Pydantic de entrada (en `server.py`)
2. Implementar función con decorador `@mcp.tool(name="nombre_tool")`
3. Añadir permisos en `~/.claude/settings.json`:
   ```json
   {
     "mcp_tools": [
       { "name": "nombre_tool", "enabled": true }
     ]
   }
   ```
4. Reiniciar servidor MCP

### Context parameter (`ctx: Context`)
- Se importa de `mcp.server.fastmcp`
- Se usa para `ctx.info(mensaje)` para logging/progreso
- **Future-proofing**: Cuando MCP soporte progreso nativo, estos `ctx.info()` se mostrarán automáticamente en Claude

### Estructura de logs incremental
- `_job_log_cursors: dict[str, int]` trackea la posición de lectura de logs por `job_id`
- En `vision3d_poll()`, devuelve solo líneas nuevas desde el último poll
- Permite UI responsiva y no repetir logs

### Directorio de salida 3D (MacOS)
```
~/Developer/Maya_projects/reference/3d_output/
├── 0/              # Default output_subdir
│   ├── input.png          # Copia de imagen de entrada
│   ├── mesh.glb           # Shape generada (decimada)
│   ├── mesh_uv.obj        # Mesh con UVs para texturizado
│   ├── texture_baked.png  # Mapa de textura baked
│   └── textured.glb       # GLB final con textura embebida
├── asset_001/
├── asset_002/
└── ...
```

---

## 9. Checklist de Configuración Inicial

- [ ] Clonar `maya-mcp` en `~/Claude_projects/maya-mcp-project/`
- [ ] Copiar `.env.example` → `.env` y configurar variables
- [ ] Instalar dependencias: `pip install -r core/requirements.txt`
- [ ] Verificar que Maya tiene Command Port en `userSetup.py`
- [ ] Lanzar servidor: `python core/server.py`
- [ ] Configurar en `~/.claude.json`: `claude mcp add -s user`
- [ ] Verificar permisos en `~/.claude/settings.json`
- [ ] Probar connection: `maya_ping()`
- [ ] Probar Vision3D: `vision3d_health()`

---

**Mantén este archivo actualizado cuando cambies la arquitectura, agregues tools nuevos, o encuentres bugs.**
