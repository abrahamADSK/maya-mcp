# HANDOFF — maya-mcp

**Nivel de completitud: Alto (~85%)**. 27 tools implementados, RAG funcional, console panel dockable en Maya.

---

## Estado actual

**Funciona**:
- **14 visible MCP tools** (dispatch pattern O1b): 9 Tier-1 + 3 Meta/RAG + 2 dispatch (`maya_session`, `maya_vision3d`)
- Internamente: 9 session handlers (`_do_ping`, `_do_launch`, etc.) + 6 Vision3D handlers (`_do_v3d_*`) — sin `@mcp.tool`
- 18 Maya scene ops, 6 Vision3D integration, 3 RAG/Intelligence (funcionalidad completa preservada)
- RAG híbrido (ChromaDB + BM25 + HyDE + RRF) con 5 corpus docs (CMDS, PyMEL, Arnold, USD, Anti-Patterns)
- Safety module con 14+ regex patterns + explicaciones + alternativas seguras
- Token tracking con estimación de eficiencia
- Self-learning patterns con model trust gates (Sonnet/Opus write, otros stage)
- Console panel dockable en Maya (workspaceControl, PySide2/PySide6, persiste entre sesiones)
- MCP Pipeline menu en Maya menu bar (auto-instalado en primer connect)
- Server health monitor panel con status por MCP server (maya, fpt, flame, vision3d)
- Claude worker en QThread con subprocess streaming (claude -p --output-format stream-json)
- macOS .app bundle generator (build_app_bundle.py)
- Import GLB/OBJ/FBX/Alembic/MA/MB con namespace y escala
- Viewport capture (playblast a PNG/JPG)
- Scene snapshot (estado completo de la escena)
- Shelf button creator

**Limitaciones**:
- NO VERIFICADO — requiere Maya abierto con Command Port habilitado
- Vision3D tools requieren GPU server activo (glorfindel)

---

## Relación con vision3d

