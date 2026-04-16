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

import os
import socket
import json
import logging
import tempfile
import uuid
from typing import Any, Optional, Tuple

logger = logging.getLogger("maya_mcp.bridge")

# Default configuration
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8100
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
        """Sends a raw MEL command to Maya and returns the response.

        Distinguishes three cases on the recv loop:

        - The peer sends data, then closes (or stops sending). Normal happy
          path; we return the collected bytes.
        - The peer sends nothing and never closes. The recv() call hits its
          socket timeout with an empty buffer. This is the "silent Maya"
          condition (orphaned port after a crash, modal dialog blocking the
          interpreter, long-running command in the queue) — we raise
          MayaConnectionError instead of returning an empty string, which
          the caller would otherwise misinterpret as a successful no-op.
        - The peer sends data, then stops. We have data already, the
          subsequent recv() times out — return what we have. This is how
          Maya's command port behaves in normal operation since the
          protocol has no terminator.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))
                sock.sendall((command + '\n').encode('utf-8'))

                response = b''
                got_any = False  # True once recv() has returned at least once
                while True:
                    try:
                        chunk = sock.recv(4096)
                        got_any = True
                        if not chunk:
                            break  # peer closed cleanly
                        response += chunk
                    except socket.timeout:
                        if not got_any:
                            raise MayaConnectionError(
                                f"Maya Command Port at {self.host}:{self.port} "
                                f"accepted the connection but returned no data "
                                f"within {self.timeout}s. Maya may be blocked by a "
                                "modal dialog, executing a long-running command, "
                                "or the Command Port may be orphaned after a crash."
                            )
                        break

                return response.decode('utf-8').strip().strip('\x00')

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

    # ── Wrapper file body (module-level template) ───────────────────────────
    # Uses _mcp_ prefix on every internal symbol to avoid collisions with
    # user variables in the Script Editor. The wrapper exec()s the user code
    # in an isolated namespace, stringifies the result, and writes it to
    # _MCP_RESULT_PATH so the bridge can read it from the local filesystem
    # without depending on Maya's command-port stdout capture.
    #
    # Pre-populates the user namespace with ``cmds`` and ``json``: every
    # server.py tool generates Python that assumes ``cmds`` is in scope, and
    # direct ``bridge.execute()`` callers expect the same. Before this was
    # fixed the wrapper imported cmds at its own module level which did NOT
    # reach the user exec namespace, so callers had to redundantly import
    # maya.cmds themselves; any code that did not was greeted with a NameError.
    _WRAPPER_BODY = (
        "import maya.cmds as _mcp_cmds\n"
        "import json as _mcp_json\n"
        "try:\n"
        "    _mcp_result_ns = {'cmds': _mcp_cmds, 'json': _mcp_json}\n"
        "    _mcp_user_code = open(_MCP_SCRIPT_PATH).read()\n"
        "    exec(_mcp_user_code, _mcp_result_ns)\n"
        "    _mcp_result = _mcp_result_ns.get('result', 'OK')\n"
        "    if isinstance(_mcp_result, (list, dict, tuple)):\n"
        "        _mcp_payload = _mcp_json.dumps(_mcp_result)\n"
        "    else:\n"
        "        _mcp_payload = str(_mcp_result)\n"
        "except Exception as _mcp_e:\n"
        "    _mcp_payload = 'ERROR: ' + type(_mcp_e).__name__ + ': ' + str(_mcp_e)\n"
        "with open(_MCP_RESULT_PATH, 'w') as _mcp_fh:\n"
        "    _mcp_fh.write(_mcp_payload)\n"
    )

    # Diagnostic appended to MayaExecutionError when the wrapper does not
    # produce a result file. Reproduces the user-facing remediation snippet.
    _RESULT_FILE_MISSING_HINT = (
        "Maya accepted the wrapper command but no result file was produced. "
        "The Python interpreter inside Maya may be blocked (modal dialog, "
        "long-running command) or the Command Port may be in a degraded state "
        "(orphaned after a crash, opened in the wrong sourceType, or the "
        "wrapper file path is not reachable from Maya's process).\n\n"
        "The bridge sends MEL commands and wraps Python in MEL python(...) "
        "calls, so the Command Port MUST be opened with sourceType='mel'. "
        "To recover, run this in Maya's Script Editor (Python tab):\n"
        "    import maya.cmds as cmds\n"
        "    if cmds.commandPort('localhost:8100', query=True):\n"
        "        cmds.commandPort('localhost:8100', close=True)\n"
        "    cmds.commandPort('localhost:8100', sourceType='mel')\n"
        "    print('Command port restarted')"
    )

    @staticmethod
    def _prepare_wrapper_files(code: str) -> Tuple[str, str, str]:
        """Write user code + wrapper bootstrap to /tmp.

        Returns (user_path, wrapper_path, result_path). The wrapper imports
        the user code via _MCP_SCRIPT_PATH and writes its result to
        _MCP_RESULT_PATH; the bridge reads that file locally. result_path
        embeds a uuid so concurrent calls never collide.
        """
        tmp_user = tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', prefix='_mcp_user_',
            dir='/tmp', delete=False
        )
        tmp_user.write(code)
        tmp_user.close()

        result_path = os.path.join(
            '/tmp', f'_mcp_result_{os.getpid()}_{uuid.uuid4().hex}.json'
        )

        tmp_wrapper = tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', prefix='_mcp_wrap_',
            dir='/tmp', delete=False
        )
        tmp_wrapper.write(f"_MCP_SCRIPT_PATH = {tmp_user.name!r}\n")
        tmp_wrapper.write(f"_MCP_RESULT_PATH = {result_path!r}\n")
        tmp_wrapper.write(MayaBridge._WRAPPER_BODY)
        tmp_wrapper.close()

        return tmp_user.name, tmp_wrapper.name, result_path

    @staticmethod
    def _cleanup_temp_files(*paths: Optional[str]) -> None:
        """Best-effort unlink of every path. Missing files are silently ignored."""
        for path in paths:
            if not path:
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Failed to remove temp file %s: %s", path, exc)

    def send_python(self, code: str) -> str:
        """
        Executes Python code in Maya and returns the result as a string.

        Single-connection file-based return:
        1. Write user code + wrapper bootstrap + result path to /tmp
        2. Send Maya a single MEL command that exec()s the wrapper
        3. The wrapper writes the stringified result to _MCP_RESULT_PATH
        4. Bridge reads the result file from local disk

        This bypasses Maya's command-port stdout capture entirely, so the
        result return path keeps working even if the Command Port was opened
        without echoOutput=True or if the print()/return wiring is degraded.

        Raises:
            MayaConnectionError: cannot reach the Command Port at all.
            MayaExecutionError: the wrapper raised, OR Maya accepted the
                command but produced no result file (blocked interpreter,
                orphaned port, modal dialog).
        """
        user_path: Optional[str] = None
        wrapper_path: Optional[str] = None
        result_path: Optional[str] = None
        try:
            user_path, wrapper_path, result_path = self._prepare_wrapper_files(code)

            # Single MEL command: load and exec the wrapper script in Maya.
            # We do NOT depend on the response payload — the wrapper writes
            # its result to _MCP_RESULT_PATH and we read it locally below.
            mel_cmd = f'python("exec(open(\'{wrapper_path}\').read())")'
            self._send_raw(mel_cmd)

            if not os.path.exists(result_path):
                raise MayaExecutionError(self._RESULT_FILE_MISSING_HINT)

            with open(result_path, 'r', encoding='utf-8') as fh:
                result = fh.read()

            if result.startswith("ERROR:"):
                raise MayaExecutionError(result)

            return result

        finally:
            self._cleanup_temp_files(user_path, wrapper_path, result_path)

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
