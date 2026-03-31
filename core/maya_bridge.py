"""
maya_bridge.py — Communication layer with Maya via Command Port.

Manages TCP connection with Maya, sending MEL/Python commands,
and receiving results. It is the shared module used by all
MCP server tools.

Features:
  - Palmer dual connection pattern (execute + read result)
  - Namespace scoping: all internal variables use _mcp_ prefix
  - Undo chunk wrapper: groups operations into a single Ctrl+Z
  - Batch execution: multiple code blocks in a single connection
"""

import socket
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("maya_mcp.bridge")

# Default configuration
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 7001
DEFAULT_TIMEOUT = 10.0


class MayaBridgeError(Exception):
    """Base error for Maya communication problems."""
    pass


class MayaConnectionError(MayaBridgeError):
    """Cannot connect to Maya."""
    pass


class MayaExecutionError(MayaBridgeError):
    """Maya returned an error when executing the command."""
    pass


class MayaBridge:
    """
    Communication bridge with Maya via Command Port (TCP).

    Sends MEL or Python commands to Maya and collects responses.
    Uses Palmer's dual connection pattern: one to execute
    and save result, another to retrieve it.
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
        """Sends a raw MEL command to Maya and returns the response."""
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
                f"Cannot connect to Maya at {self.host}:{self.port}. "
                "Verify that Maya is open and the Command Port is active."
            )
        except socket.timeout:
            raise MayaConnectionError(
                f"Timeout connecting to Maya ({self.timeout}s). "
                "Maya might be busy or the port is incorrect."
            )

    def send_mel(self, command: str) -> str:
        """Executes a MEL command in Maya."""
        logger.debug(f"MEL: {command}")
        result = self._send_raw(command)
        logger.debug(f"Result: {result[:200]}")
        return result

    def send_python(self, code: str) -> str:
        """
        Executes Python code in Maya.

        Uses the temporary file + dual connection pattern:
        1. Writes the Python code to a temporary file in /tmp
        2. Sends Maya a simple MEL command that executes that file
        3. Retrieves _mcp_result in second connection

        This approach avoids all quote escaping, brace,
        and special character problems when passing code inline via MEL.
        """
        import tempfile
        import os

        # Wrap the user code to capture result.
        # All internal variables use _mcp_ prefix to avoid
        # collisions with user variables in the Script Editor.
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

        # Write user code to temporary file
        tmp_user = None
        tmp_wrapper = None
        try:
            # File with the user code
            tmp_user = tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', prefix='_mcp_user_',
                dir='/tmp', delete=False
            )
            tmp_user.write(code)
            tmp_user.close()

            # File with the wrapper that executes the user code
            tmp_wrapper = tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', prefix='_mcp_wrap_',
                dir='/tmp', delete=False
            )
            tmp_wrapper.write(f"_MCP_SCRIPT_PATH = '{tmp_user.name}'\n")
            tmp_wrapper.write(wrapper)
            tmp_wrapper.close()

            # Connection 1: execute the wrapper (simple MEL command, no escaping)
            mel_cmd = f'python("exec(open(\'{tmp_wrapper.name}\').read())")'
            self._send_raw(mel_cmd)

            # Connection 2: retrieve result
            result = self._send_raw('python("print(_mcp_result)")')

            if result.startswith("ERROR:"):
                raise MayaExecutionError(result)

            return result

        finally:
            # Clean up temporary files
            if tmp_user and os.path.exists(tmp_user.name):
                os.unlink(tmp_user.name)
            if tmp_wrapper and os.path.exists(tmp_wrapper.name):
                os.unlink(tmp_wrapper.name)

    def execute(self, code: str, as_json: bool = False) -> Any:
        """
        Executes Python code in Maya with option to parse JSON.

        Args:
            code: Python code to execute in Maya.
                  Must assign its result to the 'result' variable.
            as_json: If True, attempts to parse the response as JSON.

        Returns:
            String with the result, or dict/list if as_json=True.

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
        Executes Python code in Maya wrapped in an undo chunk.

        All operations within the chunk are grouped into a single
        Ctrl+Z, so the user can undo everything a tool did with one undo.

        Args:
            code: Python code to execute.
            chunk_name: Descriptive name of the chunk (visible in undo history).
            as_json: If True, parse result as JSON.
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
        Executes multiple code blocks in a single TCP connection.

        Significantly reduces latency when there are many consecutive
        operations (e.g., create 10 objects, import + adjust + material).

        All blocks are grouped in a single undo chunk.

        Args:
            code_blocks: List of Python code strings.
            chunk_name: Name of the undo chunk.

        Returns:
            List of results (one per block).
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
        Checks the connection with Maya and returns environment info.

        Returns:
            dict with version, platform, scene_objects, etc.
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
