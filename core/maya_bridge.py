"""
maya_bridge.py — Capa de comunicación con Maya via Command Port.

Gestiona la conexión TCP con Maya, envío de comandos MEL/Python,
y recepción de resultados. Es el módulo compartido que usan todos
los tools del MCP server.

Features:
  - Patrón de doble conexión Palmer (execute + read result)
  - Namespace scoping: todas las variables internas usan prefijo _mcp_
  - Undo chunk wrapper: agrupa operaciones en un solo Ctrl+Z
  - Batch execution: múltiples bloques de código en una sola conexión
"""

import socket
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("maya_mcp.bridge")

# Configuración por defecto
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 7001
DEFAULT_TIMEOUT = 10.0


class MayaBridgeError(Exception):
    """Error base para problemas de comunicación con Maya."""
    pass


class MayaConnectionError(MayaBridgeError):
    """No se puede conectar a Maya."""
    pass


class MayaExecutionError(MayaBridgeError):
    """Maya devolvió un error al ejecutar el comando."""
    pass


class MayaBridge:
    """
    Puente de comunicación con Maya via Command Port (TCP).

    Envía comandos MEL o Python a Maya y recoge las respuestas.
    Usa el patrón de doble conexión de Palmer: una para ejecutar
    y guardar resultado, otra para recuperarlo.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT
    ):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _send_raw(self, command: str) -> str:
        """Envía un comando MEL crudo a Maya y devuelve la respuesta."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))
                sock.sendall((command + '\n').encode('utf-8'))

                response = b''
                while True:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        response += chunk
                    except socket.timeout:
                        break

                return response.decode('utf-8').strip()

        except ConnectionRefusedError:
            raise MayaConnectionError(
                f"No se puede conectar a Maya en {self.host}:{self.port}. "
                "Verifica que Maya está abierto y el Command Port activo."
            )
        except socket.timeout:
            raise MayaConnectionError(
                f"Timeout conectando a Maya ({self.timeout}s). "
                "Maya podría estar ocupado o el puerto incorrecto."
            )

    def send_mel(self, command: str) -> str:
        """Ejecuta un comando MEL en Maya."""
        logger.debug(f"MEL: {command}")
        result = self._send_raw(command)
        logger.debug(f"Resultado: {result[:200]}")
        return result

    def send_python(self, code: str) -> str:
        """
        Ejecuta código Python en Maya.

        Usa el patrón de archivo temporal + doble conexión:
        1. Escribe el código Python a un archivo temporal en /tmp
        2. Envía a Maya un comando MEL simple que ejecuta ese archivo
        3. Recupera _mcp_result en segunda conexión

        Este enfoque evita todos los problemas de escape de comillas,
        llaves y caracteres especiales al pasar código inline via MEL.
        """
        import tempfile
        import os

        # Envolver el código del usuario para capturar resultado.
        # Todas las variables internas usan prefijo _mcp_ para evitar
        # colisiones con variables del usuario en el Script Editor.
        wrapper = (
            "import maya.cmds as cmds\n"
            "import json\n"
            "try:\n"
            "    _mcp_result_ns = {}\n"
            "    _mcp_user_code = open(_MCP_SCRIPT_PATH).read()\n"
            "    exec(_mcp_user_code, _mcp_result_ns)\n"
            "    _mcp_result = _mcp_result_ns.get('result', 'OK')\n"
            "    if isinstance(_mcp_result, (list, dict, tuple)):\n"
            "        _mcp_result = json.dumps(_mcp_result)\n"
            "    else:\n"
            "        _mcp_result = str(_mcp_result)\n"
            "except Exception as e:\n"
            "    _mcp_result = f'ERROR: {type(e).__name__}: {e}'\n"
        )

        # Escribir código del usuario a archivo temporal
        tmp_user = None
        tmp_wrapper = None
        try:
            # Archivo con el código del usuario
            tmp_user = tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', prefix='_mcp_user_',
                dir='/tmp', delete=False
            )
            tmp_user.write(code)
            tmp_user.close()

            # Archivo con el wrapper que ejecuta el código del usuario
            tmp_wrapper = tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', prefix='_mcp_wrap_',
                dir='/tmp', delete=False
            )
            tmp_wrapper.write(f"_MCP_SCRIPT_PATH = '{tmp_user.name}'\n")
            tmp_wrapper.write(wrapper)
            tmp_wrapper.close()

            # Conexión 1: ejecutar el wrapper (comando MEL simple, sin escaping)
            mel_cmd = f'python("exec(open(\'{tmp_wrapper.name}\').read())")'
            self._send_raw(mel_cmd)

            # Conexión 2: recuperar resultado
            result = self._send_raw('python("print(_mcp_result)")')

            if result.startswith("ERROR:"):
                raise MayaExecutionError(result)

            return result

        finally:
            # Limpiar archivos temporales
            if tmp_user and os.path.exists(tmp_user.name):
                os.unlink(tmp_user.name)
            if tmp_wrapper and os.path.exists(tmp_wrapper.name):
                os.unlink(tmp_wrapper.name)

    def execute(self, code: str, as_json: bool = False) -> Any:
        """
        Ejecuta código Python en Maya con opción de parsear JSON.

        Args:
            code: Código Python a ejecutar en Maya.
                  Debe asignar su resultado a la variable 'result'.
            as_json: Si True, intenta parsear la respuesta como JSON.

        Returns:
            String con el resultado, o dict/list si as_json=True.

        Example:
            bridge.execute("result = cmds.ls(type='mesh')", as_json=True)
            # → ["pSphereShape1", "pCubeShape1"]
        """
        raw = self.send_python(code)

        if as_json:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return raw

    def execute_in_undo(self, code: str, chunk_name: str = "mcp_operation",
                        as_json: bool = False) -> Any:
        """
        Ejecuta código Python en Maya envuelto en un undo chunk.

        Todas las operaciones dentro del chunk se agrupan en un solo
        Ctrl+Z, para que el usuario pueda deshacer todo lo que hizo
        un tool con un solo undo.

        Args:
            code: Código Python a ejecutar.
            chunk_name: Nombre descriptivo del chunk (visible en undo history).
            as_json: Si True, parsear resultado como JSON.
        """
        wrapped = (
            "import maya.cmds as cmds\n"
            f"cmds.undoInfo(openChunk=True, chunkName='{chunk_name}')\n"
            "try:\n"
        )
        # Indent user code inside the try block
        for line in code.split("\n"):
            wrapped += f"    {line}\n"
        wrapped += (
            "except Exception as _mcp_undo_err:\n"
            "    result = {'error': str(_mcp_undo_err)}\n"
            "finally:\n"
            "    cmds.undoInfo(closeChunk=True)\n"
        )
        return self.execute(wrapped, as_json=as_json)

    def execute_batch(self, code_blocks: list, chunk_name: str = "mcp_batch") -> list:
        """
        Ejecuta múltiples bloques de código en una sola conexión TCP.

        Reduce latencia significativamente cuando hay muchas operaciones
        seguidas (ej: crear 10 objetos, importar + ajustar + material).

        Todos los bloques se agrupan en un solo undo chunk.

        Args:
            code_blocks: Lista de strings de código Python.
            chunk_name: Nombre del undo chunk.

        Returns:
            Lista de resultados (uno por bloque).
        """
        if not code_blocks:
            return []

        # Build a single mega-script that captures results per block
        parts = [
            "import maya.cmds as cmds",
            "import json",
            f"cmds.undoInfo(openChunk=True, chunkName='{chunk_name}')",
            "_mcp_batch_results = []",
            "try:",
        ]
        for i, block in enumerate(code_blocks):
            parts.append(f"    # --- Block {i} ---")
            parts.append("    try:")
            for line in block.split("\n"):
                if line.strip():
                    parts.append(f"        {line}")
            parts.append(f"        _mcp_batch_results.append(result if 'result' in dir() else 'OK')")
            parts.append("    except Exception as _mcp_blk_err:")
            parts.append(f"        _mcp_batch_results.append({{'error': str(_mcp_blk_err)}})")
        parts.append("finally:")
        parts.append("    cmds.undoInfo(closeChunk=True)")
        parts.append("result = _mcp_batch_results")

        combined = "\n".join(parts)
        raw = self.execute(combined)

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return [raw]

    def ping(self) -> dict:
        """
        Verifica la conexión con Maya y devuelve info del entorno.

        Returns:
            dict con version, platform, scene_objects, etc.
        """
        version = self.send_mel("about -v")
        os_info = self.send_mel("about -os")

        code = """
import maya.cmds as cmds
result = {
    'objects': len(cmds.ls()),
    'scene': cmds.file(q=True, sceneName=True) or 'untitled',
    'renderer': cmds.getAttr('defaultRenderGlobals.currentRenderer')
}
"""
        scene_info = self.execute(code, as_json=True)

        return {
            "status": "connected",
            "version": version,
            "os": os_info,
            "scene": scene_info if isinstance(scene_info, dict) else {}
        }