maya-mcp actúa como cliente REST de vision3d. 6 tools (`vision3d_health`, `shape_generate_remote`, `shape_generate_text`, `texture_mesh_remote`, `vision3d_poll`, `vision3d_download`) envían requests HTTP a `GPU_API_URL` (default: http://localhost:8000).

```
maya-mcp (Mac) --httpx--> vision3d FastAPI (glorfindel:8000)
```

Workflow: submit job → poll status (SSE streaming) → download GLB/OBJ → maya_import_file.

---

## Tests existentes

### Safety module — `tests/test_safety.py` (67 tests, all passing)

Suite de pytest para `core/safety.py`. Cubre los 15 patrones de detección de código peligroso con tests individuales por patrón + verificación de que inputs seguros no disparan falsos positivos.

| Clase | Tests | Cubre |
|---|---|---|
| TestNewSceneForce | 3 | `cmds.file(new=True, force=True)` |
| TestDeleteAllNodes | 2 | `cmds.delete(cmds.ls())` |
| TestWildcardDelete | 3 | `cmds.delete('*')` |
| TestUndoDisableWithoutFlush | 2 | `undoInfo(stateWithoutFlush=False)` |
| TestUndoDisableState | 3 | `undoInfo(state=False)` |
| TestFilesystemDeletion | 4 | `os.remove`, `os.unlink`, `shutil.rmtree` |
| TestPathTraversal | 3 | `../` y `..\\` |
| TestUnloadPlugin | 2 | `cmds.unloadPlugin` |
| TestRemoveNamespace | 2 | `removeNamespace` + `deleteNamespaceContent` |
| TestPolyReduceReferenced | 3 | `polyReduce` en geometría referenciada |
| TestMelSourceInjection | 3 | `mel.eval('source ...')` |
| TestUnlockNode | 2 | `cmds.lockNode(lock=False)` |
| TestBulkDeleteLoop | 3 | `for x in cmds.ls(): cmds.delete` |
| TestRemoveReference | 3 | `cmds.file(removeReference=True)` |
| TestRendererChange | 3 | Cambio de renderer no-Arnold |
| TestSafeInputPasses | 11 | Operaciones normales no bloqueadas |
| TestAll15Patterns | 15 (parametrizado) | Verificación exhaustiva de los 15 patrones |

Run: `pytest tests/test_safety.py -v`

No requiere Maya abierto ni dependencias externas.

### Import file — `tests/test_import_file.py` (19 tests, all passing)

Suite de pytest para `maya_import_file` en `core/server.py`. Monkeypatcha `bridge.execute` para capturar el código Python enviado a Maya y verificar que contiene los comandos correctos. Stubs internos para `mcp`, `maya_bridge`, y `safety`.

| Clase | Tests | Cubre |
|---|---|---|
| TestImportGLB | 2 | GLB → type='glTF', GLTF → type='glTF' |
| TestImportOBJ | 1 | OBJ → type='OBJ' |
| TestImportFBX | 4 | FBX → type='FBX', ABC → Alembic, MA → mayaAscii, MB → mayaBinary |
| TestImportNamespace | 2 | Namespace en cmds.file(), sin namespace por defecto |
| TestImportScale | 3 | Scale factor en cmds.scale(), sin scale por defecto, solo transforms |
| TestImportErrors | 3 | MayaBridgeError, RuntimeError, group_under creates group |
| TestImportStructure | 4 | Undo chunk, returnNewNodes, before/after diff, extensión desconocida |

Run: `pytest tests/test_import_file.py -v`

No requiere Maya abierto ni dependencias MCP/externas.

### Vision3D integration — `tests/test_vision3d.py` (21 tests, all passing)

Suite de pytest para las 6 Vision3D tools en `core/server.py`. Usa `httpx.MockTransport` para simular la API REST de Vision3D — no requiere GPU server, red, ni Maya abierto. Stubs internos para `mcp`, `maya_bridge`, y `safety` (no requiere los SDKs instalados).

| Clase | Tests | Cubre |
|---|---|---|
| TestVision3dHealth | 3 | Health available (200), non-200, server down |
| TestShapeGenerateRemote | 3 | Submit → job_id, image not found, API error |
| TestShapeGenerateText | 2 | Text submit → job_id, API error |
| TestVision3dPoll | 5 | Running + logs, incremental logs, completed + files, failed, 404 not found |
| TestVision3dDownload | 3 | Download to disk, partial failure, size reporting |
| TestServerDown | 5 | ConnectError en health/poll/download/generate_remote/generate_text |

Run: `pytest tests/test_vision3d.py -v`

No requiere Vision3D server, GPU, ni dependencias MCP/Maya.

### Maya Bridge — `tests/test_maya_bridge.py` (24 tests, all passing)

Suite de pytest para `core/maya_bridge.py` y las tool functions de `core/server.py` que dependen del bridge TCP. Usa un mock TCP server (definido en `conftest.py`) — no requiere Maya abierto.

| Clase | Tests | Cubre |
|---|---|---|
| TestTCPConnection | 3 | Conexión TCP al mock, múltiples comandos, config host/port |
| TestSendReceive | 6 | MEL round-trip, execute raw/json/fallback, ERROR: raise, unicode |
| TestTimeout | 3 | ConnectionRefused → MayaConnectionError, timeout, unreachable host |
| TestMayaPing | 4 | ping() retorna version+scene, named scene, non-dict fallback, refused |
| TestMayaCreatePrimitive | 4 | cube default, sphere named+positioned, cylinder all transforms, 6 types |
| TestMayaExecutePython | 4 | Code forwarding, stats increment, safety block, bridge error handling |

Run: `pytest tests/test_maya_bridge.py -v`

Fixtures en `conftest.py`: `MockMayaTCPServer` (mock TCP), `mock_maya_server` (fixture), `bridge_to_mock` (MayaBridge→mock).

### RAG search — `tests/test_rag_search.py` (43 tests, all passing)

Suite de pytest para `core/rag/search.py`. Usa mini corpus de 15 chunks con embeddings determinísticos (_DetEF, SHA-256 hash → 64-dim vectors) — no requiere descarga de modelo ni Maya abierto.

| Clase | Tests | Cubre |
|---|---|---|
| TestRagSearchBasic | 5 | search() retorna chunks relevantes para "polyCube", formato, relevancia bounded, n_results |
| TestRagSearchCmds | 3 | Queries de maya.cmds retornan docs del corpus CMDS_API |
| TestRagSearchPyMEL | 3 | Queries PyMEL (PyNode, MeshVertex, DependNode) retornan docs PYMEL_API |
| TestRagSearchArnold | 2 | Queries Arnold (aiStandardSurface, AOV) retornan docs ARNOLD_API |
| TestRagSearchUSD | 2 | Queries USD (Stage/Prims, UsdShade) retornan docs USD_API |
| TestRagSearchAntiPatterns | 3 | Queries anti-patterns retornan warnings, verificación de corpus |
| TestRagSearchHydeExpansion | 6 | _hyde_expand() detecta dominio correcto (PyMEL/Arnold/USD/MEL/cmds) |
| TestRagSearchRrfFusion | 6 | _rrf_fuse() merge, boost overlapping, preserva orden, empty inputs, integración |
| TestRagSearchBm25Exact | 3 | BM25 matchea tokens exactos (polyCube, Arnold shader) |
| TestRagSearchEmptyIndex | 3 | Index vacío/ausente retorna mensaje informativo, relevance 0 |
| TestRagSearchNoMatch | 3 | Queries irrelevantes no crashean, retornan output formateado |
| TestRagSearchCache | 4 | A12 cache: identical queries cached, diferentes no cross-cached, clear_cache() |

Fixtures RAG en `conftest.py`: `MINI_RAG_CORPUS` (15 chunks, 5 APIs), `_make_deterministic_embedding_fn()` (_DetEF), `rag_chroma_collection`, `rag_corpus_json`, `rag_empty_collection`, `patch_rag_singletons`.

Run: `pytest tests/test_rag_search.py -v`

---

## Bugs conocidos

- `console/app.py:36` tenía hardcodeado `~/Claude_projects/fpt-mcp/.env` para cargar ANTHROPIC_API_KEY — refactorizado a búsqueda dinámica (2026-04-05)
- ~~8 tests en `test_maya_bridge.py` (TestMayaCreatePrimitive + TestMayaExecutePython) fallan por `ModuleNotFoundError: No module named 'mcp'`~~ → resuelto 2026-04-05: stub de mcp SDK movido a `conftest.py` (nivel módulo), todos los tests pasan.
- ~~`console/claude_worker.py`: `subprocess.Popen` no pasaba `cwd=`, heredando el CWD de Maya. Claude CLI no encontraba MCP servers registrados en `.claude/settings.json` del proyecto~~ → resuelto 2026-04-05: añadido `cwd=_REPO_ROOT` derivado de `Path(__file__).resolve().parent.parent`.

---

## Rutas hardcodeadas

### En código ejecutable (.py)

| Archivo | Ruta | Uso | Impacto |
|---|---|---|---|
| `console/app.py` | (refactorizado 2026-04-05) | Carga dinámica de .env | ✅ Resuelto |
| `console/claude_worker.py` | `~/.volta/bin`, `~/.npm-global/bin`, `~/.local/bin`, `~/.nvm/versions/node/*/bin` | Node.js discovery | Bajo (búsqueda) |
| `console/build_app_bundle.py` | `~/Applications` | Default output .app bundle | Bajo |
| `console/server_panel.py` | `~/.claude.json` | Claude Code config discovery | Bajo (path estándar) |
| `core/server.py` | `~/Library/Preferences/Autodesk/maya/...`, `~/maya/...` | userSetup.py discovery | Bajo (paths estándar de Maya) |

Todos usan `os.path.expanduser()` (no absolutas puras). Los paths de Maya son estándar y correctos.

### En documentación (.md)

| Archivo | Rutas |
|---|---|
| `CLAUDE.md` | `~/Claude_projects/maya-mcp/`, `~/.claude.json`, `~/.claude/settings.json` |
| `README.md` | `~/Library/Preferences/Autodesk/maya/`, `~/Library/Application Support/Claude/` |

---

## Script de instalación: install.sh

`install.sh` en la raíz del repo automatiza la instalación completa desde un clone limpio. Es idempotente (ejecutarlo dos veces no rompe nada). Funciona en macOS y Linux.

### Pasos que ejecuta

| Paso | Acción |
|------|--------|
| 1 | Verifica Python 3.10+ (`python3` o `python`) |
| 2 | Crea `.venv/` en la raíz del repo si no existe |
| 3 | Instala `core/requirements.txt` + RAG extras (`chromadb`, `sentence-transformers`, `rank-bm25`) |
| 4 | Construye el RAG index vía `python -m core.rag.build_index` (skip si ya existe) |
| 5 | Registra/actualiza la entrada `maya-mcp` en `~/.claude.json` (usa `jq` si disponible, Python como fallback) |
| 6 | Muestra resumen con ✓/⚠/✗ por paso y próximos pasos manuales |

### Notas de diseño

- **Venv en raíz**: `.venv/` en `maya-mcp/` (no en `core/`), consistente con la ruta que usa `server_panel.py` y el ejemplo de `claude mcp add` del README.
- **Sin setup.py**: el proyecto no tiene `pyproject.toml` ni `setup.py`, por lo que se usa `pip install -r core/requirements.txt`.
- **RAG extras separados**: `chromadb`, `sentence-transformers` y `rank-bm25` no están en `core/requirements.txt` (el requirements de runtime es mínimo), pero el script los instala para que el índice funcione.
- **RAG build skip**: si `core/rag/index/` y `core/rag/corpus.json` ya existen, se omite el rebuild (el índice viene committed en el repo).
- **~/.claude.json idempotente**: el entry se hace upsert — si ya existía, se sobreescribe con las rutas actuales del clone. No duplica.
- **Errores no fatales**: RAG build y registro JSON no abortan la instalación; se reportan como warnings/errors en el resumen final.

### Uso

```bash
chmod +x install.sh
./install.sh
```

---

## Pendiente

- ~~Crear tests automatizados (prioritario)~~ → safety tests creados (67 tests)
- ~~Crear tests para server.py (MCP tools) — requiere mocks de Maya bridge~~ → bridge tests creados (24 tests)
- Ampliar `check_dangerous` a más tools (actualmente solo `maya_delete` y `maya_execute_python`)
- Documentar test plan completo (equivalente al de flame-mcp)
- Evaluar si el auto-setup del panel (inyección en userSetup.py) necesita confirmación del usuario
- Crear tests de safety para flame-mcp (20 patrones + AST, inline en flame_mcp_server.py)

---

## Última actualización: 2026-04-07 (sesión 5) — Dispatch pattern O1b implementado.

### Tarea 1 — asyncio modernizado en `test_maya_bridge.py`
- Reemplazadas las 8 ocurrencias de `asyncio.get_event_loop().run_until_complete()` por `asyncio.run()` en `TestMayaCreatePrimitive` (4) y `TestMayaExecutePython` (4).
- Añadido `import asyncio` al top-level del archivo; eliminados los `import asyncio` inline dentro de cada método.

### Tarea 2 — Stubs compartidos en `conftest.py`
- **conftest.py**: Añadido stub del mcp SDK al nivel del módulo (guard `if "mcp" not in sys.modules`). Añadido fixture `mock_ctx` (AsyncMock con `ctx.info`). Añadidos `import types as _types` y `AsyncMock` a los imports.
- **test_vision3d.py**: Eliminados los bloques de stubs inline de mcp, maya_bridge y safety (≈60 líneas). Eliminados `_make_mock_ctx()` y el fixture local `mock_ctx`. Eliminados `import types` y `AsyncMock` (ya no necesarios). El archivo usa el `mock_ctx` fixture de conftest.py.
- **test_import_file.py**: Eliminados los bloques de stubs inline de mcp, maya_bridge y safety (≈60 líneas). Eliminados `import types`, `from unittest.mock import patch, MagicMock`. Añadido `from maya_bridge import MayaBridgeError` (módulo real de core/). Actualizado `_StubMayaBridgeError` → `MayaBridgeError` en `test_bridge_error_returns_message`.
- Resultado: maya_bridge y safety usan los módulos reales de `core/` (no tienen dependencias externas). mcp se sigue stubbing desde conftest.py.

### Tarea 3 — `tests/requirements-test.txt` creado
Dependencias documentadas: `pytest>=7.4.0`, `pytest-asyncio>=0.23.0`, `httpx>=0.27.0`, `chromadb>=0.5.0`, `rank-bm25>=0.2.2`. Incluida nota explicando que mcp SDK NO es necesario (el stub de conftest.py lo reemplaza).

### Resultado pytest
`python -m pytest tests/ -v` → **174 passed, 0 failed** en 1.67s (sandbox Linux). Todos los test files corren correctamente juntos en un mismo proceso pytest sin colisiones de `sys.modules`.

---

### Sesión 4 — 2026-04-05 — Script install.sh

- **install.sh creado** en raíz del repo. Automatiza los 5 pasos de instalación (Python check, venv, deps, RAG build, ~/.claude.json). Idempotente, funciona en macOS y Linux.
- **HANDOFF.md actualizado** con sección "Script de instalación" documentando diseño y uso.
- No se modificó código fuente del proyecto.

**Sesión anterior (2026-04-05, sesión 3)**: Fix cwd en ClaudeWorker subprocess. asyncio modernizado en tests. Stubs compartidos en conftest.py. tests/requirements-test.txt creado. 174 tests pasando.

---

## Sesión 5 — 2026-04-07 — Dispatch pattern O1b

### Objetivo
Implementar el dispatch pattern definido en `COWORK_O1_DISPATCH.md` para reducir los tools visibles de 27 a 14, compatibilidad con Qwen 3.5 9B (8K context).

### Cambios en `core/server.py`

**Nuevos Enums y modelos Pydantic:**
```python
class SessionAction(str, Enum):
    PING = "ping"; LAUNCH = "launch"; NEW_SCENE = "new_scene"
    SAVE_SCENE = "save_scene"; LIST_SCENE = "list_scene"
    SCENE_SNAPSHOT = "scene_snapshot"; DELETE = "delete"
    EXECUTE_PYTHON = "execute_python"; SHELF_BUTTON = "shelf_button"

class Vision3DAction(str, Enum):
    HEALTH="health"; GENERATE_IMAGE="generate_image"; GENERATE_TEXT="generate_text"
    TEXTURE="texture"; POLL="poll"; DOWNLOAD="download"
```

**Herramientas convertidas a `_do_*` (sin `@mcp.tool`):**
- 9 session handlers: `_do_ping`, `_do_launch`, `_do_new_scene`, `_do_save_scene`, `_do_list_scene`, `_do_scene_snapshot`, `_do_delete`, `_do_execute_python`, `_do_shelf_button`
- 6 Vision3D handlers: `_do_v3d_health`, `_do_v3d_generate_image`, `_do_v3d_generate_text`, `_do_v3d_texture`, `_do_v3d_poll`, `_do_v3d_download`

**Nuevas dispatch tools (con `@mcp.tool`):**
- `maya_session(params: SessionDispatchInput)` — enruta a 9 session handlers
- `maya_vision3d(params: Vision3DDispatchInput, ctx: Context)` — enruta a 6 V3D handlers, pasa ctx

**Preservado:**
- `asyncio.to_thread(bridge.execute, code)` en `_do_scene_snapshot` y `_do_shelf_button`
- `await ctx.info(...)` en todos los V3D handlers (ctx pasado desde dispatch)
- Validación Pydantic interna en cada `_do_*` via `try/except ValidationError`

**Resultado:**
- `grep -c '@mcp.tool' core/server.py` → **14** (era 27)

### Cambios en tests

**`tests/test_maya_bridge.py`** — `TestMayaExecutePython`:
- `server.maya_execute_python(ExecutePythonInput(code="..."))` → `server._do_execute_python({"code": "..."})`
- 4 tests actualizados a la nueva firma de `_do_*` (dict en lugar de Pydantic model)

**`tests/test_vision3d.py`** — todos los 21 tests:
- `srv.vision3d_health(mock_ctx)` → `srv._do_v3d_health({}, mock_ctx)`
- `srv.vision3d_poll(params, mock_ctx)` → `srv._do_v3d_poll(params.model_dump(), mock_ctx)`
- (mismo patrón para download, generate_remote, generate_text)
- Strings `next_step`: `"vision3d_poll"` → `"poll"`, `"vision3d_download"` → `"download"`

**Resultado pytest:**
- `tests/ --ignore=tests/test_rag_search.py` → **131/131 passed** (test_rag_search.py requiere chromadb — mismo estado previo)
- `tests/test_rag_search.py` → 43 errors (chromadb no instalable en sandbox, ✅ en Mac)
